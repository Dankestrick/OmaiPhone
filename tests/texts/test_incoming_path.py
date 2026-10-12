"""A message handle from the phone must not choose a path outside the runtime dir."""

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend" / "texts"))

import bluetooth


class IncomingPathTests(unittest.TestCase):
    def test_only_a_hex_handle_stays_inside_the_directory(self):
        root = Path("/run/user/1000/omaiphone")
        safe = bluetooth.incoming_bmessage_path(root, "00000000000000AB")
        self.assertEqual(safe, root / "incoming-00000000000000AB.bmsg")
        for handle in (
            "../tmp/pwn",
            "..",
            "/etc/cron.d/pwn",
            "incoming-../../../../../../tmp/pwn",
            "A" * 17,
            "msg 1",
            "0000/0001",
            "",
            "....//",
        ):
            self.assertIsNone(bluetooth.incoming_bmessage_path(root, handle), handle)

    def test_oversized_download_is_not_parsed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "incoming-1.bmsg"
            path.write_bytes(b"BEGIN:MSG\n" + b"A" * (bluetooth._MAX_BMESSAGE_BYTES + 8))
            self.assertEqual(bluetooth.read_bounded_text(path), "")
            path.write_bytes(b"BEGIN:MSG\r\nhi\r\nEND:MSG\r\n")
            self.assertIn("BEGIN:MSG", bluetooth.read_bounded_text(path))

    def test_prepared_file_is_private_and_not_a_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = bluetooth.incoming_bmessage_path(root, "abc")
            outside = root / "secret"
            outside.write_text("nope")
            link = root / "incoming-abc.bmsg"
            link.symlink_to(outside)
            with self.assertRaises(OSError):
                bluetooth.prepare_download_file(target)
            self.assertEqual(outside.read_text(), "nope")
            link.unlink()
            bluetooth.prepare_download_file(target)
            self.assertEqual(os.stat(target).st_mode & 0o777, 0o600)

    def test_failure_map_does_not_grow_without_bound(self):
        session = bluetooth.BluetoothSession.__new__(bluetooth.BluetoothSession)
        session._download_failures = {}
        session._message_paths = {}
        for i in range(bluetooth._MAX_TRACKED_HANDLES + 40):
            session._note_download_failure(f"{i:04x}")
            session._remember_handle(f"{i:04x}", f"/message{i}")
        self.assertLessEqual(len(session._download_failures), bluetooth._MAX_TRACKED_HANDLES)
        self.assertLessEqual(len(session._message_paths), bluetooth._MAX_TRACKED_HANDLES)


if __name__ == "__main__":
    unittest.main()
