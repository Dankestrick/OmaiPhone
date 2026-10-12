"""Phone text must not become a notify-send option or notification markup."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend" / "texts"))

import messagesd
import protocol


class NotifyArgTests(unittest.TestCase):
    def test_title_cannot_be_an_option(self):
        args = messagesd.notification_argv("--urgency=critical", "hello")
        self.assertEqual(args[args.index("--") + 1], "--urgency=critical")
        self.assertNotIn("--urgency=critical", args[: args.index("--")])

    def test_markup_is_escaped(self):
        args = messagesd.notification_argv("Sam <3>", '<img src="http://evil/x">')
        title = args[args.index("--") + 1]
        body = args[-1]
        self.assertEqual(title, "Sam <3>")  # titles are plain text in Omarchy
        self.assertNotIn("<", body)
        self.assertIn("&lt;img", body)
        self.assertNotIn("http://evil", body.split("&")[0])

    def test_null_and_length_are_bounded(self):
        args = messagesd.notification_argv("A\x00B" + "C" * 200, "x" * 500)
        title = args[args.index("--") + 1]
        self.assertNotIn("\x00", title)
        self.assertLessEqual(len(title), 80)
        self.assertLessEqual(len(args[-1]), 120 * 5)  # escaping does not lengthen "x"


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
    def test_a_new_text_starts_a_popup(self):
        # Regression: a helper in messagesd once shadowed protocol.notification_title,
        # so every new text raised before its pop-up was made.
        from unittest import mock

        class Store:
            def settings(self):
                return {"notifyBody": True}

        started = []
        with mock.patch.object(messagesd, "spawn", lambda coro: (started.append(coro), coro.close())), \
                mock.patch.object(messagesd, "notify", lambda title, body: mock.MagicMock(close=lambda: None, title=title)):
            emit = messagesd.notifying_emit(Store(), lambda event: None)
            emit({"event": "message", "notify": True, "message": {"label": "Ada Lovelace", "body": "hi"}})
        self.assertEqual(len(started), 1)
        self.assertEqual(started[0].title, "Ada Lovelace")


if __name__ == "__main__":
    unittest.main()
