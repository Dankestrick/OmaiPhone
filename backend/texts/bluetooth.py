"""Talk to a paired iPhone through BlueZ.

MAP (Message Access Profile) carries the texts. PBAP (Phone Book Access
Profile) carries the contacts. Both are public Bluetooth profiles. This
file never speaks Apple's private iMessage protocol and never sees an
Apple ID password.

The numeric comparison shown during pairing is a secret for that moment.
It is sent to the overlay and is not written to the log.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path

from dbus_next import BusType, Variant
from dbus_next.aio import MessageBus
from dbus_next.constants import PropertyAccess
from dbus_next.errors import DBusError
from dbus_next.service import ServiceInterface, dbus_property, method

from protocol import (
    address_from_thread,
    classify_recipient,
    coerce_phone,
    extract_bmessage_peer,
    extract_bmessage_text,
    normalize_email,
    present_message,
    public_error,
)
from store import Store, runtime_dir, shared_contacts_file

# iOS shows "Show Message Notifications" after it sees this ANCS service
# solicited. We advertise the UUID only. We do not subscribe to it, so
# other apps' notifications are never read.
_ANCS_UUID = "7905f431-b5ce-4e99-a40f-4b1e122d00d0"
# Failed downloads of one message before it is left as a preview.
_MAX_DOWNLOAD_TRIES = 3
# How many message handles to remember. A phone can announce a new handle
# forever; the dicts must not grow with it.
_MAX_TRACKED_HANDLES = 500
# A text is a few kilobytes. Anything larger is not stored or parsed.
_MAX_BMESSAGE_BYTES = 128 * 1024
# MAP handles are hex, at most 16 digits. Anything else must not become a path.
_HANDLE_TOKEN = re.compile(r"^[0-9A-Fa-f]{1,16}$")
# ListMessages is asked for 40. A phone that ignores that must not make us
# download the whole inbox or keep every row.
_MAX_LISTING = 40
# BlueZ object paths are short. A longer "path" is not remembered.
_MAX_OBJECT_PATH = 240
# The shared phone book is written by us, but only after a download. Refuse
# to parse a file that has grown past the download cap.
_MAX_CONTACTS_FILE = 2 * 1024 * 1024
_OBEX_CANDIDATES = (
    "/usr/lib/bluetooth/obexd",
    "/usr/libexec/bluetooth/obexd",
    "obexd",
)


def _unwrap(value):
    """dbus-next wraps property values in Variant. This returns the inner value."""
    if isinstance(value, Variant):
        return value.value
    return value


def _props_of(interfaces: dict, name: str) -> dict:
    raw = interfaces.get(name) or {}
    return {key: _unwrap(val) for key, val in raw.items()}


def _handle_token(value) -> str:
    """A MAP handle, or "" when the phone's value is not a short hex token.

    The token is used as a file name and a database key. A long string is
    rejected before it is copied or hashed.
    """
    if isinstance(value, bool) or value is None:
        return ""
    if isinstance(value, int):
        if value < 0 or value.bit_length() > 64:
            return ""
        text = str(value)
    elif isinstance(value, str):
        text = value
    else:
        return ""
    if len(text) > 16 or not _HANDLE_TOKEN.fullmatch(text):
        return ""
    return text


def _handle_from(path: str, fields: dict) -> str:
    """MAP handle for one text.

    BlueZ puts the handle in the object name, like .../message42.
    The property list often does not include it. A Handle property that is
    not a short hex token is ignored, so it cannot replace the object name.
    """
    handle = _handle_token(fields.get("Handle"))
    if handle:
        return handle
    if not isinstance(path, str):
        return ""
    match = re.search(r"message([0-9A-Fa-f]{1,16})$", path[-80:])
    return match.group(1) if match else ""


def _listing_entries(listing) -> list[tuple[str, dict]]:
    """Turn a ListMessages result into (object path, properties) rows.

    BlueZ returns a dictionary whose keys are object paths. dbus-next
    hands that back as pairs, not as a flat property bag.
    """
    if isinstance(listing, dict):
        pairs = listing.items()
    else:
        pairs = listing or []
    rows = []
    for entry in pairs:
        if len(rows) >= _MAX_LISTING:
            break
        if isinstance(entry, (list, tuple)) and len(entry) == 2 and isinstance(entry[1], dict):
            path, props = entry
            if not isinstance(path, str):
                continue
            if len(path) > _MAX_OBJECT_PATH:
                continue
            rows.append((path, {key: _unwrap(val) for key, val in props.items()}))
        elif isinstance(entry, dict):
            path = entry.get("Path") or ""
            if not isinstance(path, str) or len(path) > _MAX_OBJECT_PATH:
                continue
            rows.append((path, {key: _unwrap(val) for key, val in entry.items()}))
    return rows


def incoming_bmessage_path(root: Path, handle: str) -> Path | None:
    """Path for one downloaded text, or None when the handle is not a safe file name.

    The handle comes from the phone. It is only a name inside `root`, never a
    path: `../`, an absolute path, or a too-long token would make obexd write
    (and us delete) a file outside the runtime directory.
    """
    if not isinstance(handle, str) or not _HANDLE_TOKEN.fullmatch(handle):
        return None
    directory = Path(root)
    path = directory / f"incoming-{handle}.bmsg"
    if path.parent != directory:
        return None
    return path


def prepare_download_file(path: Path) -> None:
    """Create `path` as a regular file, mode 0600. Refuse to follow a symlink."""
    fd = os.open(path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    os.close(fd)


def read_bounded_text(path: Path, limit: int = _MAX_BMESSAGE_BYTES) -> str:
    """Read a downloaded text, or "" when it is larger than `limit` bytes."""
    with open(path, "rb") as handle:
        blob = handle.read(limit + 1)
    if len(blob) > limit:
        return ""
    return blob.decode("utf-8", errors="replace")


def _map_time(value: str) -> str:
    """MAP timestamps look like 20261002T103000. Store a sortable ISO string."""
    if not isinstance(value, str):
        return Store.now_stamp()
    digits = re.sub(r"\D", "", value[:32])
    if len(digits) < 14:
        return Store.now_stamp()
    return f"{digits[0:4]}-{digits[4:6]}-{digits[6:8]}T{digits[8:10]}:{digits[10:12]}:{digits[12:14]}"


def _read_adapter_class() -> tuple[int, int] | None:
    """Current major/minor device class, so pairing can put the old one back."""
    try:
        result = subprocess.run(
            ["btmgmt", "--timeout", "3", "info"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    match = re.search(r"class (0x[0-9a-fA-F]+)", result.stdout or "")
    if not match:
        return None
    value = int(match.group(1), 16)
    minor = (value >> 2) & 0x3F
    major = (value >> 8) & 0x1F
    return major, minor


def _set_adapter_class(major: int, minor: int) -> bool:
    """Ask BlueZ to present this computer as a given device class. No sudo."""
    try:
        result = subprocess.run(
            ["btmgmt", "--timeout", "3", "class", str(major), str(minor)],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


class PairAgent(ServiceInterface):
    """BlueZ pairing agent. It shows a 6-digit code and waits for the overlay."""

    def __init__(self, on_code):
        super().__init__("org.bluez.Agent1")
        self._on_code = on_code
        self._pending: asyncio.Future | None = None

    def answer(self, accept: bool) -> None:
        pending = self._pending
        self._pending = None
        if pending is None or pending.done():
            return
        if accept:
            pending.set_result(None)
        else:
            pending.set_exception(DBusError("org.bluez.Error.Rejected", "user rejected"))

    def _begin_wait(self, code: str) -> asyncio.Future:
        self.answer(False) if self._pending else None
        loop = asyncio.get_running_loop()
        self._pending = loop.create_future()
        self._on_code(code)
        return self._pending

    @method()
    def Release(self):
        self.answer(False)

    @method()
    def Cancel(self):
        self.answer(False)

    @method()
    def RequestPinCode(self, device: "o") -> "s":
        raise DBusError("org.bluez.Error.Rejected", "numeric comparison only")

    @method()
    def DisplayPinCode(self, device: "o", pincode: "s"):
        self._on_code(str(pincode))

    @method()
    def RequestPasskey(self, device: "o") -> "u":
        raise DBusError("org.bluez.Error.Rejected", "numeric comparison only")

    @method()
    def DisplayPasskey(self, device: "o", passkey: "u", entered: "q"):
        self._on_code(f"{int(passkey):06d}")

    @method()
    async def RequestConfirmation(self, device: "o", passkey: "u"):
        try:
            await asyncio.wait_for(self._begin_wait(f"{int(passkey):06d}"), timeout=90)
        except asyncio.TimeoutError as exc:
            raise DBusError("org.bluez.Error.Rejected", "timed out") from exc

    @method()
    async def RequestAuthorization(self, device: "o"):
        try:
            await asyncio.wait_for(self._begin_wait("YES"), timeout=90)
        except asyncio.TimeoutError as exc:
            raise DBusError("org.bluez.Error.Rejected", "timed out") from exc

    @method()
    def AuthorizeService(self, device: "o", uuid: "s"):
        # Registered only while the user is pairing. MAP and PBAP are the
        # profiles this plugin uses. Rejecting unknown UUIDs here would also
        # reject the phone's own handshake, so the user's confirmation above
        # is the gate.
        return

    def _reject_if_waiting(self):
        self.answer(False)


class SolicitationAdvert(ServiceInterface):
    """A short Bluetooth announcement that asks iOS to show the message switches.

    It solicits the ANCS UUID and nothing else. There is no GATT subscription,
    so notification contents from other apps are never delivered here.
    """

    def __init__(self):
        super().__init__("org.bluez.LEAdvertisement1")

    @method()
    def Release(self):
        return

    @dbus_property(access=PropertyAccess.READ)
    def Type(self) -> "s":
        return "peripheral"

    @dbus_property(access=PropertyAccess.READ)
    def SolicitUUIDs(self) -> "as":
        return [_ANCS_UUID]

    @dbus_property(access=PropertyAccess.READ)
    def Discoverable(self) -> "b":
        return True

    @dbus_property(access=PropertyAccess.READ)
    def ManufacturerData(self) -> "a{qv}":
        # 0xffff is the Bluetooth test manufacturer id, not an Apple id.
        return {0xFFFF: Variant("ay", bytes([0x00]))}


class BluetoothSession:
    """One user's Bluetooth session: scan, pair, read, and send."""

    def __init__(self, store: Store, emit):
        self.store = store
        self.emit = emit
        self.state = "not_paired"
        self.detail = ""
        self.pair_fallback = False
        self.ready = False
        self._system: MessageBus | None = None
        self._session_bus: MessageBus | None = None
        self._agent: PairAgent | None = None
        self._agent_on = False
        self._advert_on = False
        self._saved_class: tuple[int, int] | None = None
        self._class_changed = False
        self._map_path = ""
        self._contacts_mtime = 0.0
        self._message_paths: dict[str, str] = {}
        # Downloads that came back empty or failed, per handle. A message the
        # phone will not hand over (picture-only, deleted) would otherwise be
        # fetched again every two seconds for as long as the link is up.
        self._download_failures: dict[str, int] = {}
        self._obex_proc: subprocess.Popen | None = None
        self._announce_live = False
        self._listening = False
        self._contacts_watching = False
        # These must stay referenced. If Python collects them, new-text
        # alerts from the phone are silently dropped.
        self._obex_root = None
        self._obex_manager = None
        self._tasks: set[asyncio.Task] = set()
        self.lock = asyncio.Lock()

    def _track(self, coro) -> asyncio.Task:
        """Keep a background task alive until it finishes."""
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def start(self) -> None:
        self._system = await MessageBus(bus_type=BusType.SYSTEM).connect()
        self._session_bus = await MessageBus(bus_type=BusType.SESSION).connect()
        self._agent = PairAgent(self._show_code)
        self._system.export("/io/github/dankestrick/omaiphone/texts/Agent", self._agent)
        self._system.export("/io/github/dankestrick/omaiphone/texts/Advert", SolicitationAdvert())
        self.ready = True
        # A phone paired in the system Bluetooth settings is already known.
        # Show it immediately instead of waiting for a new scan.
        await self._adopt_paired_iphone()
        await self._refresh_presence()
        self.push_status()
        if self.store.settings()["phoneAddress"]:
            self._track(self._safe_reconnect())

    async def reconnect(self) -> None:
        """Try the message link again. Used after the iPhone switches are turned on."""
        await self._safe_reconnect()

    async def _safe_reconnect(self) -> None:
        try:
            async with self.lock:
                await self._connect_saved()
        except Exception as exc:
            self.detail = public_error(exc)
            self.push_status()

    def push_status(self) -> None:
        settings = self.store.settings()
        if not settings["phoneAddress"] and self.state not in ("pairing", "scanning"):
            self.state = "not_paired"
        self.emit(
            {
                "event": "status",
                "status": {
                    "state": self.state,
                    "phoneName": settings["phoneName"],
                    "phoneAddress": settings["phoneAddress"],
                    "ownNumber": settings["ownNumber"],
                    "unread": self.store.unread_count(),
                    "notifyBody": settings["notifyBody"],
                    "detail": self.detail,
                    "pairFallback": self.pair_fallback,
                    "bluetoothReady": self.ready,
                },
            }
        )

    def _show_code(self, code: str) -> None:
        self.state = "pairing"
        self.detail = "Confirm this code on the iPhone too."
        self.emit({"event": "pair-code", "code": str(code)})
        self.push_status()

    def confirm(self, accept: bool) -> None:
        if self._agent:
            self._agent.answer(accept)

    async def _adopt_paired_iphone(self) -> None:
        """Remember an iPhone that BlueZ already has paired, and tell the overlay."""
        devices = await self._phone_dicts()
        self.emit({"event": "devices", "devices": devices})
        if not devices:
            return
        saved = self.store.settings()["phoneAddress"].upper()
        chosen = None
        for device in devices:
            if device["address"].upper() == saved:
                chosen = device
                break
        if chosen is None:
            for device in devices:
                if device.get("connected"):
                    chosen = device
                    break
            chosen = chosen or devices[0]
            self.store.set_phone(chosen["address"], chosen["name"] or "iPhone")
        # "connected" is reserved for a live message session. A Bluetooth
        # link alone means the phone is nearby and can be opened.
        self.state = "phone_nearby"
        self.detail = "iPhone is paired."

    async def scan(self) -> None:
        async with self.lock:
            self.state = "scanning"
            self.detail = "Looking for nearby phones."
            self.push_status()
            try:
                # Paired phones are already in BlueZ. Publish them before
                # the discovery wait, or a connected iPhone looks missing.
                await self._adopt_paired_iphone()
                self.push_status()
                adapter = await self._adapter()
                try:
                    await adapter.call_set_discovery_filter({"Transport": Variant("s", "bredr")})
                except DBusError:
                    pass
                try:
                    await adapter.call_start_discovery()
                except DBusError:
                    # Already discovering is fine. The list below still updates.
                    pass
                await asyncio.sleep(8)
                try:
                    await adapter.call_stop_discovery()
                except DBusError:
                    pass
                await self._adopt_paired_iphone()
                if not self.store.settings()["phoneAddress"]:
                    self.state = "not_paired"
                    self.detail = "No phone found. Move it closer and scan again."
                else:
                    self.detail = "Select your iPhone."
            except Exception as exc:
                self.detail = public_error(exc)
                self.emit({"event": "error", "message": self.detail})
            self.push_status()

    async def pair(self, address: str, explicit: bool) -> None:
        async with self.lock:
            self.pair_fallback = False
            previous = _read_adapter_class()
            self._saved_class = previous
            # Hands-free class (major 4, minor 8) is what makes iOS offer
            # message and contact switches. Restored after this attempt.
            self._class_changed = _set_adapter_class(4, 8)
            try:
                await self._register_agent()
                device = await self._device(address)
                name = await self._device_name(device)
                try:
                    if explicit:
                        await device.call_pair()
                    else:
                        await device.call_connect()
                except Exception as exc:
                    if explicit:
                        raise
                    self.pair_fallback = True
                    self.state = "not_paired"
                    self.detail = "Connect-first pairing stopped. Press p for explicit pairing. That fallback can bond Classic Bluetooth only."
                    self.emit({"event": "error", "message": public_error(exc)})
                    self.push_status()
                    return
                try:
                    await device.set_trusted(True)
                except Exception:
                    pass
                self.store.set_phone(address, name)
                self.detail = "Paired. Waiting for the iPhone switches."
                self.state = "waiting_for_switches"
                self.push_status()
                await self._start_advert()
                await self._open_profiles(address, announce=False)
            except Exception as exc:
                self.detail = public_error(exc)
                self.state = "not_paired"
                self.emit({"event": "error", "message": self.detail})
                self.push_status()
            finally:
                await self._unregister_agent()
                self._restore_class_later()

    def _restore_class_later(self) -> None:
        """Put the adapter class back once iOS has had a moment to show the switches."""

        async def _restore():
            await asyncio.sleep(120)
            await self._stop_advert()
            if self._class_changed and self._saved_class:
                major, minor = self._saved_class
                _set_adapter_class(major, minor)
                self._class_changed = False

        self._track(_restore())

    async def send(self, address: str, body: str) -> None:
        async with self.lock:
            if not self._map_path:
                raise RuntimeError("The phone is not connected for messages yet.")
            own = self.store.settings()["ownNumber"]
            kind, value = classify_recipient(address, own)
            # build happens in the caller via protocol so a bad body fails
            # before we touch Bluetooth. Re-check here as the gate.
            from protocol import build_bmessage

            payload = build_bmessage(value if kind == "email" else value, body, own)
            folder = runtime_dir()
            fd, name = tempfile.mkstemp(prefix="outgoing-", suffix=".bmsg", dir=folder)
            path = Path(name)
            try:
                os.write(fd, payload.encode("utf-8"))
            finally:
                os.close(fd)
            try:
                access = await self._iface("org.bluez.obex", self._map_path, "org.bluez.obex.MessageAccess1", self._session_bus)
                await access.call_push_message(str(path), "telecom/msg/outbox", {})
            finally:
                try:
                    path.unlink()
                except OSError:
                    pass
            label = self.store.label_for(value)
            self.store.upsert_message(
                external_id=f"local-{time.time_ns()}",
                address=value,
                body=body,
                direction="out",
                timestamp=Store.now_stamp(),
                read=True,
                has_attachment=False,
                body_complete=True,
                peer_label=label,
            )
            self._emit_threads()

    async def mark_read(self, thread: str) -> None:
        handles = self.store.mark_thread_read(thread)
        self._emit_threads()
        self.push_status()
        if not self._map_path:
            return
        for handle in handles:
            path = self._message_paths.get(handle)
            if not path:
                continue
            try:
                message = await self._iface("org.bluez.obex", path, "org.bluez.obex.Message1", self._session_bus)
                await message.set_read(True)
            except Exception:
                # Local read state still stands when the phone does not accept the update.
                continue

    async def fetch_thread(self, thread: str) -> None:
        """Download full text for messages we have only seen as a preview."""
        if not self._map_path:
            return
        async with self.lock:
            for handle in self.store.incomplete_in_thread(thread):
                if self._gave_up(handle):
                    continue
                await self._download_body(handle)

    async def _in_call(self) -> bool:
        """True while PipeWire's telephony service lists a call (the calls part of OmaiPhone)."""
        try:
            manager = await self._iface("org.pipewire.Telephony", "/org/pipewire/Telephony",
                                        "org.freedesktop.DBus.ObjectManager", self._session_bus)
            objects = await manager.call_get_managed_objects()
        except Exception:
            return False
        return any("org.pipewire.Telephony.Call1" in ifaces for ifaces in objects.values())

    async def _connect_saved(self) -> None:
        settings = self.store.settings()
        address = settings["phoneAddress"]
        if not address:
            return
        self.state = "phone_nearby"
        self.detail = "Reconnecting to the paired iPhone."
        self.push_status()
        device = await self._device(address)
        try:
            connected = await device.get_connected()
        except Exception:
            connected = False
        if not connected:
            await device.call_connect()
        await self._open_profiles(address, announce=False)

    async def _reset_phone_link(self, address: str) -> None:
        """Drop the Bluetooth link and bring it back.

        After the iPhone switch is turned on, the old link can still be
        missing the message service. A fresh connection makes the phone
        advertise it again.
        """
        if await self._in_call():
            return   # dropping the link now would end the phone call
        device = await self._device(address)
        try:
            await device.call_disconnect()
        except Exception:
            pass
        await asyncio.sleep(1.2)
        await device.call_connect()
        for _ in range(8):
            try:
                if await device.get_connected():
                    return
            except Exception:
                pass
            await asyncio.sleep(0.4)

    async def _open_profiles(self, address: str, announce: bool, recover: bool = True) -> None:
        await self._ensure_obexd()
        await self._close_profiles()
        client = await self._iface("org.bluez.obex", "/org/bluez/obex", "org.bluez.obex.Client1", self._session_bus)
        try:
            self._map_path = await client.call_create_session(address, {"Target": Variant("s", "map")})
        except Exception as exc:
            text = str(exc).lower()
            # One retry after a link reset. A second failure is shown as-is.
            if recover and ("service record" in text or "0x43" in text or "busy" in text or "forbidden" in text):
                await self._reset_phone_link(address)
                await self._open_profiles(address, announce, recover=False)
                return
            message = public_error(exc)
            if "not allowed" in message or "forbidden" in text or "0x43" in text:
                self.state = "waiting_for_switches"
            else:
                self.state = "phone_nearby"
            self.detail = message
            self.push_status()
            return
        self._listen_for_messages()
        await self._sync_inbox(announce)
        # The calls part of OmaiPhone downloads the phone book (PBAP) once for both.
        if not self._load_shared_contacts():
            # Contacts are optional. Texts still work when Sync Contacts is off.
            self.detail = "Connected. Turn on Sync Contacts on the iPhone to pick people by name."
        self.state = "connected"
        if not self.detail.startswith("Connected. Turn on"):
            # iOS sends an empty inbox on purpose. Show Notifications shares
            # texts that arrive from now on. It does not copy old conversations.
            self.detail = "Connected. New texts show up here. Older ones stay on the iPhone."
        self._announce_live = True
        await self._stop_advert()
        self._emit_threads()
        # People you have already texted are listed even when the phone
        # book download is still empty.
        self.emit({"event": "contacts", "contacts": self.store.search_contacts("")})
        self.push_status()
        if not self._contacts_watching:
            self._contacts_watching = True
            self._track(self._watch_contacts())

    async def _sync_inbox(self, announce: bool) -> None:
        access = await self._iface("org.bluez.obex", self._map_path, "org.bluez.obex.MessageAccess1", self._session_bus)
        try:
            await access.call_update_inbox()
        except Exception:
            pass
        try:
            listing = await access.call_list_messages(
                "telecom/msg/inbox",
                {"MaxCount": Variant("q", 40), "SubjectLength": Variant("y", 255)},
            )
        except Exception:
            listing = await access.call_list_messages("telecom/msg/inbox", {})
        for path, fields in _listing_entries(listing):
            handle = _handle_from(path, fields)
            if path and handle:
                self._remember_handle(handle, path)
            # The listing is a preview. The full text, and often the phone
            # number, are inside the downloaded message.
            self._ingest(fields, path=path, announce=announce)
            if handle:
                await self._download_body(handle, announce=announce, sent=bool(fields.get("Sent")))
        # A new text can already be sitting on the bus as an alert, with
        # no inbox row. Pick those up too.
        await self._collect_live_messages(announce)

    async def _collect_live_messages(self, announce: bool) -> int:
        """Download message alerts BlueZ already has, skipping finished ones."""
        if not self._session_bus or not self._map_path:
            return 0
        saved = 0
        seen = 0
        objects = await self._managed("org.bluez.obex", "/", self._session_bus)
        prefix = self._map_path + "/message"
        for path, interfaces in objects.items():
            if seen >= _MAX_LISTING:
                break
            if not str(path).startswith(prefix):
                continue
            seen += 1
            props = _props_of(interfaces, "org.bluez.obex.Message1")
            if not props:
                continue
            handle = _handle_from(str(path), props)
            if not handle:
                continue
            current = self.store.record(handle)
            if current and current["body_complete"]:
                continue
            if self._gave_up(handle):
                continue
            self._remember_handle(handle, str(path))
            await self._download_body(handle, announce=announce, sent=bool(props.get("Sent")))
            after = self.store.record(handle)
            if after and (current is None or not current["body_complete"]):
                saved += 1
        return saved

    def _ingest(self, fields: dict, path: str, announce: bool) -> tuple[dict, bool] | None:
        handle = _handle_from(path, fields)
        if path and handle:
            self._remember_handle(handle, path)
        sender = str(fields.get("SenderAddress") or fields.get("Sender") or "")
        # A brand-new text often arrives with no sender yet. The download
        # step fills that in. Skipping here would drop the text.
        if not sender:
            return None
        sender_name = str(fields.get("Sender") or "")
        # A phone number is not a display name. Leave the label empty so the
        # contact cache can fill it in, and so a notification is not forced
        # to invent a name from digits.
        label = sender_name if sender_name and sender_name != sender else ""
        subject = str(fields.get("Subject") or "")
        attached = bool(fields.get("Attachment") or fields.get("AttachmentSize"))
        if present_message(subject, False) == "attachment omitted":
            attached = True
            subject = ""
        external_id = handle or f"preview-{_map_time(str(fields.get('Timestamp') or ''))}-{sender}"
        row, created = self.store.upsert_message(
            external_id=external_id,
            address=sender,
            body=subject,
            direction="out" if fields.get("Sent") else "in",
            timestamp=_map_time(str(fields.get("Timestamp") or "")),
            read=bool(fields.get("Read")),
            has_attachment=attached,
            body_complete=False,
            peer_label=label,
        )
        if announce and created and row["direction"] == "in" and not row["read"]:
            self.emit({"event": "message", "message": row, "notify": True})
        return row, created

    async def _wait_transfer(self, transfer_path: str) -> None:
        """Message.Get answers before the file has any bytes in it.

        The returned object is a transfer. It stays "queued" until the
        phone finishes sending. When the object disappears, the file is ready.
        """
        for _ in range(50):
            try:
                intro = await self._session_bus.introspect("org.bluez.obex", transfer_path)
                obj = self._session_bus.get_proxy_object("org.bluez.obex", transfer_path, intro)
                status = _unwrap(
                    await obj.get_interface("org.freedesktop.DBus.Properties").call_get(
                        "org.bluez.obex.Transfer1", "Status"
                    )
                )
            except Exception:
                return
            if str(status) in ("complete", "error"):
                return
            await asyncio.sleep(0.1)

    async def _read_bmessage(self, target: Path) -> str:
        """Read a downloaded text, retrying while the file is still empty."""
        last = ""
        for _ in range(20):
            try:
                if target.exists() and target.stat().st_size > 0:
                    last = read_bounded_text(target)
                    if not last:
                        return ""
                    if "BEGIN:MSG" in last.upper():
                        return last
            except OSError:
                pass
            await asyncio.sleep(0.05)
        return last

    def _remember_handle(self, handle: str, path: str) -> None:
        """Keep the obex object path for a handle, dropping the oldest when full."""
        if not _handle_token(handle):
            return
        if not isinstance(path, str) or len(path) > _MAX_OBJECT_PATH or "\x00" in path:
            return
        self._message_paths[handle] = path
        while len(self._message_paths) > _MAX_TRACKED_HANDLES:
            self._message_paths.pop(next(iter(self._message_paths)))

    def _note_download_failure(self, handle: str) -> None:
        self._download_failures[handle] = self._download_failures.get(handle, 0) + 1
        while len(self._download_failures) > _MAX_TRACKED_HANDLES:
            self._download_failures.pop(next(iter(self._download_failures)))

    def _gave_up(self, handle: str) -> bool:
        return self._download_failures.get(handle, 0) >= _MAX_DOWNLOAD_TRIES

    async def _download_body(self, handle: str, announce: bool = False, sent: bool | None = None) -> None:
        """Read one text from the phone and store the words plus the sender."""
        path = self._message_paths.get(handle)
        if not path:
            return
        if await self._fetch_body(handle, path, announce, sent):
            self._download_failures.pop(handle, None)
        else:
            self._note_download_failure(handle)

    async def _fetch_body(self, handle: str, path: str, announce: bool, sent: bool | None) -> bool:
        """One download attempt. False when nothing usable came back."""
        target = incoming_bmessage_path(runtime_dir(), handle)
        if target is None:
            return False
        raw = ""
        try:
            # Create the file ourselves, mode 0600, so obexd's open cannot
            # follow a symlink or leave the text world-readable.
            prepare_download_file(target)
            message = await self._iface("org.bluez.obex", path, "org.bluez.obex.Message1", self._session_bus)
            # attachment=False: never copy image bytes onto this computer.
            result = await message.call_get(str(target), False)
            transfer = str(result[0]) if isinstance(result, (list, tuple)) and result else ""
            if transfer:
                await self._wait_transfer(transfer)
            raw = await self._read_bmessage(target)
        except Exception:
            return False
        finally:
            try:
                target.unlink()
            except OSError:
                pass
        text = extract_bmessage_text(raw)
        current = self.store.record(handle)
        if sent is None:
            sent = current["direction"] == "out" if current else False
        peer = extract_bmessage_peer(raw, sent)
        address = address_from_thread(current["thread_key"]) if current else peer
        if not address:
            address = peer
        if not address or not text:
            return False
        label = current["peer_label"] if current else ""
        _row, created = self.store.upsert_message(
            external_id=handle,
            address=address,
            body=text,
            direction="out" if sent else "in",
            timestamp=current["timestamp"] if current else Store.now_stamp(),
            read=bool(current["read"]) if current else False,
            has_attachment=bool(current["has_attachment"]) if current else ("attachment omitted" in present_message(text, False)),
            body_complete=True,
            peer_label=label,
        )
        if announce and created and not sent:
            self.emit({"event": "message", "message": _row, "notify": True})
        return True

    async def _watch_contacts(self) -> None:
        """Pick up the shared phone book whenever the calls part saves a new one."""
        while self._session_bus:
            await asyncio.sleep(8)
            try:
                self._load_shared_contacts()
            except Exception:
                continue

    def _load_shared_contacts(self) -> bool:
        """Copy OmaiPhone's contacts.json into the message store. True when contacts exist."""
        path = shared_contacts_file()
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return self.store.contact_count() > 0
        if mtime == self._contacts_mtime:
            return self.store.contact_count() > 0
        try:
            with path.open("rb") as handle:
                raw = handle.read(_MAX_CONTACTS_FILE + 1)
            if len(raw) > _MAX_CONTACTS_FILE:
                return self.store.contact_count() > 0
            payload = json.loads(raw.decode("utf-8"))
            entries = payload.get("contacts", []) if isinstance(payload, dict) else []
            if not isinstance(entries, list):
                entries = []
        except (OSError, ValueError):
            return self.store.contact_count() > 0
        self._contacts_mtime = mtime
        own = self.store.settings()["ownNumber"]
        rows = []
        for card in entries:
            name = str(card.get("name") or "Unknown")
            for phone in card.get("numbers") or []:
                number = coerce_phone(str(phone), own) or ""
                if number:
                    rows.append({"name": name, "address": number, "kind": "tel"})
            for email in card.get("emails") or []:
                cleaned = normalize_email(str(email))
                if cleaned:
                    rows.append({"name": name, "address": cleaned, "kind": "email"})
        if not rows:
            return self.store.contact_count() > 0
        self.store.replace_contacts(rows)
        self.store.apply_contact_names()
        self._emit_threads()
        self.emit({"event": "contacts", "contacts": self.store.search_contacts("")})
        return True

    def _listen_for_messages(self) -> None:
        if not self._session_bus or self._listening:
            return
        self._listening = True
        self._track(self._subscribe_messages())

    async def _subscribe_messages(self) -> None:
        try:
            intro = await self._session_bus.introspect("org.bluez.obex", "/")
            root = self._session_bus.get_proxy_object("org.bluez.obex", "/", intro)
            manager = root.get_interface("org.freedesktop.DBus.ObjectManager")
            self._obex_root = root
            self._obex_manager = manager
            manager.on_interfaces_added(self._on_message_added)
        except Exception:
            self._listening = False
            return
        self._track(self._watch_messages())

    async def _watch_messages(self) -> None:
        """Look again every couple of seconds.

        The phone can announce a text before our alert listener is ready.
        The alert stays on the bus, so a later look still finds it.
        """
        while self._session_bus:
            await asyncio.sleep(2)
            if not self._map_path:
                continue
            try:
                async with self.lock:
                    saved = await self._collect_live_messages(self._announce_live)
            except Exception:
                continue
            if saved:
                self._emit_threads()
                self.push_status()

    def _on_message_added(self, path, interfaces) -> None:
        props = _props_of(interfaces, "org.bluez.obex.Message1")
        if not props:
            return
        # Schedule the download. Doing it here, inside the D-Bus callback,
        # would stall the Bluetooth connection.
        self._track(self._capture_message(str(path), props))

    async def _capture_message(self, path: str, fields: dict) -> None:
        """Save a text the iPhone just announced."""
        handle = _handle_from(path, fields)
        if path and handle:
            self._remember_handle(handle, path)
        announce = self._announce_live
        async with self.lock:
            self._ingest(fields, path=path, announce=announce)
            if handle:
                await self._download_body(handle, announce=announce, sent=bool(fields.get("Sent")))
        self._emit_threads()
        self.push_status()

    async def _close_profiles(self) -> None:
        if not self._session_bus:
            return
        client = None
        try:
            client = await self._iface("org.bluez.obex", "/org/bluez/obex", "org.bluez.obex.Client1", self._session_bus)
        except Exception:
            return
        if self._map_path:
            try:
                await client.call_remove_session(self._map_path)
            except Exception:
                pass
        self._map_path = ""

    async def _ensure_obexd(self) -> None:
        try:
            await self._session_bus.introspect("org.bluez.obex", "/org/bluez/obex")
            return
        except Exception:
            pass
        if self._obex_proc and self._obex_proc.poll() is None:
            await asyncio.sleep(0.4)
            return
        for candidate in _OBEX_CANDIDATES:
            program = candidate
            if candidate.startswith("/") and not Path(candidate).is_file():
                continue
            try:
                self._obex_proc = subprocess.Popen(
                    [program],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
                break
            except FileNotFoundError:
                continue
        await asyncio.sleep(0.5)
        await self._session_bus.introspect("org.bluez.obex", "/org/bluez/obex")

    async def _adapter(self):
        objects = await self._managed("org.bluez", "/", self._system)
        for path, interfaces in objects.items():
            if "org.bluez.Adapter1" in interfaces:
                return await self._iface("org.bluez", path, "org.bluez.Adapter1", self._system)
        raise RuntimeError("No Bluetooth adapter is available.")

    async def _device(self, address: str):
        objects = await self._managed("org.bluez", "/", self._system)
        wanted = address.upper()
        for path, interfaces in objects.items():
            props = _props_of(interfaces, "org.bluez.Device1")
            if props and str(props.get("Address", "")).upper() == wanted:
                return await self._iface("org.bluez", path, "org.bluez.Device1", self._system)
        raise RuntimeError("That phone is no longer in the Bluetooth list. Scan again.")

    async def _device_name(self, device) -> str:
        try:
            name = await device.get_name()
        except Exception:
            name = ""
        return str(name or "iPhone")

    async def _phone_dicts(self) -> list[dict]:
        objects = await self._managed("org.bluez", "/", self._system)
        found = []
        for _path, interfaces in objects.items():
            props = _props_of(interfaces, "org.bluez.Device1")
            if not props:
                continue
            name = str(props.get("Name") or props.get("Alias") or "")
            icon = str(props.get("Icon") or "")
            if icon != "phone" and "iphone" not in name.lower() and not props.get("Paired"):
                continue
            if icon != "phone" and "iphone" not in name.lower():
                continue
            found.append(
                {
                    "address": str(props.get("Address") or ""),
                    "name": name or "Phone",
                    "paired": bool(props.get("Paired")),
                    "connected": bool(props.get("Connected")),
                }
            )
        return found

    async def _refresh_presence(self) -> None:
        settings = self.store.settings()
        if not settings["phoneAddress"]:
            self.state = "not_paired"
            self.detail = "Select your iPhone to pair."
            return
        try:
            device = await self._device(settings["phoneAddress"])
            connected = await device.get_connected()
        except Exception:
            self.state = "phone_nearby"
            self.detail = "The paired phone is not visible right now."
            return
        self.state = "connected" if connected and self._map_path else "phone_nearby"
        if not self.detail:
            self.detail = "Phone nearby." if not connected else "Connected."

    async def _register_agent(self) -> None:
        if self._agent_on:
            return
        manager = await self._iface("org.bluez", "/org/bluez", "org.bluez.AgentManager1", self._system)
        await manager.call_register_agent("/io/github/dankestrick/omaiphone/texts/Agent", "DisplayYesNo")
        try:
            await manager.call_request_default_agent("/io/github/dankestrick/omaiphone/texts/Agent")
        except Exception:
            pass
        self._agent_on = True

    async def _unregister_agent(self) -> None:
        if not self._agent_on:
            return
        try:
            manager = await self._iface("org.bluez", "/org/bluez", "org.bluez.AgentManager1", self._system)
            await manager.call_unregister_agent("/io/github/dankestrick/omaiphone/texts/Agent")
        except Exception:
            pass
        self._agent_on = False

    async def _start_advert(self) -> None:
        if self._advert_on:
            return
        try:
            objects = await self._managed("org.bluez", "/", self._system)
        except Exception:
            return
        for path, interfaces in objects.items():
            if "org.bluez.LEAdvertisingManager1" not in interfaces:
                continue
            try:
                manager = await self._iface("org.bluez", path, "org.bluez.LEAdvertisingManager1", self._system)
                await manager.call_register_advertisement("/io/github/dankestrick/omaiphone/texts/Advert", {})
                self._advert_on = True
            except Exception:
                self.detail = "Paired, but the message switches may not appear until the phone is reopened in Bluetooth settings."
            return

    async def _stop_advert(self) -> None:
        if not self._advert_on:
            return
        try:
            objects = await self._managed("org.bluez", "/", self._system)
            for path, interfaces in objects.items():
                if "org.bluez.LEAdvertisingManager1" not in interfaces:
                    continue
                manager = await self._iface("org.bluez", path, "org.bluez.LEAdvertisingManager1", self._system)
                await manager.call_unregister_advertisement("/io/github/dankestrick/omaiphone/texts/Advert")
        except Exception:
            pass
        self._advert_on = False

    async def _managed(self, service: str, path: str, bus: MessageBus) -> dict:
        intro = await bus.introspect(service, path)
        root = bus.get_proxy_object(service, path, intro)
        manager = root.get_interface("org.freedesktop.DBus.ObjectManager")
        return await manager.call_get_managed_objects()

    async def _iface(self, service: str, path: str, interface: str, bus: MessageBus):
        intro = await bus.introspect(service, path)
        return bus.get_proxy_object(service, path, intro).get_interface(interface)

    def _emit_threads(self) -> None:
        self.emit({"event": "threads", "threads": self.store.threads()})

