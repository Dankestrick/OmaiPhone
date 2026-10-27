"""Only the saved iPhone gets calls, contacts and texts; another phone is ignored."""

import contextlib
import io
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
                mock.patch("omaiphone.daemon.audio.stay_disconnected", return_value=False), \
                mock.patch("omaiphone.daemon.GLib.timeout_add_seconds"):
            daemon.on_event({"type": "phone-connected", "path": "/ag0", "address": MINE["address"]})
            daemon.on_event({"type": "call-added", "path": "/ag0/call1", "state": "incoming", "number": "+15551234567"})
        self.assertEqual(daemon.addresses, {"/ag0": MINE["address"]})
        daemon._adopt.assert_called_once()

    def test_surprise_link_is_dropped_while_disconnect_holds(self):
        daemon = self.make()
        dropped = []
        with mock.patch.object(bluez, "is_pinned", lambda address, bus=None: True), \
                mock.patch("omaiphone.daemon.audio.stay_disconnected", return_value=True), \
                mock.patch.object(daemon, "_drop_unwanted_link", side_effect=lambda address: dropped.append(address)):
            daemon.on_event({"type": "phone-connected", "path": "/ag0", "address": MINE["address"]})
        self.assertEqual(dropped, [MINE["address"]])
        self.assertEqual(daemon.addresses, {})
        daemon._sync_contacts_if_stale.assert_not_called()
        daemon._calls_stay_on_phone.assert_not_called()
        daemon._adopt.assert_not_called()

    def test_drop_does_nothing_once_connect_clears_the_hold(self):
        daemon = self.make()
        with mock.patch("omaiphone.daemon.audio.stay_disconnected", return_value=False), \
                mock.patch.object(bluez, "disconnect") as disconnect, \
                mock.patch.object(bluez, "system_bus") as bus, \
                mock.patch.object(bluez, "find_phone") as find:
            daemon._drop_unwanted_link(MINE["address"])
        disconnect.assert_not_called()
        bus.assert_not_called()
        find.assert_not_called()

    def test_drop_disconnects_without_another_trust_change(self):
        daemon = self.make()
        device = dict(MINE)
        with mock.patch("omaiphone.daemon.audio.stay_disconnected", return_value=True), \
                mock.patch.object(bluez, "system_bus", return_value="bus"), \
                mock.patch.object(bluez, "find_phone", return_value=device) as find, \
                mock.patch.object(bluez, "disconnect") as disconnect, \
                mock.patch.object(bluez, "set_property") as set_property:
            daemon._drop_unwanted_link(MINE["address"])
        find.assert_called_once_with("bus", MINE["address"])
        disconnect.assert_called_once_with("bus", device)
        set_property.assert_not_called()


class StayDisconnectedTests(unittest.TestCase):
    def test_flag_roundtrip_leaves_other_settings(self):
        import tempfile
        from omaiphone import audio
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.json")
            audio.save_config({"phoneAddress": "AA:AA:AA:AA:AA:AA", "ringtone": False}, path)
            self.assertFalse(audio.stay_disconnected(path))
            audio.set_stay_disconnected(False, path)
            self.assertEqual(audio.load_config(path)["phoneAddress"], "AA:AA:AA:AA:AA:AA")
            audio.set_stay_disconnected(True, path)
            saved = audio.load_config(path)
            self.assertTrue(saved["stayDisconnected"])
            self.assertEqual(saved["phoneAddress"], "AA:AA:AA:AA:AA:AA")
            self.assertFalse(saved["ringtone"])
            audio.set_stay_disconnected(True, path)
            audio.set_stay_disconnected(False, path)
            self.assertFalse(audio.stay_disconnected(path))

    def _device(self):
        return {"path": "/org/bluez/hci0/dev_AA", "name": "My iPhone", "address": MINE["address"], "hfp": True}

    def test_disconnect_holds_and_untrusts_before_the_radio_drops(self):
        from types import SimpleNamespace
        from omaiphone import cli
        order = []
        device = self._device()

        def held(value):
            order.append(("held", value))

        def prop(_bus, _path, _iface, name, value):
            order.append(("prop", name, value.get_boolean()))

        def disconnect(_bus, dev):
            order.append(("disconnect", dev["name"]))

        with mock.patch.object(cli.bluez, "system_bus", return_value="bus"), \
                mock.patch.object(cli.bluez, "find_phone", return_value=device), \
                mock.patch.object(cli.audio, "set_stay_disconnected", side_effect=held), \
                mock.patch.object(cli.bluez, "set_property", side_effect=prop), \
                mock.patch.object(cli.bluez, "disconnect", side_effect=disconnect), \
                contextlib.redirect_stdout(io.StringIO()):
            cli.cmd_disconnect(SimpleNamespace(address=None))
        self.assertEqual(order, [
            ("held", True),
            ("prop", "Trusted", False),
            ("disconnect", "My iPhone"),
        ])

    def test_disconnect_still_drops_when_untrust_fails(self):
        from types import SimpleNamespace
        from omaiphone import cli
        order = []
        with mock.patch.object(cli.bluez, "system_bus", return_value="bus"), \
                mock.patch.object(cli.bluez, "find_phone", return_value=self._device()), \
                mock.patch.object(cli.audio, "set_stay_disconnected", side_effect=lambda value: order.append(("held", value))), \
                mock.patch.object(cli.bluez, "set_property", side_effect=bluez.BluetoothError("no")), \
                mock.patch.object(cli.bluez, "disconnect", side_effect=lambda *_: order.append("disconnect")), \
                contextlib.redirect_stdout(io.StringIO()):
            cli.cmd_disconnect(SimpleNamespace(address=None))
        self.assertEqual(order, [("held", True), "disconnect"])

    def test_connect_clears_the_hold_and_trusts_before_connecting(self):
        from types import SimpleNamespace
        from omaiphone import cli
        order = []
        device = self._device()

        class Tel:
            def gateway(self, address):
                order.append(("gateway", address))
                return ("/ag", {})

        def prop(_bus, _path, _iface, name, value):
            order.append(("prop", name, value.get_boolean()))

        with mock.patch.object(cli.bluez, "system_bus", return_value="bus"), \
                mock.patch.object(cli.bluez, "find_phone", return_value=device), \
                mock.patch.object(cli.audio, "set_stay_disconnected", side_effect=lambda value: order.append(("held", value))), \
                mock.patch.object(cli.bluez, "set_property", side_effect=prop), \
                mock.patch.object(cli.bluez, "connect", side_effect=lambda *_: order.append("connect")), \
                mock.patch.object(cli, "Telephony", return_value=Tel()), \
                contextlib.redirect_stdout(io.StringIO()):
            cli.cmd_connect(SimpleNamespace(address=None))
        self.assertEqual(order, [
            ("held", False),
            ("prop", "Trusted", True),
            "connect",
            ("gateway", MINE["address"]),
        ])


class PrivateFolderTests(unittest.TestCase):
    def test_old_open_folder_is_closed_on_write(self):
        import tempfile
        from omaiphone import audio
        with tempfile.TemporaryDirectory() as tmp:
            folder = os.path.join(tmp, "state")
            os.mkdir(folder, 0o755)
            os.chmod(folder, 0o755)
            audio.write_private(os.path.join(folder, "last-number"), "+15551234567")
            self.assertEqual(os.stat(folder).st_mode & 0o777, 0o700)
            self.assertEqual(os.stat(os.path.join(folder, "last-number")).st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
