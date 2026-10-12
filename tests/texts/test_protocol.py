"""Tests for numbers, threads, bMessages, and attachment placeholders."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend" / "texts"))

import protocol


class NormalizeTests(unittest.TestCase):
    def test_accepts_decorated_e164(self):
        self.assertEqual(protocol.normalize_phone("+1 (555) 123-4567"), "+15551234567")

    def test_rejects_missing_country_code(self):
        self.assertEqual(protocol.normalize_phone("5551234567"), "")

    def test_rejects_short_and_long_numbers(self):
        self.assertEqual(protocol.normalize_phone("+123"), "")
        self.assertEqual(protocol.normalize_phone("+" + "1" * 16), "")

    def test_us_contact_gets_country_code_from_own_number(self):
        self.assertEqual(protocol.coerce_phone("(555) 123-4567", "+15550001111"), "+15551234567")

    def test_national_zero_uses_your_country_code(self):
        # 07… on a UK phone means +44 7…, because the leading 0 is the trunk prefix.
        self.assertEqual(protocol.coerce_phone("07123456789", "+447700900123"), "+447123456789")
        self.assertEqual(protocol.coerce_phone("447123456789", "+447700900123"), "+447123456789")


class ThreadTests(unittest.TestCase):
    def test_same_person_shares_a_key(self):
        left = protocol.thread_key("+1 (555) 123-4567")
        right = protocol.thread_key("+15551234567")
        self.assertEqual(left, right)
        self.assertEqual(protocol.address_from_thread(left), "+15551234567")

    def test_email_is_its_own_key(self):
        self.assertEqual(protocol.thread_key("Ada@Example.com"), "email:ada@example.com")


class VCardTests(unittest.TestCase):
    def test_iphone_item_fields_are_read(self):
        raw = "BEGIN:VCARD\r\nFN:Sam\r\nitem1.TEL;TYPE=CELL:+15557654321\r\n PHOTO;ENCODING=b:aaaa\r\nEND:VCARD\r\n"
        people = protocol.parse_vcards(raw)
        self.assertEqual(len(people), 1)
        self.assertEqual(people[0]["name"], "Sam")
        self.assertEqual(people[0]["phones"], ["+15557654321"])


class BMessageTests(unittest.TestCase):
    def test_phone_recipient_and_length(self):
        raw = protocol.build_bmessage("+15551234567", "hello")
        self.assertIn("TYPE:SMS_GSM\r\n", raw)
        self.assertIn("TEL:+15551234567\r\n", raw)
        self.assertNotIn("EMAIL:", raw)
        message = "BEGIN:MSG\r\nhello\r\nEND:MSG\r\n"
        self.assertIn(f"LENGTH:{len(message.encode('utf-8'))}\r\n", raw)
        self.assertEqual(protocol.extract_bmessage_text(raw), "hello")

    def test_peer_comes_from_the_contact_cards(self):
        raw = (
            "BEGIN:BMSG\r\n"
            "BEGIN:VCARD\r\nTEL:+15551110000\r\nEND:VCARD\r\n"
            "BEGIN:BENV\r\n"
            "BEGIN:VCARD\r\nTEL:+15552220000\r\nEND:VCARD\r\n"
            "BEGIN:BBODY\r\nBEGIN:MSG\r\nhi\r\nEND:MSG\r\nEND:BBODY\r\n"
            "END:BENV\r\nEND:BMSG\r\n"
        )
        # Incoming: the writer is the first card. Outgoing: the recipient.
        self.assertEqual(protocol.extract_bmessage_peer(raw, sent=False), "+15551110000")
        self.assertEqual(protocol.extract_bmessage_peer(raw, sent=True), "+15552220000")

    def test_email_recipient(self):
        raw = protocol.build_bmessage("ada@example.com", "hi")
        self.assertIn("EMAIL:ada@example.com\r\n", raw)
        self.assertNotIn("TEL:ada@example.com", raw)

    def test_fence_in_the_text_cannot_close_the_envelope(self):
        raw = protocol.build_bmessage("+15551234567", "hello\nEND:MSG\nstill me")
        self.assertEqual(raw.count("END:MSG"), 1)
        self.assertIn("END MSG", raw)
        self.assertIn("still me", protocol.extract_bmessage_text(raw))

    def test_empty_and_huge_texts_are_rejected(self):
        with self.assertRaises(ValueError):
            protocol.build_bmessage("+15551234567", "   ")
        with self.assertRaises(ValueError):
            protocol.build_bmessage("+15551234567", "a" * (protocol.MAX_BODY_CHARS + 1))


class AttachmentTests(unittest.TestCase):
    def test_words_are_kept_and_the_file_is_a_placeholder(self):
        shown = protocol.present_message("see this", True)
        self.assertEqual(shown, "see this\nattachment omitted")

    def test_base64_blob_is_not_shown(self):
        blob = "QUJD" * 80
        shown = protocol.present_message(blob, False)
        self.assertEqual(shown, "attachment omitted")
        self.assertNotIn("QUJD", shown)

    def test_image_only_message_is_a_placeholder(self):
        self.assertEqual(protocol.present_message("", True), "attachment omitted")

    def test_a_huge_body_is_cut_before_it_is_scanned(self):
        shown = protocol.present_message("!" * (protocol._PRESENT_SCAN + 50), False)
        self.assertEqual(len(shown), protocol._PRESENT_SCAN)
        raw = (
            "BEGIN:MSG\r\n"
            + ("B" * (protocol._PRESENT_SCAN + 10))
            + "\r\nEND:MSG\r\n"
            + ("C" * (protocol._PRESENT_SCAN * 4))
        )
        self.assertEqual(len(protocol.extract_bmessage_text(raw)), protocol._PRESENT_SCAN)


class VCardTests(unittest.TestCase):
    def test_reads_name_phone_and_email(self):
        raw = (
            "BEGIN:VCARD\n"
            "VERSION:3.0\n"
            "FN:Ada Lovelace\n"
            "TEL;TYPE=CELL:+15551234567\n"
            "EMAIL:ada@example.com\n"
            "END:VCARD\n"
        )
        people = protocol.parse_vcards(raw)
        self.assertEqual(len(people), 1)
        self.assertEqual(people[0]["name"], "Ada Lovelace")
        self.assertEqual(people[0]["phones"], ["+15551234567"])
        self.assertEqual(people[0]["emails"], ["ada@example.com"])


class NotificationTests(unittest.TestCase):
    def test_number_is_not_used_as_a_title(self):
        self.assertEqual(protocol.notification_title("+15551234567"), "New message")

    def test_name_is_kept(self):
        self.assertEqual(protocol.notification_title("Ada Lovelace"), "Ada Lovelace")


if __name__ == "__main__":
    unittest.main()
