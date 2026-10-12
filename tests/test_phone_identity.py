"""Only the saved iPhone gets calls, contacts and texts; another phone is ignored."""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from omaiphone import bluez  # noqa: E402

MINE = {"path": "/org/bluez/hci0/dev_AA", "address": "AA:AA:AA:AA:AA:AA", "name": "My iPhone",
        "paired": True, "trusted": True, "connected": True, "phone": True, "hfp": True, "battery": None}
OTHER = dict(MINE, path="/org/bluez/hci0/dev_BB", address="BB:BB:BB:BB:BB:BB", name="iPhone")


class PinnedPhoneTests(unittest.TestCase):
    def setUp(self):
        self.config = {}
        patches = [
            mock.patch.object(bluez.audio, "load_config", lambda *a, **k: dict(self.config)),
            mock.patch.object(bluez.audio, "save_config", lambda config, *a, **k: self.config.update(config)),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def phones(self, *found):
        p = mock.patch.object(bluez, "devices", lambda bus, phones_only=False: list(found))
        p.start()
        self.addCleanup(p.stop)

    def test_only_paired_phone_is_saved_on_first_use(self):
        self.phones(MINE)
        self.assertEqual(bluez.find_phone(None)["address"], MINE["address"])
        self.assertEqual(self.config["phoneAddress"], MINE["address"])

    def test_two_phones_are_never_guessed(self):
        self.phones(OTHER, MINE)
        with self.assertRaises(bluez.BluetoothError):
            bluez.find_phone(None)
        self.assertNotIn("phoneAddress", self.config)

    def test_saved_phone_wins_over_other_phones(self):
        self.config["phoneAddress"] = MINE["address"]
        self.phones(OTHER, MINE)
        self.assertEqual(bluez.find_phone(None)["address"], MINE["address"])

    def test_saved_phone_gone_is_an_error_not_a_fallback(self):
        self.config["phoneAddress"] = MINE["address"]
        self.phones(OTHER)
        with self.assertRaises(bluez.BluetoothError):
            bluez.find_phone(None)

    def test_is_pinned(self):
        self.config["phoneAddress"] = MINE["address"]
        self.phones(OTHER, MINE)
        self.assertTrue(bluez.is_pinned(MINE["address"].lower(), bus=object()))
        self.assertFalse(bluez.is_pinned(OTHER["address"], bus=object()))
        self.assertFalse(bluez.is_pinned("", bus=object()))


class PairingAgentTests(unittest.TestCase):
    def test_just_works_pairing_is_rejected(self):
        agent = bluez.PairingAgent(None, confirm=lambda *_: True)
        invocation = mock.Mock()
        agent._handle(None, None, None, None, "RequestAuthorization",
                      mock.Mock(unpack=lambda: (MINE["path"],)), invocation)
        invocation.return_dbus_error.assert_called_once()
        invocation.return_value.assert_not_called()


class DaemonPhoneFilterTests(unittest.TestCase):
    def make(self):
        from omaiphone.daemon import Daemon
        daemon = Daemon.__new__(Daemon)
        daemon.addresses = {}
        daemon.calls = {}
        daemon._calls_stay_on_phone = mock.Mock()
        daemon._sync_contacts_if_stale = mock.Mock()
        daemon._sync_history_later = mock.Mock()
        daemon._adopt = mock.Mock()
        daemon._update_ringer = mock.Mock()
        daemon._sync_audio = mock.Mock()
        return daemon

    def test_other_phone_and_its_calls_are_ignored(self):
        daemon = self.make()
        with mock.patch.object(bluez, "is_pinned", lambda address, bus=None: address == MINE["address"]):
            daemon.on_event({"type": "phone-connected", "path": "/ag1", "address": OTHER["address"]})
            daemon.on_event({"type": "call-added", "path": "/ag1/call1", "state": "incoming", "number": "+15551234567"})
        self.assertEqual(daemon.addresses, {})
        daemon._adopt.assert_not_called()

    def test_saved_phone_calls_are_handled(self):
        daemon = self.make()
        with mock.patch.object(bluez, "is_pinned", lambda address, bus=None: address == MINE["address"]), \
                mock.patch("omaiphone.daemon.GLib.timeout_add_seconds"):
            daemon.on_event({"type": "phone-connected", "path": "/ag0", "address": MINE["address"]})
            daemon.on_event({"type": "call-added", "path": "/ag0/call1", "state": "incoming", "number": "+15551234567"})
        self.assertEqual(daemon.addresses, {"/ag0": MINE["address"]})
        daemon._adopt.assert_called_once()


if __name__ == "__main__":
    unittest.main()
