"""Which iPhone alerts belong to the conversation on screen."""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend" / "texts"))
sys.path.insert(0, str(ROOT / "backend"))

from matching import matching_uids  # noqa: E402
from omaiphone.notifications import TEXT_APPS  # noqa: E402


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


class MatchingTests(unittest.TestCase):
    def test_exact_name_match(self):
        inbox = [
            note(1, title="Mom"),
            note(2, title="Mom "),
            note(3, title="Mommy"),
            note(4, title="mom"),
            note(5, title="Mary Ann"),
            note(6, title="Mary  Ann"),
        ]
        self.assertEqual(matching_uids(inbox, "Mom", "+15551234567"), [1, 2])
        self.assertEqual(matching_uids(inbox, " Mary Ann ", ""), [5])

    def test_number_in_different_formats(self):
        inbox = [
            note(1, title="+1 (555) 123-4567"),
            note(2, title="(555) 123-4567"),
            note(3, title="555.123.4567"),
            note(4, title="0015551234567"),
            note(5, title="+44 7911 123456"),
            note(6, title="5559876543"),
        ]
        self.assertEqual(matching_uids(inbox, "", "+15551234567"), [1, 2, 3, 4])
        self.assertEqual(matching_uids(inbox, "555-123-4567", ""), [1, 2, 3, 4])
        self.assertEqual(matching_uids(inbox, "", "07911123456"), [5])

    def test_different_person_is_kept(self):
        inbox = [
            note(1, title="Mom"),
            note(2, title="Dad"),
            note(3, title="+19995550199"),
            note(4, title="Mom", message="Dad said hi"),
            note(5, title="Dad", subtitle="Mom"),
        ]
        self.assertEqual(matching_uids(inbox, "Mom", "+15551234567"), [1, 4])

    def test_other_app_with_the_same_title_is_kept(self):
        inbox = [
            note(1, title="Mom"),
            note(2, title="Mom", appId="com.apple.mobilemail", app="Mail"),
            note(3, title="Mom", appId="com.apple.mobilesms"),
            note(4, title="+15551234567", appId="com.apple.mobilephone", app="Phone"),
        ]
        self.assertIn("com.apple.MobileSMS", TEXT_APPS)
        self.assertEqual(matching_uids(inbox, "Mom", "+15551234567"), [1])

    def test_notification_the_phone_will_not_dismiss_is_skipped(self):
        inbox = [
            note(1, title="Mom", canDismiss=False),
            note(2, title="Mom", canDismiss=1),
            note(3, title="Mom", canDismiss="true"),
            note(4, title="Mom"),
            note(5),  # canDismiss missing once we pop it
        ]
        inbox[4].pop("canDismiss")
        self.assertEqual(matching_uids(inbox, "Mom", ""), [4])

    def test_empty_or_odd_labels_dismiss_nothing(self):
        inbox = [
            note(1, title="Mom"),
            note(2, title="Unknown"),
            note(3, title="???"),
            note(4, title=""),
            note(5, title="12345"),
            note(6, title="unknown"),
        ]
        for label in ("", "   ", "Unknown", "unknown", "???", "\x00", "\n", "Mom\nDad", "12345"):
            self.assertEqual(matching_uids(inbox, label, ""), [], label)
        self.assertEqual(matching_uids(inbox, "Nope", "not-a-number"), [])
        self.assertEqual(matching_uids(inbox, "Mom", ""), [1])

    def test_email_matches_the_address_only(self):
        inbox = [
            note(1, title="Ada@Example.com"),
            note(2, title="Ada"),
            note(3, title="bea@example.com"),
            note(4, title="Hi", message="ada@example.com"),
        ]
        self.assertEqual(matching_uids(inbox, "Ada", "ada@example.com"), [1, 2])

    def test_only_a_real_uid_is_returned(self):
        inbox = [
            note(True, title="Mom"),
            note("12;rm", title="Mom"),
            note("007", title="Mom"),
            note(-3, title="Mom"),
            note(2**32, title="Mom"),
            note("8", title="Mom"),
            note(8, title="Mom"),
            "not-a-notification",
            note(9, title="Mom"),
        ]
        self.assertEqual(matching_uids(inbox, "Mom", ""), [8, 9])
        self.assertEqual(matching_uids(None, "Mom", "+15551234567"), [])
        self.assertEqual(matching_uids({"title": "Mom"}, "Mom", ""), [])

    def test_a_quoted_number_inside_a_name_is_not_a_number(self):
        inbox = [note(1, title="Call +15551234567 now"), note(2, title="Mom 2")]
        self.assertEqual(matching_uids(inbox, "", "+15551234567"), [])
        self.assertEqual(matching_uids(inbox, "Mom 2", "+15551234567"), [2])
