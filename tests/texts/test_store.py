"""Tests for the local database. They use a temporary directory, not your real texts."""

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend" / "texts"))

import store


class StoreTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.chmod(self._tmp.name, 0o700)
        self.db = store.Store(Path(self._tmp.name) / "messages.db")

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    def test_messages_and_bodies_are_capped(self):
        self.db.set_own_number("+15550001111")
        for i in range(store.MESSAGES_MAX + 5):
            created, _ = self.db.upsert_message(
                external_id=f"map-{i}",
                address="+15552223333",
                body="x" * (store.BODY_MAX + 10),
                direction="in",
                timestamp=f"2026-10-02T10:{i // 60:02d}:{i % 60:02d}",
                read=True,
                has_attachment=False,
                body_complete=True,
            )
        messages = self.db.messages_for(created["thread"])
        self.assertEqual(len(messages), store.MESSAGES_MAX)
        self.assertEqual(messages[-1]["id"], created["id"])  # newest kept, oldest first
        self.assertTrue(all(len(m["body"]) <= store.BODY_MAX for m in messages))

    def test_a_huge_id_is_shortened_from_a_prefix(self):
        huge = "m" * (store._HASH_PREFIX + 100000)
        first = store._bound_id(huge)
        self.assertEqual(len(first), len("id-") + 32)
        self.assertNotEqual(first, store._bound_id(huge + "more"))
        self.assertEqual(store._bound_id("map-1"), "map-1")
        self.assertEqual(store._bound_text("a\x00b" + "c" * 10000, 4), "abcc")
        self.assertEqual(store._bound_text({"nope": "x"}, 10), "")

    def test_phone_fields_are_bounded_in_the_database(self):
        self.db.set_own_number("+15550001111")
        self.db.upsert_message(
            external_id="h" * 500,
            address="+" + "1" * 400,
            body="y" * (store.BODY_MAX + 50),
            direction="sideways",
            timestamp="t" * 80,
            read=False,
            has_attachment=False,
            body_complete=True,
            peer_label="n" * 500,
        )
        row = self.db._db.execute(
            "SELECT external_id, body, peer_label, direction, timestamp, thread_key FROM messages"
        ).fetchone()
        self.assertLessEqual(len(row["external_id"]), store.ID_MAX)
        self.assertLessEqual(len(row["body"]), store.BODY_MAX)
        self.assertLessEqual(len(row["peer_label"]), store.LABEL_MAX)
        self.assertLessEqual(len(row["timestamp"]), store.STAMP_MAX)
        self.assertEqual(row["direction"], "in")
        self.assertNotIn("..", row["thread_key"])
        people = [
            {"name": "n" * 500, "address": f"+1555{i:07d}", "kind": "tel"}
            for i in range(store.CONTACTS_MAX + 10)
        ]
        self.db.replace_contacts(people)
        self.assertEqual(self.db.contact_count(), store.CONTACTS_MAX)
        contact = self.db._db.execute("SELECT name, address FROM contacts LIMIT 1").fetchone()
        self.assertLessEqual(len(contact["name"]), store.LABEL_MAX)
        self.assertLessEqual(len(contact["address"]), store.ADDRESS_MAX)

    def test_old_messages_are_dropped_once_the_table_is_full(self):
        self.db.set_own_number("+15550001111")
        for i in range(store.ROW_MAX + 5):
            self.db.upsert_message(
                external_id=f"map-{i}",
                address="+15552223333",
                body="hi",
                direction="in",
                timestamp=f"2026-01-01T00:{i // 60:02d}:{i % 60:02d}",
                read=True,
                has_attachment=False,
                body_complete=True,
            )
        count = self.db._db.execute("SELECT COUNT(*) AS n FROM messages").fetchone()["n"]
        self.assertEqual(count, store.ROW_MAX)
        newest = self.db.record(f"map-{store.ROW_MAX + 4}")
        oldest = self.db.record("map-0")
        self.assertIsNotNone(newest)
        self.assertIsNone(oldest)

    def test_database_file_is_private(self):
        mode = os.stat(self.db.path).st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_threads_group_and_present_attachments(self):
        self.db.set_own_number("+15550001111")
        created, is_new = self.db.upsert_message(
            external_id="map-1",
            address="+1 (555) 222-3333",
            body="hello",
            direction="in",
            timestamp="2026-10-02T10:00:00",
            read=False,
            has_attachment=True,
            body_complete=True,
            peer_label="Ada",
        )
        self.assertTrue(is_new)
        self.assertIn("attachment omitted", created["body"])
        self.assertNotIn("BEGIN:", created["body"])

        again, is_new = self.db.upsert_message(
            external_id="map-1",
            address="+15552223333",
            body="hello",
            direction="in",
            timestamp="2026-10-02T10:00:00",
            read=False,
            has_attachment=True,
            body_complete=True,
            peer_label="Ada",
        )
        self.assertFalse(is_new)
        self.assertEqual(again["id"], created["id"])

        self.db.upsert_message(
            external_id="local-1",
            address="+15552223333",
            body="reply",
            direction="out",
            timestamp="2026-10-02T10:05:00",
            read=True,
            has_attachment=False,
            body_complete=True,
        )
        threads = self.db.threads()
        self.assertEqual(len(threads), 1)
        self.assertEqual(threads[0]["unread"], 1)
        self.assertEqual(threads[0]["label"], "Ada")
        self.db.mark_thread_read(threads[0]["key"])
        self.assertEqual(self.db.unread_count(), 0)

    def test_unread_conversations_sort_first(self):
        self.db.upsert_message(
            external_id="old-unread",
            address="+15551110000",
            body="older",
            direction="in",
            timestamp="2026-10-01T09:00:00",
            read=False,
            has_attachment=False,
            body_complete=True,
            peer_label="Older",
        )
        self.db.upsert_message(
            external_id="new-read",
            address="+15552220000",
            body="newer",
            direction="in",
            timestamp="2026-10-02T09:00:00",
            read=True,
            has_attachment=False,
            body_complete=True,
            peer_label="Newer",
        )
        labels = [row["label"] for row in self.db.threads()]
        self.assertEqual(labels, ["Older", "Newer"])

    def test_contact_search(self):
        self.db.replace_contacts(
            [
                {"name": "Ada Lovelace", "address": "+15551234567", "kind": "tel"},
                {"name": "Grace", "address": "grace@example.com", "kind": "email"},
            ]
        )
        found = self.db.search_contacts("ada")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["address"], "+15551234567")

    def test_a_conversation_is_someone_you_can_text(self):
        self.db.upsert_message(
            external_id="map-9",
            address="+15557654321",
            body="hi",
            direction="in",
            timestamp="2026-10-02T11:00:00",
            read=True,
            has_attachment=False,
            body_complete=True,
            peer_label="Sam",
        )
        found = self.db.search_contacts("sam")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["name"], "Sam")
        self.assertEqual(found[0]["address"], "+15557654321")

    def test_contact_name_replaces_a_bare_number(self):
        self.db.upsert_message(
            external_id="map-8",
            address="+15557654321",
            body="hi",
            direction="in",
            timestamp="2026-10-02T11:00:00",
            read=True,
            has_attachment=False,
            body_complete=True,
            peer_label="+15557654321",
        )
        self.db.replace_contacts(
            [{"name": "Sam", "address": "+15557654321", "kind": "tel"}]
        )
        self.assertEqual(self.db.apply_contact_names(), 1)
        self.assertEqual(self.db.threads()[0]["label"], "Sam")


if __name__ == "__main__":
    unittest.main()
