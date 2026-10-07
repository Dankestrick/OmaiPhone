"""Holds the phone's notification and media services and mirrors its notifications.

The iPhone allows one ATT channel per computer, and bluetoothd already holds
it (BlueZ runs a GATT client on the link). So the background service talks to
Apple's notification and media services through BlueZ's GATT objects
(gatt.BluezGatt) and everything else (panel, CLI) talks to the service.
"""

import re

from gi.repository import Gio, GLib

from . import gatt, notifications, rawatt
from .audio import load_config
from .companion import Session

RETRY_SECONDS = (5, 15, 30, 60, 120, 300)
RESOLVE_RETRY_SECONDS = 4        # BlueZ is still reading the phone's services
TICK_SECONDS = 3
PERMISSION_RETRY_TICKS = 10      # ask again every 30 s while the phone says "not allowed"
PUBLISH_DELAY_MS = 150

NOTIFY = "org.freedesktop.Notifications"
NOTIFY_PATH = "/org/freedesktop/Notifications"


def system_bus():
    return Gio.bus_get_sync(Gio.BusType.SYSTEM, None)


class NotReady(Exception):
    """The phone is connected but BlueZ has not read its services yet."""


class Toaster:
    """Desktop notifications for mirrored ones, one per phone notification."""

    def __init__(self, bus):
        self.bus = bus
        self.ids = {}   # phone uid -> desktop notification id

    def show(self, notification):
        app, summary, body = notifications.toast_text(notification)
        uid = notification["uid"]
        hints = {"urgency": GLib.Variant("y", 1), "category": GLib.Variant("s", "im.received"),
                 "desktop-entry": GLib.Variant("s", "omaiphone")}
        try:
            result = self.bus.call_sync(
                NOTIFY, NOTIFY_PATH, NOTIFY, "Notify",
                GLib.Variant("(susssasa{sv}i)", (app, self.ids.get(uid, 0), "phone", summary, body, [], hints, -1)),
                GLib.VariantType("(u)"), 0, 5000, None)
            self.ids[uid] = result.unpack()[0]
        except GLib.Error:
            pass   # no notification daemon: the panel still lists it

    def close(self, uid):
        desktop_id = self.ids.pop(uid, 0)
        if not desktop_id:
            return
        try:
            self.bus.call_sync(NOTIFY, NOTIFY_PATH, NOTIFY, "CloseNotification", GLib.Variant("(u)", (desktop_id,)),
                               None, 0, 5000, None)
        except GLib.Error:
            pass

    def forget(self):
        self.ids = {}


