import os
import struct
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
sys.path.insert(0, os.path.dirname(__file__))

from omaiphone import ancs, att  # noqa: E402
from omaiphone.companion import COMMAND_TIMEOUT, REQUEST_TIMEOUT, Session, fallback_app_name  # noqa: E402
from sim_phone import BATTERY_LEVEL, SimPhone, connect  # noqa: E402


class Recorder:
    """Collects what a Session reports."""

    def __init__(self):
        self.states, self.notifications, self.removed, self.media = [], [], [], []
        self.now = 1000.0

    def session(self, send):
        return Session(send, on_state=lambda state, detail: self.states.append((state, detail)),
                       on_notification=self.notifications.append, on_removed=self.removed.append,
                       on_media=self.media.append, clock=lambda: self.now)


def started(phone):
    recorder = Recorder()
    session, pump = connect(recorder.session, phone)
    session.start()
    pump()
    return recorder, session, pump


class AttCodecTests(unittest.TestCase):
    def test_uuid_round_trip(self):
        self.assertEqual(att.uuid_from_wire(att.uuid_to_wire(0x2902)), "00002902-0000-1000-8000-00805f9b34fb")
        self.assertEqual(att.uuid_from_wire(att.uuid_to_wire(ancs.SERVICE)), ancs.SERVICE)
        with self.assertRaises(ValueError):
            att.uuid_from_wire(b"\x01\x02\x03")

    def test_parses_what_the_real_phone_sent(self):
        # First answer of the iPhone 14 during the feasibility probe
        pdu = bytes.fromhex("1106" "01000500" "0018" "06000900" "0118" "0a000e00" "0a18")
        self.assertEqual(att.parse_services(pdu), [
            (1, 5, "00001800-0000-1000-8000-00805f9b34fb"),
            (6, 9, "00001801-0000-1000-8000-00805f9b34fb"),
            (10, 14, "0000180a-0000-1000-8000-00805f9b34fb")])

    def test_rejects_malformed_listings(self):
        for parse in (att.parse_services, att.parse_characteristics, att.parse_descriptors):
            with self.assertRaises(ValueError):
                parse(bytes([0x11, 3, 1, 2, 3]))


