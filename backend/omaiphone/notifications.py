"""What the PC keeps of the phone's notifications, and when it shows one.

Notification texts are personal. They live in memory and in one file in the
user's runtime directory (tmpfs, gone at logout, readable only by the user)
so that the panel and the CLI can show them. They are never logged.
"""

import json
import os

from .audio import STATE_DIR, write_private

LIMIT = 60
FIELD_MAX = 500

# Without a runtime directory the state falls back to the user's state
# directory, still private to the user but on disk.
RUNTIME_DIR = os.path.join(os.environ.get("XDG_RUNTIME_DIR") or STATE_DIR, "omaiphone")
STATE_FILE = os.path.join(RUNTIME_DIR, "mirror.json")

EMPTY_MEDIA = {"available": False, "player": "", "title": "", "artist": "", "album": "", "playing": False,
               "commands": []}

TEXT_APPS = ("com.apple.MobileSMS",)

PUBLIC_FIELDS = ("uid", "appId", "app", "title", "subtitle", "message", "date", "category", "important",
                 "canDismiss")


def plain_markup(value, limit, keep_newlines=False):
    """Escape text for a notification daemon that renders the body as markup."""
    text = str(value or "").replace("\x00", "")
    if keep_newlines:
        text = text.replace("\r\n", "\n").replace("\r", "\n")
    else:
        text = " ".join(text.split())
    text = text[:limit]
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _field(value):
    if isinstance(value, str):
        return value.replace("\x00", "")[:FIELD_MAX]
    return value


class Store:
    """The phone's current notifications, newest first."""

    def __init__(self):
        self.items = {}
        self.dropped = []

    def apply(self, notification):
        """Add or replace. Returns True when this uid was not known before.

        `dropped` is the uids evicted to keep the list at LIMIT. The caller
        closes those desktop notifications.
        """
        self.dropped = []
        uid = notification["uid"]
        new = uid not in self.items
        self.items.pop(uid, None)
        self.items[uid] = {key: _field(notification.get(key)) for key in PUBLIC_FIELDS}
        while len(self.items) > LIMIT:
            oldest = min(self.items, key=lambda item: (self.items[item]["date"] or "", self.items[item]["uid"]))
            self.items.pop(oldest, None)
            self.dropped.append(oldest)
        return new

    def remove(self, uid):
        return self.items.pop(uid, None) is not None

    def clear(self):
        self.items = {}

    def listing(self):
        # ANCS uids grow with time, so they break ties between equal dates
        ordered = sorted(self.items.values(), key=lambda n: (n["date"] or "", n["uid"]), reverse=True)
        return ordered[:LIMIT]


def mirror_enabled(config):
    return config.get("mirror", False) is True


def should_toast(notification, config):
    """Whether a desktop notification pops up for it."""
    if config.get("mirrorToasts", True) is False:
        return False
    if notification.get("preexisting") or notification.get("silent"):
        return False
    # Calls already ring through the hands-free part of this plugin.
    if notification.get("category") == "incoming-call":
        return False
    # Texts already pop up through the Messages part (MAP), with the contact's name.
    if notification.get("appId") in TEXT_APPS and config.get("texts", True) is not False:
        return False
    return notification.get("appId") not in config.get("mirrorMutedApps", [])


def toast_text(notification):
    """(application name, summary, body) for the desktop notification."""
    app = notification.get("app") or "iPhone"
    title = notification.get("title") or app
    lines = [text for text in (notification.get("subtitle"), notification.get("message")) if text]
    return app, title, "\n".join(lines)


def toast_payload(notification):
    """The same three strings, escaped and capped for the notification daemon."""
    app, title, body = toast_text(notification)
    return (
        plain_markup(app, 80) or "iPhone",
        plain_markup(title, 120) or "iPhone",
        plain_markup(body, 400, keep_newlines=True),
    )


def snapshot(enabled, state="off", detail="", store=None, media=None, battery=None):
    return {
        "battery": battery if enabled else None,
        "enabled": bool(enabled),
        "state": state if enabled else "off",
        "detail": detail if enabled else "",
        "notifications": store.listing() if store and enabled else [],
        "media": dict(media) if media and enabled else dict(EMPTY_MEDIA),
    }


def write_state(data, path=STATE_FILE):
    """Atomically, and readable by the user only."""
    write_private(path, json.dumps(data))


def read_state(path=STATE_FILE):
    try:
        with open(path) as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return snapshot(False)
    if not isinstance(data, dict) or not isinstance(data.get("notifications"), list):
        return snapshot(False)
    return data


def remove_state(path=STATE_FILE):
    try:
        os.remove(path)
    except OSError:
        pass
