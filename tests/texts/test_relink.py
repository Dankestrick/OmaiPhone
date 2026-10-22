"""A message session obexd closes is noticed, retried, and does not eat a send."""

import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend" / "texts"))

import bluetooth

SESSION = "/org/bluez/obex/client/session0"
NEW_SESSION = "/org/bluez/obex/client/session1"


class FakeStore:
    def settings(self):
        return {
            "phoneAddress": "74:D5:E8:00:00:01",
            "phoneName": "iPhone",
            "ownNumber": "",
            "notifyBody": False,
        }

    def unread_count(self):
        return 0


class RelinkTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.session = bluetooth.BluetoothSession(FakeStore(), self.events.append)
        self.session._session_bus = object()
        self.session._map_path = SESSION
        self.session.state = "connected"
        self.live = {SESSION: {}}
        self.reconnects = 0

        async def managed(service, path, bus):
            return dict(self.live)

        async def reconnect():
            self.reconnects += 1
            self.live = {NEW_SESSION: {}}
            self.session._map_path = NEW_SESSION
            self.session.state = "connected"

        self.session._managed = managed
        self.session._safe_reconnect = reconnect

    def _drop(self):
        self.live = {}

    def test_live_session_is_left_alone(self):
        asyncio.run(self.session._collect_live_messages(False))
        self.assertEqual(self.session._map_path, SESSION)
        self.assertEqual(self.session.state, "connected")

    def test_closed_session_is_noticed_and_relinked(self):
        self._drop()
        delays = bluetooth._RELINK_DELAYS
        bluetooth._RELINK_DELAYS = (0,)
        try:
            async def run():
                await self.session._collect_live_messages(False)
                self.assertEqual(self.session._map_path, "")
                self.assertEqual(self.session.state, "phone_nearby")
                await asyncio.gather(*self.session._tasks)

            asyncio.run(run())
        finally:
            bluetooth._RELINK_DELAYS = delays
        self.assertEqual(self.reconnects, 1)
        self.assertEqual(self.session._map_path, NEW_SESSION)
        self.assertFalse(self.session._relinking)

    def test_send_on_dead_session_relinks_and_sends_once(self):
        sent = []

        async def push(address, body):
            if self.session._map_path == SESSION:
                raise RuntimeError("Method \"PushMessage\" doesn't exist")
            sent.append((self.session._map_path, body))

        self.session._push_text = push
        self._drop()
        asyncio.run(self.session.send("+15551234567", "hi"))
        self.assertEqual(sent, [(NEW_SESSION, "hi")])
        self.assertEqual(self.reconnects, 1)

    def test_send_refused_on_live_session_is_not_retried(self):
        sent = []

        async def push(address, body):
            sent.append(body)
            raise RuntimeError("The phone refused the message.")

        self.session._push_text = push
        with self.assertRaises(RuntimeError):
            asyncio.run(self.session.send("+15551234567", "hi"))
        self.assertEqual(sent, ["hi"])
        self.assertEqual(self.reconnects, 0)

    def test_send_without_session_reconnects_first(self):
        sent = []

        async def push(address, body):
            sent.append(self.session._map_path)

        self.session._push_text = push
        self.session._map_path = ""
        asyncio.run(self.session.send("+15551234567", "hi"))
        self.assertEqual(sent, [NEW_SESSION])

    def test_lost_session_is_removed_so_the_phone_frees_it(self):
        removed = []

        async def remove(path):
            removed.append(path)

        self.session._remove_session = remove
        self._drop()
        delays = bluetooth._RELINK_DELAYS
        bluetooth._RELINK_DELAYS = (0,)
        try:
            async def run():
                await self.session._collect_live_messages(False)
                await asyncio.gather(*self.session._tasks)

            asyncio.run(run())
        finally:
            bluetooth._RELINK_DELAYS = delays
        self.assertEqual(removed, [SESSION])

    def test_relink_waits_out_a_scan_instead_of_giving_up(self):
        self._drop()
        delays = bluetooth._RELINK_DELAYS
        bluetooth._RELINK_DELAYS = (0,)
        try:
            async def run():
                await self.session._collect_live_messages(False)
                self.session.state = "scanning"
                await asyncio.sleep(0.01)
                self.assertEqual(self.reconnects, 0)
                self.session.state = "not_paired"
                await asyncio.gather(*self.session._tasks)

            asyncio.run(run())
        finally:
            bluetooth._RELINK_DELAYS = delays
        self.assertEqual(self.reconnects, 1)
        self.assertEqual(self.session._map_path, NEW_SESSION)


class BoundedBusTests(unittest.TestCase):
    def test_a_call_with_no_reply_gives_up(self):
        class Msg:
            member = "ListMessages"

        async def never(self, msg):
            await asyncio.Event().wait()

        original = bluetooth.MessageBus.call
        timeout = bluetooth._DBUS_TIMEOUT
        bluetooth.MessageBus.call = never
        bluetooth._DBUS_TIMEOUT = 0.01
        try:
            bus = bluetooth._BoundedBus.__new__(bluetooth._BoundedBus)
            with self.assertRaisesRegex(RuntimeError, "did not answer in time"):
                asyncio.run(bus.call(Msg()))
        finally:
            bluetooth.MessageBus.call = original
            bluetooth._DBUS_TIMEOUT = timeout

    def test_pairing_waits_longer_than_the_phone_prompt(self):
        self.assertGreater(bluetooth._DBUS_SLOW_TIMEOUTS["Pair"], 90)


if __name__ == "__main__":
    unittest.main()
