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
