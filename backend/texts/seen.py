"""Clear one person's text alerts on the iPhone after they are read or answered.

The name and the number stay in this process. The notification service is
asked to dismiss a uid, and the uid is a number. When mirroring is off or
the phone is not connected, this does nothing and raises nothing.
"""

from __future__ import annotations

import sys
from pathlib import Path

from matching import matching_uids
from protocol import address_from_thread, coerce_phone, normalize_phone, thread_key

_UID_MAX = 0xFFFFFFFF


def _backend_on_path() -> None:
    folder = str(Path(__file__).resolve().parents[1])
    if folder not in sys.path:
        sys.path.insert(0, folder)


def _placeholder(label: str) -> bool:
    text = str(label or "").strip()
    # "Unknown" is the label stored when a conversation has no name yet.
    return text == "" or text.casefold() == "unknown"


def _own_number(store) -> str:
    if store is None or not hasattr(store, "settings"):
        return ""
    try:
        settings = store.settings() or {}
    except Exception:
        return ""
    if not isinstance(settings, dict):
        return ""
    return str(settings.get("ownNumber") or "")


def _thread_label(store, thread: str) -> str:
    if not thread or store is None or not hasattr(store, "threads"):
        return ""
    try:
        rows = store.threads() or []
    except Exception:
        return ""
    if not isinstance(rows, list):
        return ""
    for row in rows:
        if isinstance(row, dict) and row.get("key") == thread:
            return str(row.get("label") or "")
    return ""


def _address_forms(number: str, own: str) -> list[str]:
    forms: list[str] = []

    def add(value: str) -> None:
        text = str(value or "").strip()
        if text and text not in forms:
            forms.append(text)

    add(number)
    add(normalize_phone(number))
    if own:
        add(coerce_phone(number, own))
    return forms


def _contact_name(store, number: str, own: str) -> str:
    if store is None or not hasattr(store, "label_for"):
        return ""
    for candidate in _address_forms(number, own):
        try:
            found = str(store.label_for(candidate) or "").strip()
        except Exception:
            continue
        if found and not _placeholder(found) and found != candidate:
            return found
    return ""


def conversation_identity(store, thread: str, address: str = "") -> tuple[str, str]:
    """Name and phone number or email for the conversation the user has open."""
    thread = str(thread or "")
    number = str(address or "").strip() or address_from_thread(thread)
    own = _own_number(store)
    label = _thread_label(store, thread)
    if _placeholder(label):
        for form in _address_forms(number, own):
            label = _thread_label(store, thread_key(form, own))
            if not _placeholder(label):
                break
    if _placeholder(label):
        label = _contact_name(store, number, own)
    if _placeholder(label):
        label = ""
    return label, number


def mirror_inbox(state=None) -> list:
    """Notifications that can be cleared right now.

    The mirror file still holds the last list for a moment after the phone
    drops. Clearing is allowed only while mirroring is on and the link is ready.
    """
    try:
        if state is None:
            state = _read_mirror_state()
        if not isinstance(state, dict):
            return []
        if state.get("enabled") is not True or state.get("state") != "ready":
            return []
        notes = state.get("notifications")
        return notes if isinstance(notes, list) else []
    except Exception:
        return []


def _read_mirror_state():
    _backend_on_path()
    from omaiphone.notifications import read_state

    return read_state()


def _safe_uid(uid):
    if isinstance(uid, bool) or not isinstance(uid, int):
        return None
    if uid < 0 or uid > _UID_MAX or not str(uid).isdigit():
        return None
    return uid


def _dismiss_on_phone(uid: int) -> None:
    """Existing D-Bus Dismiss. The uid is the only value that leaves this process."""
    safe = _safe_uid(uid)
    if safe is None:
        return
    _backend_on_path()
    from gi.repository import GLib

    from omaiphone.control import call

    call("Dismiss", GLib.Variant("(u)", (safe,)))


def clear_conversation(store, thread: str, address: str = "", *, inbox=None, dismiss=None) -> None:
    """Dismiss this person's text alerts. Never raises and never reports an error."""
    try:
        label, number = conversation_identity(store, thread, address)
        if inbox is None:
            inbox = mirror_inbox()
        if dismiss is None:
            dismiss = _dismiss_on_phone
        for uid in matching_uids(inbox, label, number):
            safe = _safe_uid(uid)
            if safe is None:
                continue
            try:
                dismiss(safe)
            except Exception:
                continue
    except Exception:
        return
