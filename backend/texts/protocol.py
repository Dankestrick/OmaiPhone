"""Pure helpers for phone numbers, message text, and MAP bMessages.

Nothing in this file talks to Bluetooth or the disk. The tests cover it
without a phone nearby. A bMessage is the plain-text envelope the public
Bluetooth Message Access Profile uses to hand one text to a phone.
"""

from __future__ import annotations

import re

# One thousand characters is long enough for a real text and far below
# a multi-kilobyte paste. MAP and the phone both get unhappy past that.
MAX_BODY_CHARS = 1000

ATTACHMENT_LINE = "attachment omitted"

_PHONE_DECORATION = re.compile(r"[^\d+]")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
# Lines that would close the bMessage envelope early if a text contained them.
_FENCE = re.compile(r"(?i)^(begin|end):(bmsg|bbody|benv|msg|vcard)\s*$")
_BASE64_ALPHABET = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=")


def normalize_phone(value: str) -> str:
    """Return E.164 (`+` and 8 to 15 digits) or an empty string.

    E.164 is the international form of a phone number. `+15551234567` is
    valid. A number without a country code is rejected here so we never
    guess which country a bare 10-digit number belongs to.
    """
    if not value:
        return ""
    cleaned = _PHONE_DECORATION.sub("", value.strip())
    if cleaned.startswith("00"):
        cleaned = "+" + cleaned[2:]
    if not cleaned.startswith("+"):
        return ""
    digits = cleaned[1:]
    if not digits.isdigit() or not 8 <= len(digits) <= 15:
        return ""
    return "+" + digits


def normalize_email(value: str) -> str:
    """Return a lowercase email address, or an empty string when it is not one."""
    if not value:
        return ""
    text = value.strip().lower()
    if len(text) > 254 or not _EMAIL.match(text):
        return ""
    return text


# One-digit calling codes. Everything else in this set is two digits.
# A number written as 07911… uses a leading 0 to mean "this country".
# The country is taken from your own number so a UK 07… becomes +44…
_CALLING_CODES_1 = {"1", "7"}
_CALLING_CODES_2 = {
    "20", "27", "30", "31", "32", "33", "34", "36", "39", "40", "41", "43", "44",
    "45", "46", "47", "48", "49", "51", "52", "53", "54", "55", "56", "57", "58",
    "60", "61", "62", "63", "64", "65", "66", "81", "82", "84", "86", "90", "91",
    "92", "93", "94", "95", "98",
}


def calling_code(own_number: str) -> str:
    """Country calling code at the start of an E.164 number, such as 44."""
    own = normalize_phone(own_number)
    if not own:
        return ""
    digits = own[1:]
    if digits[:1] in _CALLING_CODES_1:
        return digits[:1]
    if digits[:2] in _CALLING_CODES_2:
        return digits[:2]
    return digits[:3] if len(digits) >= 3 else ""


def coerce_phone(value: str, own_number: str) -> str:
    """Turn a contact number into E.164 when that is unambiguous.

    If the contact is already E.164, that value is used. A national
    number that starts with 0 uses the country code from your own number.
    A US number of exactly 10 digits uses +1 when your number is +1.
    """
    direct = normalize_phone(value)
    if direct:
        return direct
    digits = re.sub(r"\D", "", value or "")
    own = normalize_phone(own_number)
    code = calling_code(own)
    if digits.startswith("0") and not digits.startswith("00") and code:
        return normalize_phone("+" + code + digits[1:])
    if own.startswith("+1") and len(digits) == 10:
        return normalize_phone("+1" + digits)
    # 447… without a plus is the same number as +447… when you are in that country.
    if code and digits.startswith(code) and 8 <= len(digits) <= 15:
        return normalize_phone("+" + digits)
    return ""


def thread_key(address: str, own_number: str = "") -> str:
    """Group texts from the same person under one stable key.

    Formatting differences (`+1 (555) 123-4567` and `+15551234567`) share
    a key. The key is a label, not a secret, but it is still personal, so
    callers must not write it to the log.
    """
    email = normalize_email(address)
    if email:
        return "email:" + email
    phone = coerce_phone(address, own_number) or normalize_phone(address)
    if phone:
        return "tel:" + phone
    compact = re.sub(r"\s+", "", address or "")[:80]
    return "raw:" + compact if compact else "unknown"


