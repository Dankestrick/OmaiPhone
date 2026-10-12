"""Reading or answering a text clears that person's alerts, and nothing else."""

import asyncio
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "backend" / "texts"))

import messagesd  # noqa: E402
from seen import clear_conversation, conversation_identity, mirror_inbox  # noqa: E402


def note(uid, **changes):
    base = {
        "uid": uid,
        "appId": "com.apple.MobileSMS",
        "app": "Messages",
        "title": "Mom",
        "subtitle": "",
        "message": "hello",
        "canDismiss": True,
    }
    base.update(changes)
    return base


class FakeStore:
    def __init__(self):
        self.rows = [{"key": "tel:+15551234567", "label": "Mom"}]
        self.names = {"+15551234567": "Mom"}
        self.own = "+15550001111"
        self.read = None

    def threads(self):
        return self.rows

    def label_for(self, address):
        return self.names.get(address, address)

    def settings(self):
        return {"ownNumber": self.own}

    def mark_thread_read(self, key):
        self.read = key


class FakeSession:
    def __init__(self):
        self.read = None
        self.sent = None

    async def mark_read(self, thread):
        self.read = thread

    async def send(self, address, body):
        self.sent = (address, body)


class IdentityTests(unittest.TestCase):
    def test_open_thread_keeps_its_name_and_number(self):
        label, number = conversation_identity(FakeStore(), "tel:+15551234567", "")
        self.assertEqual(label, "Mom")
        self.assertEqual(number, "+15551234567")

    def test_formatted_number_finds_the_saved_name(self):
        store = FakeStore()
        store.rows = []
        label, number = conversation_identity(store, "", "(555) 123-4567")
        self.assertEqual(number, "(555) 123-4567")
        self.assertEqual(label, "Mom")

    def test_unknown_placeholder_is_not_a_name(self):
        store = FakeStore()
        store.rows = [{"key": "tel:+15551234567", "label": "Unknown"}]
        store.names = {}
        label, _number = conversation_identity(store, "tel:+15551234567", "")
        self.assertEqual(label, "")


class ClearTests(unittest.TestCase):
    def test_only_that_persons_dismissable_texts_are_cleared(self):
        dismissed = []

        def dismiss(uid):
            self.assertIs(type(uid), int)
            dismissed.append(uid)

        inbox = [
            note("4", title="Mom"),
            note(5, title="Dad"),
            note(6, title="Mom", appId="com.apple.mobilemail", app="Mail"),
            note(7, title="Mom", canDismiss=False),
            note("nope", title="Mom"),
        ]
        clear_conversation(FakeStore(), "tel:+15551234567", inbox=inbox, dismiss=dismiss)
        self.assertEqual(dismissed, [4])

    def test_a_dismiss_failure_is_quiet(self):
        def blow(_uid):
            raise RuntimeError("not connected")

        clear_conversation(
            FakeStore(),
            "tel:+15551234567",
            inbox=[note(3, title="Mom")],
            dismiss=blow,
        )

    def test_mirror_off_or_phone_away_has_nothing_to_clear(self):
        notes = [note(1, title="Mom")]
        self.assertEqual(mirror_inbox({"enabled": False, "state": "ready", "notifications": notes}), [])
        self.assertEqual(mirror_inbox({"enabled": True, "state": "waiting", "notifications": notes}), [])
        self.assertEqual(mirror_inbox({"enabled": True, "state": "connecting", "notifications": notes}), [])
        self.assertEqual(mirror_inbox({"enabled": True, "state": "ready", "notifications": "nope"}), [])
        self.assertEqual(mirror_inbox({"enabled": True, "state": "ready", "notifications": notes}), notes)

        dismissed = []
        with mock.patch("seen.mirror_inbox", return_value=[]) as reader:
            clear_conversation(FakeStore(), "tel:+15551234567", dismiss=dismissed.append)
        reader.assert_called_once()
        self.assertEqual(dismissed, [])


class HookTests(unittest.TestCase):
    def test_opening_and_replying_ask_for_a_clear(self):
        store = FakeStore()
        session = FakeSession()
        calls = []
        order = []

        class Ordering:
            async def mark_read(self, thread):
                order.append(("read", thread))

        async def run():
            with mock.patch.object(messagesd, "emit"):
                with mock.patch.object(
                    messagesd,
                    "_clear_phone_alerts",
                    side_effect=lambda *args: calls.append(args),
                ):
                    await messagesd.dispatch("mark-read", {"thread": "tel:+15551234567"}, store, None)
                    await messagesd.dispatch("mark-read", {"thread": "tel:+15551234567"}, store, Ordering())
                    await messagesd.dispatch(
                        "send",
                        {"thread": "tel:+15551234567", "address": "+15551234567", "body": "on my way"},
                        store,
                        session,
                    )

        asyncio.run(run())
        self.assertEqual(store.read, "tel:+15551234567")
        self.assertEqual(session.sent, ("+15551234567", "on my way"))
        self.assertEqual(order, [("read", "tel:+15551234567")])
        self.assertEqual(
            calls,
            [
                (store, "tel:+15551234567"),
                (store, "tel:+15551234567"),
                (store, "tel:+15551234567", "+15551234567"),
            ],
        )

    def test_a_failed_reply_does_not_clear(self):
        store = FakeStore()
        events = []
        calls = []

        class Failing:
            async def send(self, address, body):
                raise RuntimeError("phone unreachable")

        async def run():
            with mock.patch.object(messagesd, "emit", side_effect=lambda event: events.append(event)):
                with mock.patch.object(messagesd, "_clear_phone_alerts", side_effect=lambda *args: calls.append(args)):
                    await messagesd.handle(
                        json.dumps(
                            {
                                "cmd": "send",
                                "thread": "tel:+15551234567",
                                "address": "+15551234567",
                                "body": "hi",
                            }
                        ),
                        store,
                        Failing(),
                    )

        asyncio.run(run())
        self.assertEqual(calls, [])
        self.assertTrue(any(event.get("event") == "error" for event in events))

    def test_a_clear_failure_is_not_shown(self):
        store = FakeStore()
        events = []

        async def run():
            with mock.patch.object(messagesd, "emit", side_effect=lambda event: events.append(event)):
                with mock.patch("seen.clear_conversation", side_effect=RuntimeError("boom")):
                    await messagesd.handle(
                        json.dumps({"cmd": "mark-read", "thread": "tel:+15551234567"}),
                        store,
                        None,
                    )

        asyncio.run(run())
        self.assertFalse(any(event.get("event") == "error" for event in events))
        self.assertTrue(any(event.get("event") == "threads" for event in events))
