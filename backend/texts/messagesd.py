#!/usr/bin/env python3
"""Messages part of OmaiPhone (texts over MAP), based on iMsg by Nikolaos Gkionis.

The overlay talks to this process with one JSON object per line on
stdin. Events go out as JSON lines on stdout. Stderr is only for
connection state. Message text, contact names, pairing codes, and your
phone number are never written there.

This process runs as your user. It does not use sudo, and it does
not start a shell to run commands.
"""

from __future__ import annotations

import asyncio
import json
import sys

from protocol import address_from_thread, normalize_phone, notification_title, public_error
from store import Store

_MAC = __import__("re").compile(r"^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")


# The event loop keeps only a weak reference to tasks. Without this set,
# a send or a notification can be garbage-collected before it finishes.
_background: set[asyncio.Task] = set()


def spawn(coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _background.add(task)
    task.add_done_callback(_background.discard)
    return task


def emit(event: dict) -> None:
    """Send one event to the shell. The caller must already have removed secrets from logs."""
    sys.stdout.write(json.dumps(event, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _plain_title(value: str, limit: int) -> str:
    """One plain line for the title. Omarchy shows titles as plain text (tested),
    so escaping it would show "Tom &amp; Jerry"."""
    return " ".join(str(value or "").replace("\x00", "").split())[:limit]


def notification_text(value: str, limit: int, keep_newlines: bool = False) -> str:
    """Plain text for a desktop notification.

    notify-send and the notification daemon treat the body as markup.
    A name or a text from the phone must not become an option (`--urgency`)
    or a tag (`<img>`, `<a href>`).
    """
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


def notification_content(title: str, body: str) -> tuple[str, str]:
    """Plain title and escaped body for one pop-up.

    Omarchy shows the title as plain text, so escaping it would show
    "&amp;" on screen. The body is markup.
    """
    safe_title = _plain_title(title, 80) or "New message"
    safe_body = notification_text(body, 120, keep_newlines=True) if body else ""
    return safe_title, safe_body


def log_state(state: str) -> None:
    """Record a connection state. The string is one of the fixed state names."""
    if state not in {
        "not_paired",
        "scanning",
        "pairing",
        "phone_nearby",
        "waiting_for_switches",
        "connected",
    }:
        state = "unknown"
    print(f"state {state}", file=sys.stderr, flush=True)


def status_from_store(store: Store, ready: bool, detail: str) -> dict:
    settings = store.settings()
    return {
        "state": "not_paired" if not settings["phoneAddress"] else "phone_nearby",
        "phoneName": settings["phoneName"],
        "phoneAddress": settings["phoneAddress"],
        "ownNumber": settings["ownNumber"],
        "unread": store.unread_count(),
        "notifyBody": settings["notifyBody"],
        "detail": detail,
        "pairFallback": False,
        "bluetoothReady": ready,
    }


# org.freedesktop.Notifications. A click in Omarchy invokes the "default" action.
_NOTIFY_NAME = "org.freedesktop.Notifications"
_NOTIFY_PATH = "/org/freedesktop/Notifications"
_BUS_NAME = "org.freedesktop.DBus"
_BUS_PATH = "/org/freedesktop/DBus"
# A burst of texts must not put a Notify call in flight for every one of them.
_NOTIFY_LIMIT = 2
_NOTIFY_TIMEOUT = 5
# Ids this program created. Closed ones are dropped; the rest stay bounded.
_POPUP_LIMIT = 50

_MATCH_ACTION = (
    "type='signal',sender='org.freedesktop.Notifications',"
    "interface='org.freedesktop.Notifications',"
    "path='/org/freedesktop/Notifications',member='ActionInvoked'"
)
_MATCH_CLOSED = (
    "type='signal',sender='org.freedesktop.Notifications',"
    "interface='org.freedesktop.Notifications',"
    "path='/org/freedesktop/Notifications',member='NotificationClosed'"
)
_MATCH_OWNER = (
    "type='signal',sender='org.freedesktop.DBus',"
    "interface='org.freedesktop.DBus',path='/org/freedesktop/DBus',"
    "member='NameOwnerChanged',arg0='org.freedesktop.Notifications'"
)

_dbus = None


def _dbus_types():
    global _dbus
    if _dbus is None:
        from dbus_next import Message, Variant
        from dbus_next.constants import MessageType

        _dbus = (Message, Variant, MessageType)
    return _dbus


def _clip(value, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return value.replace("\x00", "")[:limit]


def _notification_id(value) -> int | None:
    """A Notify id is a positive uint32. Booleans are ints in Python and are not ids."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value <= 0 or value > 0xFFFFFFFF:
        return None
    return value


def _unique_name(value) -> str:
    """A connection's unique bus name (`:1.42`). Well-known names do not count."""
    if not isinstance(value, str) or not value.startswith(":") or len(value) > 255:
        return ""
    return value


def notification_message(title: str, body: str):
    """One Notify call. The phone's text is a D-Bus string, never a command argument."""
    Message, Variant, _message_type = _dbus_types()
    summary, text = notification_content(title, body)
    return Message(
        destination=_NOTIFY_NAME,
        path=_NOTIFY_PATH,
        interface=_NOTIFY_NAME,
        member="Notify",
        signature="susssasa{sv}i",
        body=[
            "Messages",
            0,
            "",
            summary,
            text,
            ["default", "Open"],
            {"urgency": Variant("y", 1)},
            -1,
        ],
    )


class PopupClicks:
    """Which notification ids this program created, and what a click should open.

    ActionInvoked is a session-bus signal. A signal addressed to this
    connection is delivered even when it matches no rule, so the sender has
    to be checked here. Only the current owner of the notification name is
    accepted, and only for an id Notify returned to us.
    """

    def __init__(self):
        self.owner = ""
        self._ids: dict[int, dict] = {}

    @property
    def tracked(self) -> int:
        return len(self._ids)

    def set_owner(self, owner: str) -> None:
        if not isinstance(owner, str):
            owner = ""
        if owner == self.owner:
            return
        self.owner = owner
        self._ids.clear()

    def remember(self, notification_id: int, thread: str, address: str, label: str) -> None:
        nid = _notification_id(notification_id)
        if nid is None:
            return
        self._ids.pop(nid, None)
        self._ids[nid] = {
            "thread": _clip(thread, 240),
            "address": _clip(address, 200),
            "label": _clip(label, 200),
        }
        while len(self._ids) > _POPUP_LIMIT:
            self._ids.pop(next(iter(self._ids)))

    def on_closed(self, sender: str, notification_id: int) -> None:
        if not self.owner or sender != self.owner:
            return
        nid = _notification_id(notification_id)
        if nid is None:
            return
        self._ids.pop(nid, None)

    def on_action(self, sender: str, notification_id: int, action: str) -> dict | None:
        if not self.owner or sender != self.owner or action != "default":
            return None
        nid = _notification_id(notification_id)
        if nid is None:
            return None
        found = self._ids.get(nid)
        if not found:
            return None
        return dict(found)


class TextPopups:
    """Desktop pop-ups for new texts, and the click that opens one conversation."""

    def __init__(self, emit):
        self._emit = emit
        self.clicks = PopupClicks()
        self._bus = None
        self._in_flight = 0
        self._MessageType = None

    async def attach(self, bus) -> None:
        """Listen on the texts session bus. A missing bus means no pop-ups."""
        if bus is None or self._bus is not None:
            return
        try:
            _message, _variant, self._MessageType = _dbus_types()
        except Exception:
            return
        self._bus = bus
        try:
            bus.add_message_handler(self._on_message)
        except Exception:
            pass
        try:
            await self._subscribe()
        except Exception:
            return

    async def notify(
        self, title: str, body: str, thread: str = "", address: str = "", label: str = ""
    ) -> None:
        """Show one pop-up. A D-Bus failure shows nothing and does not raise."""
        if self._bus is None or self._in_flight >= _NOTIFY_LIMIT:
            return
        self._in_flight += 1
        try:
            reply = await self._call(notification_message(title, body))
            self._remember(reply, thread, address, label)
        except Exception:
            return
        finally:
            self._in_flight -= 1

    async def _subscribe(self) -> None:
        for rule in (_MATCH_ACTION, _MATCH_CLOSED, _MATCH_OWNER):
            try:
                await self._call(self._bus_message("AddMatch", "s", [rule]))
            except Exception:
                continue
        try:
            reply = await self._call(self._bus_message("GetNameOwner", "s", [_NOTIFY_NAME]))
        except Exception:
            return
        self._take_owner(reply)

    def _bus_message(self, member: str, signature: str, body: list):
        Message, _variant, _message_type = _dbus_types()
        return Message(
            destination=_BUS_NAME,
            path=_BUS_PATH,
            interface=_BUS_NAME,
            member=member,
            signature=signature,
            body=body,
        )

    async def _call(self, msg):
        return await asyncio.wait_for(self._bus.call(msg), _NOTIFY_TIMEOUT)

    def _take_owner(self, reply) -> None:
        if reply is None or reply.message_type != self._MessageType.METHOD_RETURN:
            return
        if reply.sender != _BUS_NAME or reply.signature != "s" or not reply.body:
            return
        owner = _unique_name(reply.body[0])
        if owner:
            self.clicks.set_owner(owner)

    def _remember(self, reply, thread: str, address: str, label: str) -> None:
        if reply is None or reply.message_type != self._MessageType.METHOD_RETURN:
            return
        if reply.signature != "u" or not reply.body:
            return
        sender = _unique_name(reply.sender)
        if not sender:
            return
        # The bus routed Notify to the current owner. Learn that name only
        # when GetNameOwner has not already told us who it is.
        if not self.clicks.owner:
            self.clicks.set_owner(sender)
        if sender != self.clicks.owner:
            return
        nid = _notification_id(reply.body[0])
        if nid is None:
            return
        self.clicks.remember(nid, thread, address, label)

    def _on_message(self, msg):
        try:
            if self._MessageType is None or msg.message_type != self._MessageType.SIGNAL:
                return None
            self._on_signal(msg)
        except Exception:
            return None
        return None

    def _on_signal(self, msg) -> None:
        if (
            msg.member == "NameOwnerChanged"
            and msg.interface == _BUS_NAME
            and msg.path == _BUS_PATH
            and msg.sender == _BUS_NAME
        ):
            self._name_owner_changed(msg)
            return
        if msg.interface != _NOTIFY_NAME or msg.path != _NOTIFY_PATH:
            return
        if not self.clicks.owner or msg.sender != self.clicks.owner:
            return
        if msg.member == "ActionInvoked":
            self._action_invoked(msg)
        elif msg.member == "NotificationClosed":
            self._notification_closed(msg)

    def _name_owner_changed(self, msg) -> None:
        if msg.signature != "sss" or len(msg.body) != 3:
            return
        name, _old, new = msg.body
        if name != _NOTIFY_NAME or not isinstance(new, str):
            return
        if new == "":
            self.clicks.set_owner("")
            return
        owner = _unique_name(new)
        if owner:
            self.clicks.set_owner(owner)

    def _action_invoked(self, msg) -> None:
        if msg.signature != "us" or len(msg.body) != 2:
            return
        action = msg.body[1]
        if not isinstance(action, str):
            return
        opened = self.clicks.on_action(msg.sender, msg.body[0], action)
        if not opened:
            return
        self._emit(
            {
                "event": "open-thread",
                "thread": opened["thread"],
                "address": opened["address"],
                "label": opened["label"],
            }
        )

    def _notification_closed(self, msg) -> None:
        if msg.signature != "uu" or not msg.body:
            return
        self.clicks.on_closed(msg.sender, msg.body[0])


def _clear_phone_alerts(store, thread: str, address: str = "") -> None:
    """The phone still shows a text after it is read here. Clear that person's alert.

    A failure (mirroring off, phone away, service not running) is ignored so
    the Messages card does not show an error for it.
    """
    try:
        from seen import clear_conversation

        clear_conversation(store, thread, address)
    except Exception:
        return


async def handle(raw: str, store: Store, session) -> None:
    try:
        command = json.loads(raw)
    except json.JSONDecodeError:
        emit({"event": "error", "message": "Command was not valid JSON."})
        return
    if not isinstance(command, dict):
        emit({"event": "error", "message": "Command was not an object."})
        return
    name = str(command.get("cmd") or "")
    try:
        await dispatch(name, command, store, session)
    except Exception as exc:
        emit({"event": "error", "message": public_error(exc)})


async def dispatch(name: str, command: dict, store: Store, session) -> None:
    if name == "status":
        if session:
            session.push_status()
        else:
            emit({"event": "status", "status": status_from_store(store, False, "Bluetooth library is missing.")})
        return

    if name == "set-number":
        number = normalize_phone(str(command.get("number") or ""))
        if not number:
            raise ValueError("Enter your number in international form, like +15551234567.")
        store.set_own_number(number)
        if session:
            session.push_status()
        else:
            emit({"event": "status", "status": status_from_store(store, False, "")})
        return

    if name == "set-notify-body":
        store.set_notify_body(bool(command.get("enabled")))
        if session:
            session.push_status()
        else:
            emit({"event": "status", "status": status_from_store(store, False, "")})
        return

    if name == "threads":
        emit({"event": "threads", "threads": store.threads()})
        return

    if name == "messages":
        key = str(command.get("thread") or "")
        emit({"event": "messages", "thread": key, "messages": store.messages_for(key)})
        if session:
            await session.fetch_thread(key)
            emit({"event": "messages", "thread": key, "messages": store.messages_for(key)})
        return

    if name == "contacts":
        query = str(command.get("query") or "")[:80]
        emit({"event": "contacts", "contacts": store.search_contacts(query)})
        return

    if name == "mark-read":
        key = str(command.get("thread") or "")
        if session:
            await session.mark_read(key)
        else:
            store.mark_thread_read(key)
            emit({"event": "threads", "threads": store.threads()})
        _clear_phone_alerts(store, key)
        return

    if session is None:
        raise RuntimeError("Bluetooth messaging is not available yet.")

    if name == "scan":
        await session.scan()
        return

    if name == "reconnect":
        # Opening the card retries message access. The iPhone switch
        # can be flipped while this program is already running.
        await session.reconnect()
        return

    if name == "pair":
        address = str(command.get("address") or "")
        if not _MAC.match(address):
            raise ValueError("Pick a phone from the scan list.")
        await session.pair(address, bool(command.get("explicit")))
        return

    if name == "confirm":
        session.confirm(bool(command.get("accept")))
        return

    if name == "cancel":
        session.confirm(False)
        return

    if name == "send":
        body = str(command.get("body") or "")
        address = str(command.get("address") or "")
        if not address:
            address = address_from_thread(str(command.get("thread") or ""))
        if not address:
            raise ValueError("Choose a contact or a conversation first.")
        await session.send(address, body)
        emit({"event": "sent", "thread": str(command.get("thread") or "")})
        _clear_phone_alerts(store, str(command.get("thread") or ""), address)
        return

    raise ValueError("Unknown command.")


def notifying_emit(store: Store, inner, popups: TextPopups):
    """Wrap emit so a new inbound text can raise a name-only notification."""

    def _emit(event: dict) -> None:
        inner(event)
        if event.get("event") == "status":
            log_state(str(event.get("status", {}).get("state") or ""))
        if event.get("event") != "message" or not event.get("notify"):
            return
        message = event.get("message") or {}
        if not isinstance(message, dict):
            return
        label = message.get("label") if isinstance(message.get("label"), str) else ""
        thread = message.get("thread") if isinstance(message.get("thread"), str) else ""
        title = notification_title(label)
        body = ""
        if store.settings()["notifyBody"]:
            raw_body = message.get("body")
            body = raw_body if isinstance(raw_body, str) else ""
        # openMessages takes the number or email that address_from_thread pulls out of the key.
        spawn(popups.notify(title, body, thread, address_from_thread(thread), label))

    return _emit


async def amain_wrapped() -> None:
    store = Store()
    popups = TextPopups(emit)
    session = None
    detail = ""
    emit_fn = notifying_emit(store, emit, popups)
    try:
        from bluetooth import BluetoothSession

        session = BluetoothSession(store, emit_fn)
        await session.start()
    except Exception as exc:
        detail = public_error(exc)
        print("bluetooth unavailable", file=sys.stderr, flush=True)
        session = None
    else:
        # The texts session bus is already up. A pop-up failure must not
        # take messaging down with it.
        try:
            await popups.attach(session._session_bus)
        except Exception:
            pass
    if session is None:
        ready = status_from_store(store, False, detail or "Bluetooth messaging is not available yet.")
        emit({"event": "ready", "status": ready})
        log_state(ready["state"])
    else:
        session.push_status()
        log_state(session.state)
    emit({"event": "threads", "threads": store.threads()})

    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    await loop.connect_read_pipe(lambda: protocol, sys.stdin)
    # Pairing waits until the overlay answers. That answer has to be read
    # while pair() is still running, so commands are scheduled as tasks
    # and confirm/cancel are applied immediately.
    while True:
        line = await reader.readline()
        if not line:
            break
        text = line.decode("utf-8", errors="replace").strip()
        if not text:
            continue
        if _is_immediate(text, session):
            continue
        spawn(handle(text, store, session))
    store.close()


def _is_immediate(text: str, session) -> bool:
    """Apply pairing answers without waiting for the pairing task to finish."""
    try:
        command = json.loads(text)
    except json.JSONDecodeError:
        return False
    if not isinstance(command, dict):
        return False
    name = str(command.get("cmd") or "")
    if name not in ("confirm", "cancel"):
        return False
    if session is None:
        emit({"event": "error", "message": "Bluetooth is not running."})
        return True
    session.confirm(name == "confirm" and bool(command.get("accept")))
    return True


def main() -> None:
    try:
        asyncio.run(amain_wrapped())
    except KeyboardInterrupt:
        return


if __name__ == "__main__":
    main()
