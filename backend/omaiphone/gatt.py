"""The iPhone's GATT services through BlueZ (org.bluez.GattCharacteristic1).

bluetoothd already runs a GATT client on the phone's link, and the iPhone
accepts one ATT channel per computer, so a raw L2CAP socket of our own gets
refused. This class gives companion.Session the same few operations as
att.AttClient (discover, subscribe, write, notifications) but routes them
through BlueZ's objects instead.

"Handles" here are BlueZ object paths. Session only uses them as keys, so it
does not notice the difference.
"""

from gi.repository import Gio, GLib

from .att import ERR_ATTRIBUTE_NOT_FOUND, ERR_INSUFFICIENT_AUTHORIZATION, AttError, bounded_value

BLUEZ = "org.bluez"
SERVICE_IFACE = "org.bluez.GattService1"
CHAR_IFACE = "org.bluez.GattCharacteristic1"
DEVICE_IFACE = "org.bluez.Device1"
PROPS_IFACE = "org.freedesktop.DBus.Properties"
OM_IFACE = "org.freedesktop.DBus.ObjectManager"
TIMEOUT_MS = 12000

# BlueZ errors that mean "the iPhone has not allowed this computer yet".
PERMISSION_WORDS = ("NotPermitted", "NotAuthorized", "Authentication", "Authorization", "Encryption")


def device_path(bus, address):
    """BlueZ object path of the device with this address, or None."""
    try:
        objects = bus.call_sync(BLUEZ, "/", OM_IFACE, "GetManagedObjects", None,
                                GLib.VariantType("(a{oa{sa{sv}}})"), Gio.DBusCallFlags.NONE, 10000, None).unpack()[0]
    except GLib.Error:
        return None
    for path, ifaces in objects.items():
        device = ifaces.get(DEVICE_IFACE)
        if device and str(device.get("Address", "")).upper() == str(address).upper():
            return path
    return None


def has_services(bus, path):
    """True when BlueZ lists GATT services under the device (its GATT client holds the link)."""
    try:
        objects = bus.call_sync(BLUEZ, "/", OM_IFACE, "GetManagedObjects", None,
                                GLib.VariantType("(a{oa{sa{sv}}})"), Gio.DBusCallFlags.NONE, 10000, None).unpack()[0]
    except GLib.Error:
        return False
    return any(path_.startswith(path + "/") and SERVICE_IFACE in ifaces for path_, ifaces in objects.items())


def device_props(bus, path):
    try:
        return bus.call_sync(BLUEZ, path, PROPS_IFACE, "GetAll", GLib.Variant("(s)", (DEVICE_IFACE,)),
                             GLib.VariantType("(a{sv})"), Gio.DBusCallFlags.NONE, 5000, None).unpack()[0]
    except GLib.Error:
        return {}


def _error(error):
    """GLib.Error from BlueZ -> AttError, so Session treats it like the raw client's errors."""
    text = error.message or ""
    if any(word in text for word in PERMISSION_WORDS):
        return AttError(ERR_INSUFFICIENT_AUTHORIZATION)
    failure = AttError(0)
    failure.args = (text.split(":", 2)[-1].strip() or text,)
    return failure


