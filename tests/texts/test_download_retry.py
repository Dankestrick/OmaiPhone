"""A message the phone will not hand over is tried a few times, then left alone."""

import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend" / "texts"))

import bluetooth


class FakeStore:
    def record(self, handle):
        return None

    def incomplete_in_thread(self, thread):
        return ["h1"]


class DownloadRetryTests(unittest.TestCase):
    def setUp(self):
        self.session = bluetooth.BluetoothSession(FakeStore(), lambda event: None)
        self.session._map_path = "/org/bluez/obex/client/session0"
        self.session._message_paths["h1"] = "/org/bluez/obex/client/session0/message1"
        self.attempts = 0

    def _fetch(self, result):
        async def fetch(handle, path, announce, sent):
            self.attempts += 1
            return result
        self.session._fetch_body = fetch

    def test_gives_up_after_max_tries(self):
        self._fetch(False)
        for _ in range(10):
            asyncio.run(self.session.fetch_thread("tel:+15551234567"))
        self.assertEqual(self.attempts, bluetooth._MAX_DOWNLOAD_TRIES)
        self.assertTrue(self.session._gave_up("h1"))

    def test_success_clears_failures(self):
        self._fetch(False)
        asyncio.run(self.session._download_body("h1"))
        self._fetch(True)
        asyncio.run(self.session._download_body("h1"))
        self.assertNotIn("h1", self.session._download_failures)


if __name__ == "__main__":
    unittest.main()
