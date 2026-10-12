"""Pairing only accepts the phone being paired, with a code, and never saves a stranger's phone."""

import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend" / "texts"))

import bluetooth
from dbus_next.errors import DBusError

PHONE = "/org/bluez/hci0/dev_AA_AA_AA_AA_AA_AA"
STRANGER = "/org/bluez/hci0/dev_BB_BB_BB_BB_BB_BB"


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.codes = []
        self.agent = bluetooth.PairAgent(self.codes.append)
        self.agent.expect(PHONE)

    def test_just_works_is_rejected(self):
        with self.assertRaises(DBusError):
            asyncio.run(self.agent.RequestAuthorization.__wrapped__(self.agent, PHONE))

    def test_code_from_another_device_is_rejected(self):
        with self.assertRaises(DBusError):
            asyncio.run(self.agent.RequestConfirmation.__wrapped__(self.agent, STRANGER, 123456))
        self.assertEqual(self.codes, [])

    def test_services_only_for_the_phone_being_paired(self):
        self.agent.AuthorizeService.__wrapped__(self.agent, PHONE, "uuid")
        with self.assertRaises(DBusError):
            self.agent.AuthorizeService.__wrapped__(self.agent, STRANGER, "uuid")
        self.agent.expect(None)
        with self.assertRaises(DBusError):
            self.agent.AuthorizeService.__wrapped__(self.agent, PHONE, "uuid")


class FakeStore:
    def __init__(self, address=""):
        self.address = address

    def settings(self):
        return {"phoneAddress": self.address}

    def set_phone(self, address, name):
        self.address = address


class AdoptTests(unittest.TestCase):
    def adopt(self, devices, saved=""):
        store = FakeStore(saved)
        session = bluetooth.BluetoothSession(store, lambda event: None)

        async def phones():
            return devices
        session._phone_dicts = phones
        found = asyncio.run(session._adopt_paired_iphone())
        return found, store.address

    def test_unpaired_phone_nearby_is_not_saved(self):
        found, saved = self.adopt([{"address": "BB", "name": "iPhone", "paired": False, "connected": False}])
        self.assertTrue(found)
        self.assertEqual(saved, "")

    def test_paired_phone_is_saved(self):
        found, saved = self.adopt([
            {"address": "BB", "name": "iPhone", "paired": False, "connected": True},
            {"address": "AA", "name": "My iPhone", "paired": True, "connected": False},
        ])
        self.assertEqual(saved, "AA")


if __name__ == "__main__":
    unittest.main()