class AttClientTests(unittest.TestCase):
    def test_one_request_at_a_time(self):
        sent, answers = [], []
        client = att.AttClient(sent.append)
        client.request(b"\x0a\x01\x00", lambda pdu, error: answers.append(pdu))
        client.request(b"\x0a\x02\x00", lambda pdu, error: answers.append(pdu))
        self.assertEqual(sent, [b"\x0a\x01\x00"])
        client.feed(b"\x0b\xaa")
        self.assertEqual(sent, [b"\x0a\x01\x00", b"\x0a\x02\x00"])
        client.feed(b"\x0b\xbb")
        self.assertEqual(answers, [b"\x0b\xaa", b"\x0b\xbb"])
        self.assertFalse(client.busy)

    def test_answers_the_phones_own_requests_without_confusing_them_for_answers(self):
        sent, answers = [], []
        client = att.AttClient(sent.append)
        client.request(att.read_by_group_request(1), lambda pdu, error: answers.append((pdu, error)))
        client.feed(att.read_by_group_request(1))            # the phone asks the same thing
        self.assertEqual(sent[-1], att.error_response(att.OP_READ_BY_GROUP_REQ, 1, att.ERR_ATTRIBUTE_NOT_FOUND))
        self.assertEqual(answers, [])
        client.feed(struct.pack("<BH", att.OP_MTU_REQ, 185))
        self.assertEqual(sent[-1], struct.pack("<BH", att.OP_MTU_RSP, 672))
        client.feed(b"\x52\x03\x00\x01")                      # write command: no answer
        self.assertEqual(len(sent), 3)

    def test_indications_are_confirmed_and_delivered(self):
        sent, got = [], []
        client = att.AttClient(sent.append, lambda handle, value: got.append((handle, value)))
        client.feed(struct.pack("<BH", att.OP_INDICATE, 8) + b"\x01\x00\xff\xff")
        client.feed(struct.pack("<BH", att.OP_NOTIFY, 40) + b"abc")
        self.assertEqual(sent, [bytes([att.OP_CONFIRM])])
        self.assertEqual(got, [(8, b"\x01\x00\xff\xff"), (40, b"abc")])

    def test_errors_reach_the_caller_and_late_answers_are_ignored(self):
        sent, answers = [], []
        client = att.AttClient(sent.append)
        client.feed(b"\x13")                                  # nobody asked
        client.request(att.write_request(41, b"\x01\x00"), lambda pdu, error: answers.append(error))
        client.feed(att.error_response(att.OP_READ_REQ, 3, att.ERR_READ_NOT_PERMITTED))   # for another request
        self.assertEqual(answers, [])
        client.feed(att.error_response(att.OP_WRITE_REQ, 41, att.ERR_INSUFFICIENT_AUTHORIZATION))
        self.assertTrue(answers[0].needs_permission)
        self.assertEqual(str(answers[0]), "insufficient authorization")

    def test_discovery_stops_when_the_phone_does_not_advance(self):
        sent, done = [], []

        def send(pdu):
            sent.append(pdu)
            if len(sent) > 2:
                raise AssertionError("discovery kept requesting")
            # End handle 0 is behind the start handle, so the next request would repeat.
            client.feed(bytes([att.OP_READ_BY_GROUP_RSP, 6]) + struct.pack("<HHH", 1, 0, 0x1800))

        client = att.AttClient(send)
        client.discover_services(lambda services, error: done.append((services, error)))
        self.assertEqual(len(sent), 1)
        self.assertIsNone(done[0][0])
        self.assertIsInstance(done[0][1], ValueError)

    def test_discovery_follows_one_advancing_range(self):
        sent, done = [], []

        def send(pdu):
            sent.append(pdu)
            start = struct.unpack_from("<H", pdu, 1)[0]
            if start == 1:
                client.feed(bytes([att.OP_READ_BY_GROUP_RSP, 6]) + struct.pack("<HHH", 1, 5, 0x1800))
            else:
                client.feed(att.error_response(att.OP_READ_BY_GROUP_REQ, start, att.ERR_ATTRIBUTE_NOT_FOUND))

        client = att.AttClient(send)
        client.discover_services(lambda services, error: done.append((services, error)))
        self.assertEqual([struct.unpack_from("<H", pdu, 1)[0] for pdu in sent], [1, 6])
        self.assertIsNone(done[0][1])
        self.assertEqual(done[0][0], [(1, 5, att.BASE_UUID % 0x1800)])

    def test_characteristic_discovery_stops_when_the_handle_does_not_advance(self):
        sent, done = [], []

        def send(pdu):
            sent.append(pdu)
            if len(sent) > 2:
                raise AssertionError("discovery kept requesting")
            # Declaration handle 0 does not move the cursor past the start handle.
            client.feed(bytes([att.OP_READ_BY_TYPE_RSP, 7]) + struct.pack("<HBHH", 0, 0x02, 2, 0x2A00))

        client = att.AttClient(send)
        client.discover_characteristics(1, 20, lambda chars, error: done.append((chars, error)))
        self.assertEqual(len(sent), 1)
        self.assertIsNone(done[0][0])
        self.assertIsInstance(done[0][1], ValueError)

    def test_fail_pending_releases_everyone(self):
        answers = []
        client = att.AttClient(lambda pdu: None)
        for handle in (1, 2, 3):
            client.request(struct.pack("<BH", att.OP_READ_REQ, handle), lambda pdu, error: answers.append(error))
        client.fail_pending(att.AttError(0))
        self.assertEqual(len(answers), 3)
        self.assertFalse(client.busy)

    def test_oversized_notification_is_not_delivered(self):
        sent, got = [], []
        client = att.AttClient(sent.append, lambda handle, value: got.append(value))
        client.feed(struct.pack("<BH", att.OP_NOTIFY, 7) + b"x" * (att.MAX_VALUE_BYTES + 1))
        client.feed(struct.pack("<BH", att.OP_INDICATE, 7) + b"ok")
        self.assertEqual(got, [b"ok"])
        self.assertEqual(sent, [bytes([att.OP_CONFIRM])])
        self.assertIsNone(att.bounded_value(b"x" * (att.MAX_VALUE_BYTES + 1)))
        self.assertEqual(att.bounded_value([1, 2]), b"\x01\x02")


