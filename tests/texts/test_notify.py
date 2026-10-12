"""Phone text must not become notification markup, and a pop-up click is not a free pass."""

import asyncio
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend" / "texts"))

import messagesd
import protocol
from dbus_next import Message
from dbus_next.constants import MessageType


def _return(sender, signature="", body=None):
    return Message(
        message_type=MessageType.METHOD_RETURN,
        reply_serial=1,
        sender=sender,
        signature=signature,
        body=list(body or []),
    )


def _signal(member, signature, body, sender, interface=None, path=None):
    return Message(
        message_type=MessageType.SIGNAL,
        path=path or "/org/freedesktop/Notifications",
        interface=interface or "org.freedesktop.Notifications",
        member=member,
        sender=sender,
        signature=signature,
        body=body,
    )


class FakeBus:
    """Just enough of a session bus for Notify and the two signals."""

    def __init__(self, owner=":1.9", fail_notify=False, fail_owner=False):
        self.owner = owner
        self.fail_notify = fail_notify
        self.fail_owner = fail_owner
        self.sent = []
        self.next_id = 1
        self.handler = None

    def add_message_handler(self, handler):
        self.handler = handler

    async def call(self, msg):
        self.sent.append(msg)
        if msg.member == "AddMatch":
            return _return("org.freedesktop.DBus")
        if msg.member == "GetNameOwner":
            if self.fail_owner:
                raise RuntimeError("no owner")
            return _return("org.freedesktop.DBus", "s", [self.owner])
        if msg.member == "Notify":
            if self.fail_notify:
                raise RuntimeError("notify failed")
            nid = self.next_id
            self.next_id += 1
            return _return(self.owner, "u", [nid])
        raise AssertionError(msg.member)


class HoldingBus(FakeBus):
    """Notify calls wait until release, so the in-flight cap can be seen."""

    def __init__(self):
        super().__init__()
        self.notify_calls = 0
        self.release = asyncio.Event()
        self.two_started = asyncio.Event()

    async def call(self, msg):
        if msg.member != "Notify":
            return await super().call(msg)
        self.notify_calls += 1
        if self.notify_calls >= 2:
            self.two_started.set()
        await self.release.wait()
        nid = self.notify_calls
        return _return(self.owner, "u", [nid])


class NotifyArgTests(unittest.TestCase):
    def test_title_stays_plain_text(self):
        title, body = messagesd.notification_content("--urgency=critical", "hello")
        self.assertEqual(title, "--urgency=critical")
        self.assertEqual(body, "hello")
        title, _body = messagesd.notification_content("Tom & Jerry", "")
        self.assertEqual(title, "Tom & Jerry")

    def test_markup_is_escaped(self):
        title, body = messagesd.notification_content("Sam <3>", '<img src="http://evil/x">')
        self.assertEqual(title, "Sam <3>")  # titles are plain text in Omarchy
        self.assertNotIn("<", body)
        self.assertIn("&lt;img", body)
        self.assertNotIn("http://evil", body.split("&")[0])

    def test_null_and_length_are_bounded(self):
        title, body = messagesd.notification_content("A\x00B" + "C" * 200, "x" * 500)
        self.assertNotIn("\x00", title)
        self.assertNotIn("\x00", body)
        self.assertLessEqual(len(title), 80)
        self.assertEqual(len(body), 120)

    def test_notify_call_is_a_dbus_message(self):
        msg = messagesd.notification_message("Sam <3>", '<img src="http://evil/x">')
        self.assertEqual(msg.member, "Notify")
        self.assertEqual(msg.body[3], "Sam <3>")
        self.assertIn("&lt;img", msg.body[4])
        self.assertEqual(msg.body[5], ["default", "Open"])
        msg._marshall()