class Mirror:
    def __init__(self, toaster, log=print, bus_factory=system_bus, config_loader=load_config,
                 state_path=notifications.STATE_FILE, client_factory=None):
        self.toaster = toaster
        self.log = log
        self.bus_factory = bus_factory
        # client_factory(address, on_lost) -> GATT client; tests pass a simulated phone.
        self.client_factory = client_factory or self._bluez_client
        self.config_loader = config_loader
        self.state_path = state_path
        self.config = config_loader()
        self.store = notifications.Store()
        self.address = None
        self.client = None
        self.session = None
        self.state = "off"
        self.detail = ""
        self._device_watch = 0
        self._ticker = 0
        self._retry_timer = 0
        self._publish_timer = 0
        self._attempt = 0
        self._ticks = 0
        self._bus = None

    # ---- what the service tells the mirror ------------------------------------------

    def phone_connected(self, address):
        self.address = address if re.fullmatch(r"(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}", address or "") else None
        self.refresh()

    def phone_disconnected(self):
        self.address = None
        self._close()
        self._cancel_retry()
        self._set_state("waiting", "the iPhone is not connected")

    def refresh(self):
        """Apply the configuration: called at start and whenever it changes."""
        self.config = self.config_loader()
        if not self.enabled:
            self._close()
            self._cancel_retry()
            self.toaster.forget()
            self._set_state("off")
        elif self.client is None and not self._retry_timer:
            if self.address:
                self._connect()
            else:
                self._set_state("waiting", "the iPhone is not connected")
        self._publish()

    def stop(self):
        self._close()
        self._cancel_retry()
        if self._publish_timer:
            GLib.source_remove(self._publish_timer)
            self._publish_timer = 0
        notifications.remove_state(self.state_path)

    @property
    def enabled(self):
        return notifications.mirror_enabled(self.config)

    # ---- actions ----------------------------------------------------------------------

    def dismiss(self, uid):
        return bool(self.session and self.session.dismiss(uid))

    def media(self, command):
        return bool(self.session and self.session.media_command(command))

    # ---- the services, through BlueZ ---------------------------------------------------

    @property
    def bus(self):
        if self._bus is None:
            self._bus = self.bus_factory()
        return self._bus

    def _connect(self):
        self._cancel_retry()
        try:
            self.client = self.client_factory(self.address, self._lost)
        except NotReady:
            # BlueZ reads the phone's services right after it connects; try again shortly.
            self._set_state("connecting")
            self._retry_timer = GLib.timeout_add_seconds(RESOLVE_RETRY_SECONDS, self._again)
            return
        except (LookupError, GLib.Error, OSError) as error:
            return self._lost(getattr(error, "message", None) or str(error))
        if getattr(self.client, "connecting", False):
            self._set_state("connecting")
            self.client.on_ready = self._start_session
        else:
            self._start_session()

    def _start_session(self):
        self.session = Session(lambda _packet: None, on_state=self._session_state, on_notification=self._notification,
                               on_removed=self._removed, on_media=lambda _media: self._publish(), client=self.client)
        self._ticks = 0
        self._ticker = GLib.timeout_add_seconds(TICK_SECONDS, self._tick)
        self._set_state("discovering")
        self.session.start()

    def _bluez_client(self, address, on_lost):
        path = gatt.device_path(self.bus, address)
        if not path:
            raise LookupError("BlueZ does not know the iPhone")
        props = gatt.device_props(self.bus, path)
        if not props.get("Connected"):
            raise LookupError("the iPhone is not connected")
        if not props.get("ServicesResolved"):
            raise NotReady()
        self._watch_device(path, on_lost)
        if gatt.has_services(self.bus, path):
            return gatt.BluezGatt(self.bus, path)
        # bluetoothd has no GATT client on this link: open the ATT channel ourselves.
        return rawatt.RawAtt(address, on_lost)

    def _again(self):
        self._retry_timer = 0
        if self.enabled and self.address and self.client is None:
            self._connect()
        return False

    def _watch_device(self, path, on_lost):
        def changed(_conn, _sender, _path, _iface, _member, params, _data):
            interface, props, _invalid = params.unpack()
            if interface != gatt.DEVICE_IFACE or self.client is None:
                return
            if props.get("Connected") is False or props.get("ServicesResolved") is False:
                on_lost("the iPhone disconnected")

        self._device_watch = self.bus.signal_subscribe(
            gatt.BLUEZ, gatt.PROPS_IFACE, "PropertiesChanged", path, None, Gio.DBusSignalFlags.NONE, changed, None)

    def _tick(self):
        if self.session is None:
            self._ticker = 0
            return False
        self._ticks += 1
        if self.session.state == "needs-permission" and self._ticks % PERMISSION_RETRY_TICKS == 0:
            self.session.retry()
        if not self.session.tick():
            self._ticker = 0
            self._lost(self.session.detail)
            return False
        return True

    def _close(self):
        if self._ticker:
            GLib.source_remove(self._ticker)
            self._ticker = 0
        if self._device_watch:
            self._bus.signal_unsubscribe(self._device_watch)
            self._device_watch = 0
        if self.session:
            self.session.close()
            self.session = None
        if self.client is not None:
            self.client.close()
            self.client = None
        self.store.clear()

    def _cancel_retry(self):
        if self._retry_timer:
            GLib.source_remove(self._retry_timer)
            self._retry_timer = 0

    def _lost(self, reason):
        self._close()
        if not self.enabled or not self.address:
            return
        delay = RETRY_SECONDS[min(self._attempt, len(RETRY_SECONDS) - 1)]
        self._attempt += 1
        self.log(f"notification link lost ({reason}); next try in {delay} s")
        self._set_state("waiting", reason)
        self._cancel_retry()
        self._retry_timer = GLib.timeout_add_seconds(delay, self._again)

    # ---- what the session reports -------------------------------------------------------

    def _session_state(self, state, detail):
        if state == "failed":
            return   # _tick or _readable turn this into a retry
        if state == "ready":
            self._attempt = 0
            self.log("mirroring the iPhone's notifications")
        elif state in ("needs-permission", "unsupported"):
            self.log(f"notifications not available: {detail}")
        self._set_state(state, detail)

    def _notification(self, notification):
        self.store.apply(notification)
        if notifications.should_toast(notification, self.config):
            self.toaster.show(notification)
        self._publish()

    def _removed(self, uid):
        if self.store.remove(uid):
            self._publish()
        self.toaster.close(uid)

    # ---- state for the panel and the CLI ------------------------------------------------

    def _set_state(self, state, detail=""):
        if (state, detail) != (self.state, self.detail):
            self.state, self.detail = state, detail
            self._publish()

    def snapshot(self):
        media = self.session.media if self.session else None
        return notifications.snapshot(self.enabled, self.state, self.detail, self.store, media)

    def _publish(self):
        if self._publish_timer:
            return

        def write():
            self._publish_timer = 0
            try:
                notifications.write_state(self.snapshot(), self.state_path)
            except OSError as error:
                self.log(f"could not write the mirror state: {error}")
            return False

        self._publish_timer = GLib.timeout_add(PUBLISH_DELAY_MS, write)