class GattAndRawTests(unittest.TestCase):
    def test_gatt_drops_an_oversized_notification(self):
        from omaiphone.gatt import BluezGatt
        got = []
        gatt = BluezGatt.__new__(BluezGatt)
        gatt.on_notify = lambda path, value: got.append((path, value))
        gatt._deliver("/char", b"hi")
        gatt._deliver("/char", bytearray(att.MAX_VALUE_BYTES + 1))
        self.assertEqual(got, [("/char", b"hi")])

    def test_one_wake_reads_a_fixed_number_of_packets(self):
        from omaiphone.rawatt import PACKETS_PER_WAKE, RawAtt
        fed = []

        class Sock:
            def __init__(self):
                self.left = PACKETS_PER_WAKE + 10

            def recv(self, _n):
                if self.left <= 0:
                    raise BlockingIOError
                self.left -= 1
                return b"\x1b\x01\x00ok"

            def close(self):
                pass

        raw = RawAtt.__new__(RawAtt)
        raw._closed = False
        raw._watch = 0
        raw.sock = Sock()
        raw.att = SimpleNamespace(feed=fed.append)
        raw.on_lost = lambda _message: None
        self.assertTrue(raw._readable(0, 0))
        self.assertEqual(len(fed), PACKETS_PER_WAKE)
        self.assertEqual(raw.sock.left, 10)
        self.assertTrue(raw._readable(0, 0))
        self.assertEqual(len(fed), PACKETS_PER_WAKE + 10)
        self.assertTrue(raw._readable(0, 0))
        self.assertFalse(raw._closed)

        raw.sock = SimpleNamespace(recv=lambda _n: b"", close=lambda: None)
        lost = []
        raw.on_lost = lost.append
        self.assertFalse(raw._readable(0, 0))
        self.assertTrue(raw._closed)
        self.assertEqual(lost, ["the iPhone closed the data channel"])