def address_from_thread(key: str) -> str:
    """Recover the sendable address stored inside a thread key."""
    if key.startswith("tel:"):
        return key[4:]
    if key.startswith("email:"):
        return key[6:]
    return ""


def classify_recipient(address: str, own_number: str = "") -> tuple[str, str]:
    """Return `("tel", e164)` or `("email", address)`.

    Raises ValueError when the address cannot be sent to. The message
    never includes the address text in a way that gets logged by the daemon;
    the UI shows ValueError text, which names the rule, not the number.
    """
    phone = coerce_phone(address, own_number)
    if phone:
        return "tel", phone
    email = normalize_email(address)
    if email:
        return "email", email
    raise ValueError("Recipient must be a +country phone number or an email address.")


def sanitize_body(body: str) -> str:
    """Clean text that will be placed inside a bMessage.

    A text that itself contains `END:MSG` would close the envelope early
    and could smuggle extra fields. Those fence lines are defanged.
    """
    if body is None:
        raise ValueError("Message text is empty.")
    if "\x00" in body:
        raise ValueError("Message text cannot contain a null character.")
    text = body.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        raise ValueError("Message text is empty.")
    if len(text) > MAX_BODY_CHARS:
        raise ValueError(f"Message text is longer than {MAX_BODY_CHARS} characters.")
    lines = []
    for line in text.split("\n"):
        if _FENCE.match(line.strip()):
            lines.append(line.replace(":", " ", 1))
        else:
            lines.append(line)
    return "\r\n".join(lines)


def build_bmessage(recipient: str, body: str, own_number: str = "") -> str:
    """Build one MAP 1.4 bMessage for a single recipient.

    The originator card is empty because the phone fills in the sender.
    Phone numbers use the vCard TEL field. Email addresses use EMAIL.
    `LENGTH` counts the bytes of the BEGIN:MSG ... END:MSG block.
    """
    kind, value = classify_recipient(recipient, own_number)
    safe = sanitize_body(body)
    message = f"BEGIN:MSG\r\n{safe}\r\nEND:MSG\r\n"
    length = len(message.encode("utf-8"))
    recipient_line = f"TEL:{value}\r\n" if kind == "tel" else f"EMAIL:{value}\r\n"
    return (
        "BEGIN:BMSG\r\n"
        "VERSION:1.0\r\n"
        "STATUS:UNREAD\r\n"
        "TYPE:SMS_GSM\r\n"
        "FOLDER:telecom/msg/outbox\r\n"
        "BEGIN:VCARD\r\n"
        "VERSION:2.1\r\n"
        "N:\r\n"
        "TEL:\r\n"
        "END:VCARD\r\n"
        "BEGIN:BENV\r\n"
        "BEGIN:VCARD\r\n"
        "VERSION:2.1\r\n"
        "N:\r\n"
        f"{recipient_line}"
        "END:VCARD\r\n"
        "BEGIN:BBODY\r\n"
        "CHARSET:UTF-8\r\n"
        f"LENGTH:{length}\r\n"
        f"{message}"
        "END:BBODY\r\n"
        "END:BENV\r\n"
        "END:BMSG\r\n"
    )


def extract_bmessage_peer(raw: str, sent: bool) -> str:
    """Who the conversation is with, taken from a downloaded bMessage.

    The first contact card is the person who wrote the text. The next card
    is who received it. A new-text alert often leaves the sender blank, so
    the downloaded file is the place that actually names them.
    """
    cards = parse_vcards(raw or "")

    def pick(card: dict | None) -> str:
        if not card:
            return ""
        if card["phones"]:
            return card["phones"][0]
        if card["emails"]:
            return card["emails"][0]
        return ""

    origin = pick(cards[0]) if cards else ""
    recipient = pick(cards[1]) if len(cards) > 1 else ""
    # A text you sent is filed under the recipient. One you received is
    # filed under the writer. If the writer is missing, keep the other
    # number so the text is not thrown away.
    if sent:
        return recipient or origin
    return origin or recipient


def extract_bmessage_text(raw: str) -> str:
    """Pull the plain text out of a downloaded bMessage, ignoring attachments."""
    if not raw:
        return ""
    match = re.search(r"BEGIN:MSG\r?\n(.*?)\r?\nEND:MSG", raw, flags=re.DOTALL | re.IGNORECASE)
    if not match:
        return ""
    return match.group(1).replace("\r\n", "\n").replace("\r", "\n").strip()


