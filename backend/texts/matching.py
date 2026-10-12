"""Which mirrored notifications belong to the conversation on screen.

The iPhone's notification title for a text is the contact name as the phone
shows it, or the number. This file decides the uids that are safe to clear.
It does not talk to the phone. If a title might be someone else, it is left
alone.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from protocol import coerce_phone, normalize_email, normalize_phone

# ANCS uids travel as a D-Bus uint32. Anything else is not a uid.
_UID_MAX = 0xFFFFFFFF


def _backend_on_path() -> None:
    folder = str(Path(__file__).resolve().parents[1])
    if folder not in sys.path:
        sys.path.insert(0, folder)


def _text_apps() -> tuple:
    """The same tuple the notification mirror uses. Imported lazily so this
    module still loads in the texts process before the calls package is needed."""
    _backend_on_path()
    from omaiphone.notifications import TEXT_APPS

    return TEXT_APPS


def _clean(value) -> str:
    if value is None:
        return ""
    return str(value).replace("\x00", "").strip()


def _phone_shaped(value: str) -> bool:
    """True when the whole string is a phone number, not a sentence that contains one.

    A name with digits in it, or a message that quotes a number, must not be
    treated as the sender's number.
    """
    text = _clean(value)
    if not text or any(ch.isalpha() for ch in text):
        return False
    if any(ord(ch) < 32 for ch in text):
        return False
    digits = re.sub(r"\D", "", text)
    return 8 <= len(digits) <= 15


def _usable_name(label: str) -> str:
    """A contact name we are willing to match exactly. Empty when it is not one.

    "Unknown" is the placeholder this program stores when it has no name.
    A string with no letters is a number or punctuation, not a name.
    """
    text = _clean(label)
    if not text or text.casefold() == "unknown":
        return ""
    if any(ord(ch) < 32 for ch in text):
        return ""
    if not any(ch.isalpha() for ch in text):
        return ""
    return text


def _name_match(title, label) -> bool:
    name = _usable_name(label)
    if not name:
        return False
    return _clean(title) == name


def _e164_forms(value: str, hint: str, direct: str) -> set[str]:
    forms = set()
    if direct:
        forms.add(direct)
    coerced = coerce_phone(value, hint)
    if coerced:
        forms.add(coerced)
    return forms


def _same_phone(left, right) -> bool:
    """True when both strings are the same number written differently.

    Formatting (spaces, dashes, a leading +) is ignored. A national number
    is accepted only when the other side is already that number in full
    international form, so a 10-digit string is not guessed into a country.
    """
    if not _phone_shaped(left) or not _phone_shaped(right):
        return False
    left_e = normalize_phone(left)
    right_e = normalize_phone(right)
    if left_e and right_e:
        return left_e == right_e
    hint = left_e or right_e
    if hint:
        return bool(_e164_forms(left, hint, left_e) & _e164_forms(right, hint, right_e))
    try:
        _backend_on_path()
        from omaiphone.phone import InvalidNumber, normalize
    except Exception:
        return False
    try:
        return normalize(left) == normalize(right)
    except InvalidNumber:
        return False


def _same_email(left, right) -> bool:
    one = normalize_email(_clean(left))
    other = normalize_email(_clean(right))
    return bool(one and one == other)


def _sender_matches(title, label, address) -> bool:
    if _name_match(title, label):
        return True
    if _same_phone(title, address) or _same_phone(title, label):
        return True
    if _same_email(title, address) or _same_email(title, label):
        return True
    return False


def _uid_number(value):
    """A uid that is safe to hand to Dismiss, or None."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        number = value
    elif isinstance(value, str) and value.isdigit() and not (len(value) > 1 and value.startswith("0")):
        number = int(value)
    else:
        return None
    if number < 0 or number > _UID_MAX:
        return None
    return number


def matching_uids(inbox, label, address) -> list:
    """Uids of this person's text notifications that the phone allows us to clear.

    `label` is the name the conversation shows. `address` is the phone number
    or email. Only a Messages notification is considered, and only when its
    title is exactly that name, or the same phone number or email. Other
    apps, other people, and notifications the phone will not dismiss are
    skipped. An empty or placeholder label clears nothing by itself.
    """
    if not isinstance(inbox, list):
        return []
    apps = _text_apps()
    found = []
    seen = set()
    for item in inbox:
        if not isinstance(item, dict):
            continue
        if item.get("appId") not in apps:
            continue
        if item.get("canDismiss") is not True:
            continue
        if not _sender_matches(item.get("title"), label, address):
            continue
        uid = _uid_number(item.get("uid"))
        if uid is None or uid in seen:
            continue
        seen.add(uid)
        found.append(uid)
    return found