class AncsCodecTests(unittest.TestCase):
    def test_event(self):
        event = ancs.parse_event(struct.pack("<BBBBI", 0, ancs.FLAG_PREEXISTING | ancs.FLAG_NEGATIVE_ACTION, 6, 3, 77))
        self.assertEqual((event["event"], event["uid"], event["category"]), ("added", 77, "email"))
        self.assertTrue(event["preexisting"] and event["canDismiss"])
        self.assertFalse(event["silent"] or event["important"])
        self.assertIsNone(ancs.parse_event(b"\x00\x01"))
        self.assertIsNone(ancs.parse_event(struct.pack("<BBBBI", 9, 0, 0, 0, 1)))
        self.assertEqual(ancs.parse_event(struct.pack("<BBBBI", 2, 0, 200, 0, 1))["category"], "other")

    def test_commands(self):
        self.assertEqual(ancs.get_notification_attributes(5, ((0, None), (1, 64), (3, 255))),
                         bytes.fromhex("00" "05000000" "00" "014000" "03ff00"))
        self.assertEqual(ancs.get_app_attributes("com.x"), b"\x01com.x\x00\x00")
        self.assertEqual(ancs.perform_action(5, ancs.ACTION_NEGATIVE), bytes.fromhex("02" "05000000" "01"))

    def test_attributes_arrive_in_pieces(self):
        full = (b"\x00" + struct.pack("<I", 9) + struct.pack("<BH", 0, 5) + b"com.x"
                + struct.pack("<BH", 1, 4) + "Müm".encode() + struct.pack("<BH", 3, 0))
        for cut in range(len(full)):
            self.assertIsNone(ancs.parse_notification_attributes(full[:cut], expected=3), cut)
        self.assertEqual(ancs.parse_notification_attributes(full, expected=3), (9, {0: "com.x", 1: "Müm", 3: ""}))
        self.assertIsNone(ancs.parse_notification_attributes(b"\x01" + full[1:], expected=3))

    def test_app_attributes(self):
        data = b"\x01com.x\x00" + struct.pack("<BH", 0, 8) + b"Messages"
        self.assertEqual(ancs.parse_app_attributes(data), ("com.x", {0: "Messages"}))
        self.assertIsNone(ancs.parse_app_attributes(data[:-1]))
        self.assertIsNone(ancs.parse_app_attributes(b"\x01com.x"))

    def test_date_and_media(self):
        self.assertEqual(ancs.parse_date("20261002T094118"), "2026-10-02T09:41:18")
        self.assertEqual(ancs.parse_date("yesterday"), "")
        self.assertEqual(ancs.parse_media_update(bytes([2, 2, 0]) + b"Sinnerman"), {"title": "Sinnerman"})
        self.assertEqual(ancs.parse_media_update(bytes([0, 1, 0]) + b"1,1.0,3.2"), {"playing": True})
        self.assertEqual(ancs.parse_media_update(bytes([0, 1, 0]) + b"0,0.0,3.2"), {"playing": False})
        self.assertEqual(ancs.parse_media_update(bytes([1, 0, 0]) + b"3"), {})
        self.assertEqual(ancs.parse_media_update(b"\x02"), {})
        self.assertEqual(ancs.parse_supported_commands(bytes([4, 0, 1, 99])), ["pause", "play", "previous"])
        long_title = b"T" * (ancs.MAX_MEDIA_CHARS + 40)
        self.assertEqual(len(ancs.parse_media_update(bytes([2, 2, 0]) + long_title)["title"]), ancs.MAX_MEDIA_CHARS)

    def test_oversized_attribute_is_rejected_before_the_bytes_arrive(self):
        huge = b"\x00" + struct.pack("<I", 1) + struct.pack("<BH", 3, ancs.MAX_VALUE_BYTES + 1)
        with self.assertRaises(ancs.Rejected):
            ancs.parse_notification_attributes(huge, expected=1)
        # A length we did ask for, but the bytes are not here yet, is still incomplete.
        pending = b"\x00" + struct.pack("<I", 1) + struct.pack("<BH", 3, 10)
        self.assertIsNone(ancs.parse_notification_attributes(pending, expected=1))
        with self.assertRaises(ancs.Rejected):
            ancs.parse_app_attributes(b"\x01" + b"a" * (ancs.MAX_APP_ID + 5))


class SessionCapTests(unittest.TestCase):
    def test_hostile_attribute_ends_the_command(self):
        session = Session(lambda _packet: None)
        session.state = "ready"
        session._current = ("attributes", 1, 0)
        session._data = b"\x00" + struct.pack("<I", 1) + struct.pack("<BH", 3, 5000)
        session._data_arrived()
        self.assertIsNone(session._current)
        self.assertEqual(session._data, b"")

    def test_data_source_buffer_stays_bounded(self):
        session = Session(lambda _packet: None)
        session.state = "ready"
        session._handles[ancs.DATA_SOURCE] = 9
        session._current = ("attributes", 1, 0)
        session._on_notify(9, b"\x01" + b"x" * (ancs.MAX_ACCUMULATED + 100))
        self.assertLessEqual(len(session._data), ancs.MAX_ACCUMULATED)

    def test_notification_uids_are_capped(self):
        removed = []
        session = Session(lambda _packet: None, on_removed=removed.append)
        session.state = "ready"
        session._handles[ancs.CONTROL_POINT] = 4
        for uid in range(session_limit()):
            session._event({"event": "added", "uid": uid, "category": "other", "silent": False,
                            "important": False, "preexisting": False, "canDismiss": False, "canAct": False,
                            "categoryCount": 1})
        self.assertLessEqual(len(session._events), companion_max())
        self.assertTrue(removed)
        self.assertIn(0, removed)