class BluezGatt:
    def __init__(self, bus, device, on_notify=None):
        self.bus = bus
        self.device = device
        self.on_notify = on_notify or (lambda handle, value: None)
        self._subscriptions = []   # signal ids
        self._notifying = []       # characteristic paths with StartNotify on
        self._objects = {}         # BlueZ objects from the last discovery
        self._closed = False

    # ---- what Session expects from a client ---------------------------------------

    @property
    def busy(self):
        return False   # D-Bus calls carry their own timeouts

    def fail_pending(self, _error):
        self.close()

    def feed(self, _packet):
        pass

    def discover_services(self, on_done):
        """on_done([(path, path, uuid)], None): the phone's GATT services as BlueZ resolved them."""
        def reply(bus, result):
            try:
                objects = bus.call_finish(result).unpack()[0]
            except GLib.Error as error:
                return on_done(None, _error(error))
            self._objects = objects
            found = []
            for path, ifaces in objects.items():
                service = ifaces.get(SERVICE_IFACE)
                if service and path.startswith(self.device + "/"):
                    found.append((path, path, str(service.get("UUID", "")).lower()))
            on_done(found, None)

        self.bus.call(BLUEZ, "/", OM_IFACE, "GetManagedObjects", None, GLib.VariantType("(a{oa{sa{sv}}})"),
                      Gio.DBusCallFlags.NONE, TIMEOUT_MS, None, reply)

    def discover_characteristics(self, first, _last, on_done):
        """on_done({uuid: {"value": path, "properties": flags}}, None) for one service."""
        found = {}
        for path, ifaces in self._objects.items():
            char = ifaces.get(CHAR_IFACE)
            if char and str(char.get("Service", "")) == first:
                found[str(char.get("UUID", "")).lower()] = {"value": path, "properties": list(char.get("Flags", []))}
        GLib.idle_add(lambda: (on_done(found, None), False)[1])

    def subscribe(self, characteristic, on_done):
        path = characteristic.get("value") if characteristic else None
        if not path:
            return on_done(AttError(ERR_ATTRIBUTE_NOT_FOUND))
        self._listen(path)

        def reply(bus, result):
            try:
                bus.call_finish(result)
            except GLib.Error as error:
                if "InProgress" not in (error.message or ""):   # already notifying counts as done
                    return on_done(_error(error))
            if path not in self._notifying:
                self._notifying.append(path)
            on_done(None)

        self.bus.call(BLUEZ, path, CHAR_IFACE, "StartNotify", None, None, Gio.DBusCallFlags.NONE,
                      TIMEOUT_MS, None, reply)

    def read(self, handle, on_done):
        def reply(bus, result):
            try:
                raw = bus.call_finish(result).unpack()[0]
            except GLib.Error as error:
                return on_done(None, _error(error))
            value = bounded_value(raw)
            if value is None:
                return on_done(None, AttError(0))
            on_done(value, None)

        self.bus.call(BLUEZ, handle, CHAR_IFACE, "ReadValue", GLib.Variant("(a{sv})", ({},)),
                      GLib.VariantType("(ay)"), Gio.DBusCallFlags.NONE, TIMEOUT_MS, None, reply)

    def write(self, handle, value, on_done=None):
        options = {"type": GLib.Variant("s", "request")}

        def reply(bus, result):
            try:
                bus.call_finish(result)
                failure = None
            except GLib.Error as error:
                failure = _error(error)
            if on_done:
                on_done(failure)

        self.bus.call(BLUEZ, handle, CHAR_IFACE, "WriteValue",
                      GLib.Variant("(aya{sv})", (bytes(value), options)), None, Gio.DBusCallFlags.NONE,
                      TIMEOUT_MS, None, reply)

    # ---- notifications ---------------------------------------------------------------

    def _deliver(self, path, value):
        blob = bounded_value(value)
        if blob is None:
            return
        self.on_notify(path, blob)

    def _listen(self, path):
        def changed(_conn, _sender, object_path, _iface, _member, params, _data):
            if self._closed:
                return
            interface, props, _invalid = params.unpack()
            if interface == CHAR_IFACE and "Value" in props:
                self._deliver(object_path, props["Value"])

        self._subscriptions.append(self.bus.signal_subscribe(
            BLUEZ, PROPS_IFACE, "PropertiesChanged", path, None, Gio.DBusSignalFlags.NONE, changed, None))

    def close(self):
        if self._closed:
            return
        self._closed = True
        for sid in self._subscriptions:
            self.bus.signal_unsubscribe(sid)
        self._subscriptions = []
        for path in self._notifying:
            self.bus.call(BLUEZ, path, CHAR_IFACE, "StopNotify", None, None, Gio.DBusCallFlags.NONE,
                          3000, None, None)
        self._notifying = []
