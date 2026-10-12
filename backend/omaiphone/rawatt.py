"""The iPhone's GATT services over our own ATT channel (L2CAP PSM 31).

Used when bluetoothd has not opened a GATT client on the phone's link (no
GattService1 objects under the device). When bluetoothd does hold the channel,
the iPhone refuses this one and gatt.BluezGatt is used instead.

Offers companion.Session the same operations as gatt.BluezGatt.
"""

import errno
import os
import socket

from gi.repository import GLib

from .att import PSM, AttClient

# One GLib wake must return. A phone that keeps the socket readable would
# otherwise run this callback until the socket goes quiet.
PACKETS_PER_WAKE = 32


def open_channel(address):
    """Start connecting to the phone's ATT channel; the socket is non-blocking."""
    sock = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_SEQPACKET, socket.BTPROTO_L2CAP)
    sock.setblocking(False)
    code = sock.connect_ex((address, PSM))
    if code not in (0, errno.EINPROGRESS):
        sock.close()
        raise OSError(code, os.strerror(code))
    return sock


class RawAtt:
    def __init__(self, address, on_lost, opener=open_channel):
        self.on_lost = on_lost
        self.att = AttClient(self._send)
        self.sock = opener(address)
        self.connecting = True
        self.on_ready = None
        self._closed = False
        self._watch = GLib.io_add_watch(self.sock.fileno(), GLib.PRIORITY_DEFAULT,
                                        GLib.IO_OUT | GLib.IO_ERR | GLib.IO_HUP, self._connected)

    # ---- what Session expects from a client ---------------------------------------

    @property
    def on_notify(self):
        return self.att.on_notify

    @on_notify.setter
    def on_notify(self, callback):
        self.att.on_notify = callback

    @property
    def busy(self):
        return self.att.busy

    def fail_pending(self, error):
        self.att.fail_pending(error)
        self.close()

    def feed(self, packet):
        self.att.feed(packet)

    def discover_services(self, on_done):
        self.att.discover_services(on_done)

    def discover_characteristics(self, first, last, on_done):
        self.att.discover_characteristics(first, last, on_done)

    def subscribe(self, characteristic, on_done):
        self.att.subscribe(characteristic, on_done)

    def read(self, handle, on_done):
        self.att.read(handle, on_done)

    def write(self, handle, value, on_done=None):
        self.att.write(handle, value, on_done)

    # ---- the socket --------------------------------------------------------------------

    def _connected(self, _fd, _condition):
        self._watch = 0
        code = self.sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
        if code:
            self.close()
            if code == errno.ECONNREFUSED:
                # bluetoothd holds the phone's one ATT channel; its GATT objects follow shortly.
                self.on_lost("the phone's notification link is in use by BlueZ; trying through BlueZ")
            else:
                self.on_lost(os.strerror(code))
            return False
        self.connecting = False
        self._watch = GLib.io_add_watch(self.sock.fileno(), GLib.PRIORITY_DEFAULT,
                                        GLib.IO_IN | GLib.IO_ERR | GLib.IO_HUP, self._readable)
        if self.on_ready:
            self.on_ready()
        return False

    def _readable(self, _fd, condition):
        for _ in range(PACKETS_PER_WAKE):
            if self._closed:
                break
            try:
                packet = self.sock.recv(4096)
            except BlockingIOError:
                break
            except OSError as error:
                self._watch = 0
                self.close()
                self.on_lost(error.strerror or str(error))
                return False
            if not packet:
                self._watch = 0
                self.close()
                self.on_lost("the iPhone closed the data channel")
                return False
            self.att.feed(packet)
        else:
            # The phone still had data after this wake's share. Stay subscribed.
            return True
        if self._closed:
            self._watch = 0
            return False
        if condition & (GLib.IO_ERR | GLib.IO_HUP):
            self._watch = 0
            self.close()
            self.on_lost("the iPhone closed the data channel")
            return False
        return True

    def _send(self, packet):
        try:
            self.sock.send(packet)
        except OSError:
            pass   # the watch on the socket reports the broken channel

    def close(self):
        if self._closed:
            return
        self._closed = True
        if self._watch:
            GLib.source_remove(self._watch)
            self._watch = 0
        try:
            self.sock.close()
        except OSError:
            pass