def session_limit():
    from omaiphone.companion import MAX_TRACKED
    return MAX_TRACKED + 25


def companion_max():
    from omaiphone.companion import MAX_TRACKED
    return MAX_TRACKED


class SessionTests(unittest.TestCase):
    def test_finds_the_services_and_reports_what_was_already_there(self):
        phone = SimPhone()
        phone.notifications[7] = {"app": "com.apple.MobileSMS", "title": "Mum", "subtitle": "", "message": "Call me back",
                                  "date": "20261002T094118", "category": 4, "flags": ancs.FLAG_NEGATIVE_ACTION}
        recorder, session, _pump = started(phone)
        self.assertEqual(recorder.states, [("ready", "")])
        self.assertEqual(phone.subscribed, {ancs.DATA_SOURCE, ancs.NOTIFICATION_SOURCE, ancs.ENTITY_UPDATE,
                                            ancs.REMOTE_COMMAND, BATTERY_LEVEL})
        # the exact level from the Battery Service, then its updates
        self.assertEqual(session.battery, 73)
        phone.set_battery(28)
        _pump()
        self.assertEqual(session.battery, 28)
        self.assertEqual(recorder.notifications, [{
            "uid": 7, "appId": "com.apple.MobileSMS", "app": "Messages", "title": "Mum", "subtitle": "",
            "message": "Call me back", "date": "2026-10-02T09:41:18", "category": "social", "silent": False,
            "important": False, "preexisting": True, "canDismiss": True, "updated": False}])
        # the same handles the real iPhone 14 reported
        self.assertEqual((phone.handles[ancs.CONTROL_POINT], phone.handles[ancs.NOTIFICATION_SOURCE],
                          phone.handles[ancs.DATA_SOURCE]), (37, 40, 43))

    def test_new_changed_and_removed_notifications(self):
        phone = SimPhone(fragment=7)
        recorder, _session, pump = started(phone)
        phone.add(1, message="x" * 500)
        phone.add(2, app="net.whatsapp.WhatsApp", title="Group", subtitle="Ali", message="👍")
        pump()
        self.assertEqual([(n["uid"], n["app"], n["preexisting"]) for n in recorder.notifications],
                         [(1, "Messages", False), (2, "WhatsApp", False)])
        self.assertEqual(len(recorder.notifications[0]["message"]), 400, "long messages are cut by the phone")
        self.assertEqual(recorder.notifications[1]["subtitle"], "Ali")
        self.assertEqual(recorder.notifications[1]["message"], "👍")

        phone.add(1, message="edited", event=ancs.EVENT_MODIFIED)
        pump()
        self.assertEqual((recorder.notifications[-1]["message"], recorder.notifications[-1]["updated"]), ("edited", True))

        phone.remove(2)
        pump()
        self.assertEqual(recorder.removed, [2])

    def test_app_names_are_asked_once_and_guessed_when_the_phone_refuses(self):
        phone = SimPhone()
        recorder, _session, pump = started(phone)
        for uid in (1, 2, 3):
            phone.add(uid, app="com.burbn.instagram")
        phone.add(4)
        phone.add(5)
        pump()
        self.assertEqual([n["app"] for n in recorder.notifications],
                         ["Instagram", "Instagram", "Instagram", "Messages", "Messages"])
        self.assertEqual(sorted(n["uid"] for n in recorder.notifications), [1, 2, 3, 4, 5])
        self.assertEqual(fallback_app_name(""), "iPhone")

    def test_a_notification_that_vanishes_mid_way_is_dropped(self):
        phone = SimPhone()
        recorder, session, pump = started(phone)
        phone.add(1)
        phone.add(2)
        del phone.notifications[1]            # gone before the details were asked for
        pump()
        self.assertEqual([n["uid"] for n in recorder.notifications], [2])

        phone.add(3)
        session.feed(phone.outbox.popleft())   # the event arrives ...
        session.feed(struct.pack("<BH", att.OP_NOTIFY, 40) + struct.pack("<BBBBI", 2, 0, 4, 0, 3))   # ... then its removal
        pump()
        self.assertEqual([n["uid"] for n in recorder.notifications], [2])
        self.assertEqual(recorder.removed, [3])

    def test_dismiss_clears_it_on_the_phone(self):
        phone = SimPhone()
        recorder, session, pump = started(phone)
        phone.add(1)
        pump()
        self.assertTrue(session.dismiss(1))
        pump()
        self.assertEqual(phone.dismissed, [1])
        self.assertEqual(recorder.removed, [1])
        self.assertFalse(session.dismiss(1), "already gone")
        self.assertFalse(session.dismiss(99))

    def test_media_state_and_remote_control(self):
        phone = SimPhone()
        recorder, session, pump = started(phone)
        self.assertEqual(recorder.media[-1], {"available": True, "player": "Music", "title": "Sinnerman",
                                              "artist": "Nina Simone", "album": "Pastel Blues", "playing": True,
                                              "commands": ["next", "pause", "play", "previous", "toggle"]})
        phone.set_media(ancs.ENTITY_PLAYER, ancs.PLAYER_PLAYBACK_INFO, "0,0.0,40.1")
        pump()
        self.assertFalse(recorder.media[-1]["playing"])
        self.assertTrue(session.media_command("next"))
        self.assertFalse(session.media_command("explode"))
        pump()
        self.assertEqual(phone.media_commands, [3])

    def test_phone_without_media_service_still_mirrors_notifications(self):
        phone = SimPhone(with_media=False)
        recorder, session, pump = started(phone)
        phone.add(1)
        pump()
        self.assertEqual(recorder.states, [("ready", "")])
        self.assertEqual(len(recorder.notifications), 1)
        self.assertEqual(recorder.media, [])
        self.assertFalse(session.media_command("play"))

    def test_phone_without_notification_service(self):
        recorder, _session, _pump = started(SimPhone(with_ancs=False))
        self.assertEqual(recorder.states, [("unsupported", "this phone offers no notification service")])

    def test_permission_is_asked_for_instead_of_failing(self):
        recorder, session, _pump = started(SimPhone(authorized=False))
        self.assertEqual(recorder.states, [("needs-permission", "allow notifications for this computer on the iPhone")])
        self.assertTrue(session.tick())

    def test_the_phones_own_requests_do_not_derail_discovery(self):
        phone = SimPhone()
        phone.ask_something()
        recorder, _session, pump = started(phone)
        phone.ask_something()
        phone.add(1)
        pump()
        self.assertEqual(recorder.states, [("ready", "")])
        self.assertEqual(len(recorder.notifications), 1)
        self.assertEqual(len(phone.errors_from_computer), 2)

    def test_a_silent_phone_is_given_up_on(self):
        recorder = Recorder()
        session = recorder.session(lambda packet: None)       # nothing ever answers
        session.start()
        recorder.now += REQUEST_TIMEOUT - 1
        self.assertTrue(session.tick())
        recorder.now += 2
        self.assertFalse(session.tick())
        self.assertEqual(recorder.states[-1][0], "failed")

    def test_details_that_never_arrive_do_not_block_the_next_notification(self):
        phone = SimPhone()
        recorder, session, pump = started(phone)
        phone.add(1)
        session.feed(phone.outbox.popleft())                  # the event for 1 arrives, its details are requested
        recorder.now += COMMAND_TIMEOUT + 1                   # ... and take too long
        self.assertTrue(session.tick())
        phone.add(2)
        pump()                                                # the late details for 1 arrive before those for 2
        self.assertEqual([n["uid"] for n in recorder.notifications], [2])


if __name__ == "__main__":
    unittest.main()
