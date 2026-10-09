"""Local message database.

The file lives under the user's data directory and is readable only by
that user. It remembers the phone number you typed, the paired phone,
contacts, and texts. It never stores an Apple password because this
plugin never asks for one.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path

from protocol import address_from_thread, present_message, thread_key

# Every event line goes into the shell, so what one line can carry is capped.
THREADS_MAX = 300
MESSAGES_MAX = 500
BODY_MAX = 4000
LABEL_MAX = 200


def _ensure_mode(path: Path, mode: int) -> None:
    """chmod only when needed. A chmod always fires an inotify event, and the
    Omarchy shell rescans the app list on any change under ~/.local/share."""
    if path.stat().st_mode & 0o777 != mode:
        os.chmod(path, mode)


def data_dir() -> Path:
    """Directory that holds the database. Created as mode 0700."""
    root = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    path = Path(root) / "omaiphone"
    path.mkdir(parents=True, exist_ok=True)
    _ensure_mode(path, 0o700)
    return path


def shared_contacts_file() -> Path:
    """The phone book the calls part of OmaiPhone downloads (PBAP) for both parts."""
    return data_dir() / "contacts.json"


def runtime_dir() -> Path:
    """Directory for short-lived files such as an outgoing bMessage."""
    # Without a runtime dir, fall back to the private cache dir, never shared /tmp.
    root = os.environ.get("XDG_RUNTIME_DIR") or os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    path = Path(root) / "omaiphone"
    path.mkdir(parents=True, exist_ok=True)
    _ensure_mode(path, 0o700)
    return path


class Store:
    """SQLite wrapper. Use one instance from the daemon's main task only."""

    def __init__(self, path: Path | None = None):
        self.path = path or (data_dir() / "messages.db")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        _ensure_mode(self.path.parent, 0o700)
        self._db = sqlite3.connect(self.path)
        self._db.row_factory = sqlite3.Row
        # DELETE mode keeps the texts in one file. WAL would create extra
        # files that are easy to leave world-readable.
        self._db.execute("PRAGMA journal_mode=DELETE")
        self._migrate()
        _ensure_mode(self.path, 0o600)

    def close(self) -> None:
        self._db.close()

    def _migrate(self) -> None:
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS settings (
              id INTEGER PRIMARY KEY CHECK (id = 1),
              own_number TEXT NOT NULL DEFAULT '',
              phone_address TEXT NOT NULL DEFAULT '',
              phone_name TEXT NOT NULL DEFAULT '',
              notify_body INTEGER NOT NULL DEFAULT 0
            );
            INSERT OR IGNORE INTO settings (id) VALUES (1);

            CREATE TABLE IF NOT EXISTS contacts (
              id INTEGER PRIMARY KEY,
              name TEXT NOT NULL,
              address TEXT NOT NULL,
              kind TEXT NOT NULL,
              UNIQUE(address, kind)
            );

            CREATE TABLE IF NOT EXISTS messages (
              id INTEGER PRIMARY KEY,
              external_id TEXT UNIQUE,
              thread_key TEXT NOT NULL,
              peer_label TEXT NOT NULL,
              body TEXT NOT NULL,
              direction TEXT NOT NULL,
              timestamp TEXT NOT NULL,
              read INTEGER NOT NULL DEFAULT 0,
              has_attachment INTEGER NOT NULL DEFAULT 0,
              body_complete INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS messages_thread ON messages(thread_key, timestamp);
            """
        )
        self._db.commit()

    def settings(self) -> dict:
        row = self._db.execute("SELECT * FROM settings WHERE id = 1").fetchone()
        return {
            "ownNumber": row["own_number"],
            "phoneAddress": row["phone_address"],
            "phoneName": row["phone_name"],
            "notifyBody": bool(row["notify_body"]),
        }

    def set_own_number(self, number: str) -> None:
        self._db.execute("UPDATE settings SET own_number = ? WHERE id = 1", (number,))
        self._db.commit()

    def set_phone(self, address: str, name: str) -> None:
        self._db.execute(
            "UPDATE settings SET phone_address = ?, phone_name = ? WHERE id = 1",
            (address, name),
        )
        self._db.commit()

    def set_notify_body(self, enabled: bool) -> None:
        self._db.execute(
            "UPDATE settings SET notify_body = ? WHERE id = 1",
            (1 if enabled else 0,),
        )
        self._db.commit()

    def unread_count(self) -> int:
        row = self._db.execute(
            "SELECT COUNT(*) AS n FROM messages WHERE read = 0 AND direction = 'in'"
        ).fetchone()
        return int(row["n"])

    def replace_contacts(self, people: list[dict]) -> None:
        """Swap the contact cache. `people` items have name, address, and kind."""
        self._db.execute("DELETE FROM contacts")
        for person in people:
            address = str(person.get("address") or "")
            kind = str(person.get("kind") or "")
            name = str(person.get("name") or address)
            if not address or kind not in ("tel", "email"):
                continue
            self._db.execute(
                "INSERT OR REPLACE INTO contacts (name, address, kind) VALUES (?, ?, ?)",
                (name[:200], address, kind),
            )
        self._db.commit()

    def contact_count(self) -> int:
        row = self._db.execute("SELECT COUNT(*) AS n FROM contacts").fetchone()
        return int(row["n"])

    def search_contacts(self, query: str, limit: int = 300) -> list[dict]:
        """Phone-book matches, plus people you already have a conversation with.

        The phone book arrives only after Sync Contacts is on. Until then,
        the person who texted you can still be chosen for a reply.
        """
        like = f"%{query.strip().lower()}%"
        rows = self._db.execute(
            """
            SELECT name, address, kind FROM contacts
            WHERE lower(name) LIKE ? OR lower(address) LIKE ?
            ORDER BY name COLLATE NOCASE, address
            LIMIT ?
            """,
            (like, like, limit),
        ).fetchall()
        found = [{"name": row["name"], "address": row["address"], "kind": row["kind"]} for row in rows]
        seen = {row["address"] for row in found}
        needle = query.strip().lower()
        threads = self._db.execute(
            "SELECT thread_key, MAX(peer_label) AS peer_label FROM messages GROUP BY thread_key"
        ).fetchall()
        for thread in threads:
            address = address_from_thread(thread["thread_key"])
            if not address or address in seen:
                continue
            name = thread["peer_label"] or address
            if needle and needle not in name.lower() and needle not in address.lower():
                continue
            kind = "email" if "@" in address else "tel"
            found.append({"name": name, "address": address, "kind": kind})
            seen.add(address)
            if len(found) >= limit:
                break
        return found

    def apply_contact_names(self) -> int:
        """Replace a bare phone number on a conversation with the contact's name."""
        changed = 0
        rows = self._db.execute("SELECT id, thread_key, peer_label FROM messages").fetchall()
        for row in rows:
            address = address_from_thread(row["thread_key"])
            if not address:
                continue
            name = self.label_for(address)
            if not name or name == address or name == row["peer_label"]:
                continue
            self._db.execute("UPDATE messages SET peer_label = ? WHERE id = ?", (name, row["id"]))
            changed += 1
        if changed:
            self._db.commit()
        return changed

    def label_for(self, address: str) -> str:
        """Contact name for an address, or the address itself when unknown."""
        row = self._db.execute(
            "SELECT name FROM contacts WHERE address = ? ORDER BY name LIMIT 1",
            (address,),
        ).fetchone()
        if row and row["name"]:
            return row["name"]
        return address

    def upsert_message(
        self,
        external_id: str,
        address: str,
        body: str,
        direction: str,
        timestamp: str,
        read: bool,
        has_attachment: bool,
        body_complete: bool,
        peer_label: str = "",
    ) -> tuple[dict, bool]:
        """Insert or update one text. Returns the row and whether it was new."""
        own = self.settings()["ownNumber"]
        key = thread_key(address, own)
        label = peer_label or self.label_for(address) or ""
        if not label or label == address:
            earlier = self._db.execute(
                """
                SELECT peer_label FROM messages
                WHERE thread_key = ? AND peer_label != '' AND peer_label != ?
                ORDER BY id LIMIT 1
                """,
                (key, address),
            ).fetchone()
            if earlier:
                label = earlier["peer_label"]
        if not label:
            label = address
        existing = self._db.execute(
            "SELECT id, read, body_complete FROM messages WHERE external_id = ?",
            (external_id,),
        ).fetchone()
        if existing:
            # A later sync must not mark a text unread again after you opened it.
            next_read = 1 if existing["read"] or read else 0
            next_complete = 1 if existing["body_complete"] or body_complete else 0
            next_body = body if body_complete or not existing["body_complete"] else None
            if next_body is None:
                self._db.execute(
                    """
                    UPDATE messages
                    SET read = ?, has_attachment = ?, body_complete = ?, peer_label = ?
                    WHERE id = ?
                    """,
                    (next_read, 1 if has_attachment else 0, next_complete, label, existing["id"]),
                )
            else:
                self._db.execute(
                    """
                    UPDATE messages
                    SET body = ?, read = ?, has_attachment = ?, body_complete = ?, peer_label = ?
                    WHERE id = ?
                    """,
                    (body, next_read, 1 if has_attachment else 0, next_complete, label, existing["id"]),
                )
            self._db.commit()
            return self._message_by_id(existing["id"]), False

        cur = self._db.execute(
            """
            INSERT INTO messages (
              external_id, thread_key, peer_label, body, direction, timestamp,
              read, has_attachment, body_complete
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                external_id,
                key,
                label,
                body,
                direction,
                timestamp,
                1 if read else 0,
                1 if has_attachment else 0,
                1 if body_complete else 0,
            ),
        )
        self._db.commit()
        return self._message_by_id(cur.lastrowid), True

    def _message_by_id(self, message_id: int) -> dict:
        row = self._db.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
        return self._public_message(row)

    def _public_message(self, row: sqlite3.Row) -> dict:
        return {
            "id": row["id"],
            "thread": row["thread_key"],
            "label": (row["peer_label"] or "")[:LABEL_MAX],
            "body": present_message(row["body"], bool(row["has_attachment"]))[:BODY_MAX],
            "direction": row["direction"],
            "timestamp": row["timestamp"],
            "read": bool(row["read"]),
            "bodyComplete": bool(row["body_complete"]),
        }

    def threads(self) -> list[dict]:
        rows = self._db.execute(
            """
            SELECT m.thread_key, m.peer_label, m.body, m.has_attachment, m.timestamp,
                   (
                     SELECT COUNT(*) FROM messages u
                     WHERE u.thread_key = m.thread_key AND u.read = 0 AND u.direction = 'in'
                   ) AS unread
            FROM messages m
            JOIN (
              SELECT thread_key, MAX(timestamp) AS timestamp
              FROM messages GROUP BY thread_key
            ) latest ON latest.thread_key = m.thread_key AND latest.timestamp = m.timestamp
            ORDER BY unread DESC, m.timestamp DESC
            """
        ).fetchall()
        seen = set()
        result = []
        for row in rows:
            if row["thread_key"] in seen:
                continue
            seen.add(row["thread_key"])
            preview = present_message(row["body"], bool(row["has_attachment"])).replace("\n", " ")
            result.append(
                {
                    "key": row["thread_key"],
                    "label": (row["peer_label"] or address_from_thread(row["thread_key"]) or "Unknown")[:LABEL_MAX],
                    "preview": preview[:80],
                    "unread": int(row["unread"]),
                    "timestamp": row["timestamp"],
                }
            )
            if len(result) >= THREADS_MAX:
                break
        return result

    def messages_for(self, key: str) -> list[dict]:
        rows = self._db.execute(
            "SELECT * FROM messages WHERE thread_key = ? ORDER BY timestamp DESC, id DESC LIMIT ?",
            (key, MESSAGES_MAX),
        ).fetchall()
        # The newest MESSAGES_MAX, oldest first.
        return [self._public_message(row) for row in reversed(rows)]

    def record(self, external_id: str):
        """Internal row for one MAP handle. Used when filling in a full body."""
        return self._db.execute(
            "SELECT * FROM messages WHERE external_id = ?",
            (external_id,),
        ).fetchone()

    def incomplete_in_thread(self, key: str) -> list[str]:
        rows = self._db.execute(
            """
            SELECT external_id FROM messages
            WHERE thread_key = ? AND body_complete = 0 AND external_id NOT LIKE 'local-%'
            """,
            (key,),
        ).fetchall()
        return [row["external_id"] for row in rows]

    def mark_thread_read(self, key: str) -> list[str]:
        """Mark a conversation read locally. Returns MAP handles still worth updating on the phone."""
        rows = self._db.execute(
            """
            SELECT external_id FROM messages
            WHERE thread_key = ? AND read = 0 AND external_id NOT LIKE 'local-%'
            """,
            (key,),
        ).fetchall()
        self._db.execute("UPDATE messages SET read = 1 WHERE thread_key = ?", (key,))
        self._db.commit()
        return [row["external_id"] for row in rows]

    @staticmethod
    def now_stamp() -> str:
        return time.strftime("%Y-%m-%dT%H:%M:%S")