class PublicErrorTests(unittest.TestCase):
    def test_message_text_and_numbers_are_not_shown(self):
        self.assertEqual(
            protocol.public_error(RuntimeError("BEGIN:MSG\nhidden\nEND:MSG")),
            "Bluetooth error (RuntimeError).",
        )
        self.assertEqual(
            protocol.public_error(RuntimeError("from +15551234567")),
            "Bluetooth error (RuntimeError).",
        )
        self.assertEqual(
            protocol.public_error(RuntimeError("adapter is off")),
            "adapter is off",
        )


class NewTextPopupTests(unittest.TestCase):
    def _emit(self, notify_body, message):
        class Store:
            def settings(self):
                return {"notifyBody": notify_body}

        class Popups:
            def __init__(self):
                self.calls = []

            def notify(self, title, body, thread="", address="", label=""):
                self.calls.append((title, body, thread, address, label))

                async def _done():
                    return None

                return _done()

        popups = Popups()
        with mock.patch.object(messagesd, "spawn", lambda coro: coro.close()):
            emit = messagesd.notifying_emit(Store(), lambda event: None, popups)
            emit({"event": "message", "notify": True, "message": message})
        return popups.calls

    def test_a_new_text_starts_a_popup(self):
        # Regression: a helper in messagesd once shadowed protocol.notification_title,
        # so every new text raised before its pop-up was made.
        calls = self._emit(
            True,
            {"label": "Ada Lovelace", "body": "hi", "thread": "tel:+15551234567"},
        )
        self.assertEqual(len(calls), 1)
        title, body, thread, address, label = calls[0]
        self.assertEqual(title, "Ada Lovelace")
        self.assertEqual(body, "hi")
        self.assertEqual(thread, "tel:+15551234567")
        self.assertEqual(address, "+15551234567")
        self.assertEqual(label, "Ada Lovelace")

    def test_body_stays_off_the_popup_when_the_setting_is_off(self):
        calls = self._emit(
            False,
            {"label": "Ada Lovelace", "body": "secret", "thread": "tel:+15551234567"},
        )
        self.assertEqual(calls[0][1], "")

    def test_a_number_is_not_used_as_the_popup_title(self):
        calls = self._emit(
            False,
            {"label": "+15551234567", "body": "hi", "thread": "tel:+15551234567"},
        )
        self.assertEqual(calls[0][0], "New message")
        self.assertEqual(calls[0][3], "+15551234567")
        self.assertEqual(calls[0][4], "+15551234567")


class PopupClickTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.events = []
        self.bus = FakeBus()
        self.popups = messagesd.TextPopups(self.events.append)
        await self.popups.attach(self.bus)
        self.assertEqual(self.popups.clicks.owner, ":1.9")
        self.assertIsNotNone(self.bus.handler)

    def _click(self, sender, nid, action="default"):
        self.bus.handler(_signal("ActionInvoked", "us", [nid, action], sender))

    def _close(self, sender, nid):
        self.bus.handler(_signal("NotificationClosed", "uu", [nid, 2], sender))

    async def _one(self, label="Ada Lovelace"):
        await self.popups.notify("Ada", "hi", "tel:+15551234567", "+15551234567", label)

    async def test_a_click_opens_that_thread(self):
        await self._one()
        self._click(":1.9", 1)
        self.assertEqual(
            self.events,
            [
                {
                    "event": "open-thread",
                    "thread": "tel:+15551234567",
                    "address": "+15551234567",
                    "label": "Ada Lovelace",
                }
            ],
        )

    async def test_another_sender_or_an_unknown_id_is_ignored(self):
        await self._one()
        self._click(":1.other", 1)
        self._click(":1.9", 99)
        self._click(":1.9", 1, action="Open")
        self.bus.handler(_signal("ActionInvoked", "s", ["nope"], ":1.9"))
        self.assertEqual(self.events, [])
        self._click(":1.9", 1)
        self.assertEqual(len(self.events), 1)

    async def test_a_forged_name_owner_cannot_steal_the_click(self):
        await self._one()
        self.bus.handler(
            _signal(
                "NameOwnerChanged",
                "sss",
                ["org.freedesktop.Notifications", ":1.9", ":1.evil"],
                ":1.evil",
                interface="org.freedesktop.DBus",
                path="/org/freedesktop/DBus",
            )
        )
        self._click(":1.evil", 1)
        self.assertEqual(self.events, [])
        self._click(":1.9", 1)
        self.assertEqual(len(self.events), 1)

    async def test_a_real_owner_change_drops_old_ids(self):
        await self._one()
        self.bus.handler(
            _signal(
                "NameOwnerChanged",
                "sss",
                ["org.freedesktop.Notifications", ":1.9", ":1.20"],
                "org.freedesktop.DBus",
                interface="org.freedesktop.DBus",
                path="/org/freedesktop/DBus",
            )
        )
        self._click(":1.9", 1)
        self._click(":1.20", 1)
        self.assertEqual(self.events, [])
        self.assertEqual(self.popups.clicks.tracked, 0)

    async def test_close_from_the_owner_drops_the_id(self):
        await self._one()
        self._close(":1.other", 1)
        self._click(":1.9", 1)
        self.assertEqual(len(self.events), 1)
        self.events.clear()
        self._close(":1.9", 1)
        self._click(":1.9", 1)
        self.assertEqual(self.events, [])

    async def test_the_id_map_keeps_the_last_50(self):
        for i in range(60):
            number = f"+1555{i:04d}"
            await self.popups.notify("T", "", f"tel:{number}", number, "N")
        self.assertEqual(self.popups.clicks.tracked, 50)
        self._click(":1.9", 1)
        self.assertEqual(self.events, [])
        self._click(":1.9", 60)
        self.assertEqual(self.events[0]["address"], "+15550059")

    async def test_notify_failure_shows_nothing_and_does_not_raise(self):
        self.bus.fail_notify = True
        await self.popups.notify("Ada", "<b>hi</b>", "tel:+15551234567", "+15551234567", "Ada")
        self.assertEqual(self.popups.clicks.tracked, 0)
        self._click(":1.9", 1)
        self.assertEqual(self.events, [])

    async def test_phone_text_is_not_a_command(self):
        def boom(*_args, **_kwargs):
            raise AssertionError("phone text reached a command")

        with mock.patch("asyncio.create_subprocess_exec", boom), mock.patch("subprocess.Popen", boom):
            await self.popups.notify("Sam <3>", '<img src="http://evil/x">', "tel:+1", "+1", "Sam <3>")
        msg = next(item for item in self.bus.sent if item.member == "Notify")
        self.assertEqual(msg.body[3], "Sam <3>")
        self.assertNotIn("<", msg.body[4])
        self.assertIn("&lt;img", msg.body[4])
        self.assertEqual(msg.body[5], ["default", "Open"])

    async def test_only_two_popups_are_in_flight(self):
        bus = HoldingBus()
        popups = messagesd.TextPopups(lambda _event: None)
        await popups.attach(bus)
        tasks = [
            asyncio.create_task(popups.notify("T", "b", "tel:+15551234567", "+15551234567", "T"))
            for _ in range(4)
        ]
        await asyncio.wait_for(bus.two_started.wait(), timeout=2)
        await asyncio.sleep(0)
        self.assertEqual(bus.notify_calls, 2)
        bus.release.set()
        await asyncio.gather(*tasks)
        self.assertEqual(bus.notify_calls, 2)

    async def test_notify_learns_the_owner_when_lookup_fails(self):
        bus = FakeBus(fail_owner=True)
        events = []
        popups = messagesd.TextPopups(events.append)
        await popups.attach(bus)
        self.assertEqual(popups.clicks.owner, "")
        await popups.notify("Ada", "", "tel:+15551234567", "+15551234567", "Ada")
        self.assertEqual(popups.clicks.owner, ":1.9")
        bus.handler(_signal("ActionInvoked", "us", [1, "default"], ":1.9"))
        self.assertEqual(events[0]["address"], "+15551234567")


if __name__ == "__main__":
    unittest.main()