def looks_like_attachment_payload(text: str) -> bool:
    """True when a blob is almost certainly file data rather than a text.

    MAP does not give us useful images. A long base64 run, or a body that
    names an image type, is dropped and replaced with a placeholder.
    """
    if not text:
        return False
    lowered = text.lower()
    if "image/" in lowered or "begin:btml" in lowered or "filename=" in lowered:
        return True
    compact = "".join(text.split())
    if len(compact) >= 200 and all(ch in _BASE64_ALPHABET for ch in compact):
        return True
    return False


def present_message(body: str, has_attachment: bool) -> str:
    """Text the overlay is allowed to show.

    Image bytes never pass through. When the phone says a file was
    attached, the transcript keeps the words and adds one plain line.
    """
    text = (body or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if looks_like_attachment_payload(text):
        text = ""
        has_attachment = True
    if has_attachment:
        if text:
            return text + "\n" + ATTACHMENT_LINE
        return ATTACHMENT_LINE
    return text


def parse_vcards(raw: str) -> list[dict]:
    """Read names, phones, and emails out of a PBAP vCard file.

    PBAP is the Bluetooth phone book. Each card can carry several numbers.
    Callers still run those numbers through the E.164 helpers before sending.
    """
    people = []
    current = None
    for raw_line in raw.splitlines():
        # A line that starts with a space continues the line above.
        # Contact photos use that form and are not names or numbers.
        if raw_line.startswith((" ", "\t")):
            continue
        line = raw_line.strip()
        upper = line.upper()
        if upper.startswith("BEGIN:VCARD"):
            current = {"name": "", "phones": [], "emails": []}
            continue
        if upper.startswith("END:VCARD"):
            if current and (current["name"] or current["phones"] or current["emails"]):
                people.append(current)
            current = None
            continue
        if current is None or ":" not in line:
            continue
        key, value = line.split(":", 1)
        # iOS writes item1.TEL and item2.EMAIL. The part after the dot
        # is the real field name.
        field = key.split(";")[0].upper()
        if "." in field:
            field = field.rsplit(".", 1)[-1]
        value = value.strip()
        if field == "FN" and value:
            current["name"] = value
        elif field == "TEL" and value:
            current["phones"].append(value)
        elif field == "EMAIL" and value:
            current["emails"].append(value)
    return people


def notification_title(sender: str) -> str:
    """Name to show in a desktop notification.

    A phone number or email is not a name. Those fall back to a generic
    title so a lock screen does not reveal who texted.
    """
    cleaned = (sender or "").strip()
    if not cleaned:
        return "New message"
    if normalize_phone(cleaned) or normalize_email(cleaned):
        return "New message"
    if any(ch.isdigit() for ch in cleaned) and sum(ch.isdigit() for ch in cleaned) >= 7:
        return "New message"
    return cleaned[:80]


def public_error(exc: BaseException) -> str:
    """Turn a Bluetooth failure into a short sentence safe to show.

    The sentence describes the next step. It does not include message
    text, contact names, or the pairing code.
    """
    text = str(exc)
    lowered = text.lower()
    if "forbidden" in lowered or "0x43" in lowered or "not authorized" in lowered:
        return "The iPhone has not allowed message access yet. Turn on Show Message Notifications."
    if "connection refused" in lowered or "errno 111" in lowered or "host is down" in lowered:
        return "The phone is already in a message session with another computer, or it is out of range."
    if "dbus" in lowered and "not found" in lowered or "no such file" in lowered or "serviceunknown" in lowered.replace(" ", ""):
        return "Bluetooth messaging is not running. Install bluez-obex and make sure Bluetooth is on."
    if "org.bluez.error.authentication" in lowered or "authentication" in lowered and "cancel" in lowered:
        return "Pairing was cancelled."
    # Keep a short class name when the text might contain a device path.
    name = type(exc).__name__
    if len(text) > 160 or "BEGIN:MSG" in text or "BEGIN:VCARD" in text:
        return f"Bluetooth error ({name})."
    one_line = " ".join(text.split())
    return one_line or f"Bluetooth error ({name})."
