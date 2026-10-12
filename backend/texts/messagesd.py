#!/usr/bin/env python3
"""Messages part of OmaiPhone (texts over MAP), based on iMsg by Nikolaos Gkionis.

The overlay talks to this process with one JSON object per line on
stdin. Events go out as JSON lines on stdout. Stderr is only for
connection state. Message text, contact names, pairing codes, and your
phone number are never written there.

This process runs as your user. It does not use sudo, and it does
not start a shell to run commands.
"""

from __future__ import annotations

import asyncio
import json
import sys

from protocol import address_from_thread, normalize_phone, notification_title, public_error
from store import Store

_MAC = __import__("re").compile(r"^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")


# The event loop keeps only a weak reference to tasks. Without this set,
# a send or a notification can be garbage-collected before it finishes.
_background: set[asyncio.Task] = set()


def spawn(coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _background.add(task)
    task.add_done_callback(_background.discard)
    return task


def emit(event: dict) -> None:
    """Send one event to the shell. The caller must already have removed secrets from logs."""
    sys.stdout.write(json.dumps(event, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _plain_title(value: str, limit: int) -> str:
    """One plain line for the title. Omarchy shows titles as plain text (tested),
    so escaping it would show "Tom &amp; Jerry"."""
    return " ".join(str(value or "").replace("\x00", "").split())[:limit]


def notification_text(value: str, limit: int, keep_newlines: bool = False) -> str:
    """Plain text for a desktop notification.

    notify-send and the notification daemon treat the body as markup.
    A name or a text from the phone must not become an option (`--urgency`)
    or a tag (`<img>`, `<a href>`).
    """
    text = str(value or "").replace("\x00", "")
    if keep_newlines:
        text = text.replace("\r\n", "\n").replace("\r", "\n")
    else:
        text = " ".join(text.split())
    text = text[:limit]
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def notification_argv(title: str, body: str) -> list[str]:
    """notify-send arguments. `--` keeps a hostile title from being an option."""
    safe_title = _plain_title(title, 80) or "New message"
    args = ["notify-send", "--app-name=Messages", "--urgency=normal", "--", safe_title]
    if body:
        args.append(notification_text(body, 120, keep_newlines=True))
    return args


def log_state(state: str) -> None:
    """Record a connection state. The string is one of the fixed state names."""
    if state not in {
        "not_paired",
        "scanning",
        "pairing",
        "phone_nearby",
        "waiting_for_switches",
        "connected",
    }:
        state = "unknown"
    print(f"state {state}", file=sys.stderr, flush=True)


def status_from_store(store: Store, ready: bool, detail: str) -> dict:
    settings = store.settings()
    return {
        "state": "not_paired" if not settings["phoneAddress"] else "phone_nearby",
        "phoneName": settings["phoneName"],
        "phoneAddress": settings["phoneAddress"],
        "ownNumber": settings["ownNumber"],
        "unread": store.unread_count(),
        "notifyBody": settings["notifyBody"],
        "detail": detail,
        "pairFallback": False,
        "bluetoothReady": ready,
    }


# A burst of texts must not start a notify-send for every one of them.
_notify_running = 0
_NOTIFY_LIMIT = 2


async def notify(title: str, body: str) -> None:
    """Desktop notification. The title is a name, never a phone number."""
    global _notify_running
    if _notify_running >= _NOTIFY_LIMIT:
        return
    _notify_running += 1
    try:
        proc = await asyncio.create_subprocess_exec(
            *notification_argv(title, body),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except TimeoutError:
            proc.kill()
    except FileNotFoundError:
        return
    finally:
        _notify_running -= 1


async def handle(raw: str, store: Store, session) -> None:
    try:
        command = json.loads(raw)
    except json.JSONDecodeError:
        emit({"event": "error", "message": "Command was not valid JSON."})
        return
    if not isinstance(command, dict):
        emit({"event": "error", "message": "Command was not an object."})
        return
    name = str(command.get("cmd") or "")
    try:
        await dispatch(name, command, store, session)
    except Exception as exc:
        emit({"event": "error", "message": public_error(exc)})


async def dispatch(name: str, command: dict, store: Store, session) -> None:
    if name == "status":
        if session:
            session.push_status()
        else:
            emit({"event": "status", "status": status_from_store(store, False, "Bluetooth library is missing.")})
        return

    if name == "set-number":
        number = normalize_phone(str(command.get("number") or ""))
        if not number:
            raise ValueError("Enter your number in international form, like +15551234567.")
        store.set_own_number(number)
        if session:
            session.push_status()
        else:
            emit({"event": "status", "status": status_from_store(store, False, "")})
        return

    if name == "set-notify-body":
        store.set_notify_body(bool(command.get("enabled")))
        if session:
            session.push_status()
        else:
            emit({"event": "status", "status": status_from_store(store, False, "")})
        return

    if name == "threads":
        emit({"event": "threads", "threads": store.threads()})
        return

    if name == "messages":
        key = str(command.get("thread") or "")
        emit({"event": "messages", "thread": key, "messages": store.messages_for(key)})
        if session:
            await session.fetch_thread(key)
            emit({"event": "messages", "thread": key, "messages": store.messages_for(key)})
        return

    if name == "contacts":
        query = str(command.get("query") or "")[:80]
        emit({"event": "contacts", "contacts": store.search_contacts(query)})
        return

    if name == "mark-read":
        key = str(command.get("thread") or "")
        if session:
            await session.mark_read(key)
        else:
            store.mark_thread_read(key)
            emit({"event": "threads", "threads": store.threads()})
        return

    if session is None:
        raise RuntimeError("Bluetooth messaging is not available yet.")

    if name == "scan":
        await session.scan()
        return

    if name == "reconnect":
        # Opening the card retries message access. The iPhone switch
        # can be flipped while this program is already running.
        await session.reconnect()
        return

    if name == "pair":
        address = str(command.get("address") or "")
        if not _MAC.match(address):
            raise ValueError("Pick a phone from the scan list.")
        await session.pair(address, bool(command.get("explicit")))
        return

    if name == "confirm":
        session.confirm(bool(command.get("accept")))
        return

    if name == "cancel":
        session.confirm(False)
        return

    if name == "send":
        body = str(command.get("body") or "")
        address = str(command.get("address") or "")
        if not address:
            address = address_from_thread(str(command.get("thread") or ""))
        if not address:
            raise ValueError("Choose a contact or a conversation first.")
        await session.send(address, body)
        emit({"event": "sent", "thread": str(command.get("thread") or "")})
        return

    raise ValueError("Unknown command.")


def notifying_emit(store: Store, inner):
    """Wrap emit so a new inbound text can raise a name-only notification."""

    def _emit(event: dict) -> None:
        inner(event)
        if event.get("event") == "status":
            log_state(str(event.get("status", {}).get("state") or ""))
        if event.get("event") == "message" and event.get("notify"):
            message = event.get("message") or {}
            title = notification_title(str(message.get("label") or ""))
            body = ""
            if store.settings()["notifyBody"]:
                body = str(message.get("body") or "")
            spawn(notify(title, body))

    return _emit


async def amain_wrapped() -> None:
    store = Store()
    session = None
    detail = ""
    emit_fn = notifying_emit(store, emit)
    try:
        from bluetooth import BluetoothSession

        session = BluetoothSession(store, emit_fn)
        await session.start()
    except Exception as exc:
        detail = public_error(exc)
        print("bluetooth unavailable", file=sys.stderr, flush=True)
        session = None
    if session is None:
        ready = status_from_store(store, False, detail or "Bluetooth messaging is not available yet.")
        emit({"event": "ready", "status": ready})
        log_state(ready["state"])
    else:
        session.push_status()
        log_state(session.state)
    emit({"event": "threads", "threads": store.threads()})

    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    await loop.connect_read_pipe(lambda: protocol, sys.stdin)
    # Pairing waits until the overlay answers. That answer has to be read
    # while pair() is still running, so commands are scheduled as tasks
    # and confirm/cancel are applied immediately.
    while True:
        line = await reader.readline()
        if not line:
            break
        text = line.decode("utf-8", errors="replace").strip()
        if not text:
            continue
        if _is_immediate(text, session):
            continue
        spawn(handle(text, store, session))
    store.close()


def _is_immediate(text: str, session) -> bool:
    """Apply pairing answers without waiting for the pairing task to finish."""
    try:
        command = json.loads(text)
    except json.JSONDecodeError:
        return False
    if not isinstance(command, dict):
        return False
    name = str(command.get("cmd") or "")
    if name not in ("confirm", "cancel"):
        return False
    if session is None:
        emit({"event": "error", "message": "Bluetooth is not running."})
        return True
    session.confirm(name == "confirm" and bool(command.get("accept")))
    return True


def main() -> None:
    try:
        asyncio.run(amain_wrapped())
    except KeyboardInterrupt:
        return


if __name__ == "__main__":
    main()
