"""Map org.pipewire.Telephony D-Bus traffic to backend events.

All functions take plain Python values (already unpacked from GVariant) so
they can be unit tested without a bus.
"""

AG_IFACE = "org.pipewire.Telephony.AudioGateway1"
TRANSPORT_IFACE = "org.pipewire.Telephony.AudioGatewayTransport1"
CALL_IFACE = "org.pipewire.Telephony.Call1"

# HFP codec ids (Bluetooth HFP spec): 1 = CVSD, 2 = mSBC, 3 = LC3-SWB
# 127: Apple vendor LC3 super-wideband, reported by PipeWire as "lc3_a127"
CODECS = {1: "CVSD", 2: "mSBC", 3: "LC3-SWB", 127: "LC3-SWB (Apple)"}

CALLER_MAX = 80
STATE_MAX = 32
ADDRESS_MAX = 32


def clip_text(value, limit):
    """One line from the phone: no NULs, collapsed whitespace, at most `limit` characters.

    A non-string is dropped. Converting an enormous integer to text would itself
    be the cost a hostile value is trying to impose.
    """
    if isinstance(value, bool) or not isinstance(value, str):
        return ""
    chunk = value.replace("\x00", "")[:limit * 4]
    return " ".join(chunk.split())[:limit]


def clip_optional(value, limit):
    """Like clip_text, but None stays None so a missing property is still missing."""
    if value is None:
        return None
    return clip_text(value, limit)


def _codec_number(codec_id):
    if isinstance(codec_id, bool) or codec_id is None or codec_id == "":
        return None
    if isinstance(codec_id, int):
        number = codec_id
    elif isinstance(codec_id, str) and len(codec_id) <= 8:
        try:
            number = int(codec_id, 10)
        except ValueError:
            return None
    else:
        return None
    if not 0 <= number <= 255:
        return None
    return number


def codec_name(codec_id):
    number = _codec_number(codec_id)
    if not number:
        return ""
    return CODECS.get(number, f"codec {number}")


def is_wideband(codec_id):
    number = _codec_number(codec_id)
    return number is not None and number >= 2


def _level(value):
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if not 0 <= value <= 255:
        return None
    return value


def interfaces_added(path, interfaces):
    events = []
    if AG_IFACE in interfaces:
        props = interfaces[AG_IFACE]
        events.append({"type": "phone-connected", "path": path,
                       "address": clip_text(props.get("Address", ""), ADDRESS_MAX)})
    if TRANSPORT_IFACE in interfaces:
        props = interfaces[TRANSPORT_IFACE]
        events.append(_transport_event(path, props))
    if CALL_IFACE in interfaces:
        props = interfaces[CALL_IFACE]
        events.append({
            "type": "call-added",
            "path": path,
            "state": clip_text(props.get("State", ""), STATE_MAX),
            "number": clip_text(props.get("LineIdentification", ""), CALLER_MAX),
            "name": clip_text(props.get("Name", ""), CALLER_MAX),
        })
    return events


def interfaces_removed(path, interfaces):
    events = []
    if CALL_IFACE in interfaces:
        events.append({"type": "call-removed", "path": path})
    if AG_IFACE in interfaces:
        events.append({"type": "phone-disconnected", "path": path})
    return events


def properties_changed(path, interface, changed):
    events = []
    if interface == CALL_IFACE:
        if "State" in changed:
            events.append({"type": "call-state", "path": path, "state": clip_text(changed["State"], STATE_MAX)})
        if "LineIdentification" in changed or "Name" in changed:
            events.append({
                "type": "caller-id",
                "path": path,
                "number": clip_optional(changed.get("LineIdentification"), CALLER_MAX),
                "name": clip_optional(changed.get("Name"), CALLER_MAX),
            })
    elif interface == TRANSPORT_IFACE:
        events.append(_transport_event(path, changed))
    elif interface == AG_IFACE:
        volume = {}
        for key in ("SpeakerVolume", "MicrophoneVolume"):
            if key in changed:
                level = _level(changed[key])
                if level is not None:
                    volume[key] = level
        if volume:
            events.append({"type": "volume", "path": path, **volume})
    return events


def _transport_event(path, props):
    event = {"type": "audio", "path": path}
    if "State" in props:
        event["state"] = clip_text(props["State"], STATE_MAX)
    if "Codec" in props:
        event["codec"] = codec_name(props["Codec"])
        event["wideband"] = is_wideband(props["Codec"])
    return event


def ag_path_of(call_path):
    """/org/pipewire/Telephony/ag0/call1 -> /org/pipewire/Telephony/ag0"""
    return call_path.rsplit("/", 1)[0]
