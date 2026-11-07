"""The parts with no socket, on a clock the test holds: hours pass in a moment and nothing depends on timing.
tests/data/module_messages.jsonl holds real messages of the boiler, numbers and stages only, signed again here with a test key."""
import asyncio
import json
import os
from pathlib import Path
import random
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "custom_components/centrometal_local"))
sys.path.insert(0, str(ROOT / "centrometal_local_mqtt"))
import commands as c  # noqa: E402
import gateway as g  # noqa: E402
import parameters as p  # noqa: E402
import protocol as proto  # noqa: E402
import session as s  # noqa: E402
import store  # noqa: E402

PASSWORD = b"test-pass"
KEY = proto.signing_key(PASSWORD)
MESSAGES = [json.loads(line) for line in (ROOT / "tests/data/module_messages.jsonl").read_text(encoding="utf-8").splitlines()]
AT = b"AT+CWLIF\r\n"


def mstr(value):
    return len(value).to_bytes(2, "big") + value


def connect(password=PASSWORD):
    return proto.frame(0x10, mstr(b"MQTT") + bytes([4, 0xC2]) + (3600).to_bytes(2, "big") + mstr(b"BOXID") + mstr(b"BOXID") + mstr(password))


def subscribe():
    return proto.frame(0x82, b"\x00\x01" + mstr(b"cm.srv.peltec2.BOXID") + b"\x00")


def up(obj):
    return proto.pub_frame("cm.inst.peltec2.BOXID", proto.sign_obj(obj, KEY))


def read_messages(data):
    """What the session sent, as the module would read it."""
    found = []
    for head, body in proto.MQTTFrames().feed(data):
        if head >> 4 == 3:
            found.append(proto.verify_payload(proto.get_publish(head, body)[1], KEY))
    return found


class Module:
    """A session and a clock. `sent()` gives what went to the module since the last call."""

    def __init__(self, read_interval=60, synced=True):
        self.state = store.State()
        self.book = c.CommandBook()
        self.now = 1000.0
        self.events = []
        self.session = s.LocalSession(self.state, self.book, store.Journal(None),
                                      {"read_interval_seconds": read_interval, "first_poll_delay_seconds": 2.0}, self.now,
                                      lambda name, **fields: self.events.append(name))
        self.session.feed(connect() + subscribe(), self.now)
        self.session.take_output()
        if synced:
            self.say({"_sync": "id", "_token": "t", "clMsgId": 0})
            self.sent()

    def say(self, obj):
        self.session.feed(up(obj), self.now)
        self.session.tick(self.now)

    def sent(self):
        return read_messages(self.session.take_output())

    def run(self, seconds, step=0.5):
        out = []
        for _ in range(int(seconds / step)):
            self.now += step
            self.session.tick(self.now)
            out.extend(self.sent())
            if self.session.close_reason:
                break
        return out


def names(messages):
    return [next(k for k in m if k not in ("srvMsgId", "_sign")) for m in messages]


class SupervisionTests(unittest.TestCase):
    def test_no_sync_closes_the_session_so_the_module_starts_again(self):
        module = Module(synced=False)
        module.say({"B_Tk1": 50, "clMsgId": 1})
        self.assertEqual(module.run(29), [])
        self.assertIsNone(module.session.close_reason)
        module.run(2)
        self.assertEqual(module.session.close_reason, "no _sync within 30 s")

    def test_a_silent_module_is_closed_after_three_minutes(self):
        module = Module()
        module.run(179)
        self.assertIsNone(module.session.close_reason)
        module.run(2)
        self.assertEqual(module.session.close_reason, "module silent for 180 s")

    def test_ping_every_minute_keeps_it_open_for_a_day(self):
        module = Module()
        for _ in range(24 * 60):
            module.run(60, step=5)
            module.say({"PING": 1, "clMsgId": 5})
        self.assertIsNone(module.session.close_reason)

    def test_an_invalid_signature_does_not_count_as_heard(self):
        module = Module()
        forged = proto.pub_frame("cm.inst.peltec2.BOXID", proto.sign_obj({"PING": 1, "clMsgId": 5}, proto.signing_key(b"other")))
        for _ in range(4):
            module.run(50)
            module.session.feed(forged, module.now)
        module.run(1)
        self.assertEqual(module.session.close_reason, "module silent for 180 s")
        self.assertEqual(module.state.invalid_messages, 4)

    def test_only_allowed_messages_ever_leave_during_a_long_run(self):
        module = Module(read_interval=10)
        seen = set()
        for _ in range(500):
            seen.update(names(module.run(10)))
            module.say({"PING": 1, "clMsgId": 5})
            seen.update(names(module.sent()))
        allowed = {"REFRESH", "RSTAT", "PING_ACK"} | {f"PRD {slot}" for slot in p.READ_SLOTS}
        self.assertLessEqual(seen, allowed)


class PowerCommandTests(unittest.TestCase):
    def test_confirmed_only_by_the_echo_of_the_module(self):
        module = Module()
        command = module.book.power(1, module.now)
        module.session.tick(module.now)
        self.assertIn("CMD", names(module.sent()))
        self.assertEqual(command.state, c.PENDING)
        module.say({"B_CMD": 0, "clMsgId": 1})
        self.assertEqual(command.state, c.PENDING)
        module.say({"B_CMD": 1, "clMsgId": 2})
        self.assertEqual(command.state, c.CONFIRMED)
        self.assertTrue(command.finished.is_set())
        self.assertEqual(module.book.summary()["power"]["state"], c.CONFIRMED)

    def test_repeated_then_expired_and_that_is_visible(self):
        module = Module()
        for _ in range(9):
            module.run(10)
            module.say({"PING": 1, "clMsgId": 5})
        command = module.book.power(1, module.now)
        sends = 0
        for _ in range(95):
            module.now += 1
            module.session.tick(module.now)
            sends += names(module.sent()).count("CMD")
            module.say({"PING": 1, "clMsgId": 5})
            module.sent()
        self.assertEqual(command.state, c.EXPIRED)
        self.assertEqual(module.book.summary()["power"]["state"], c.EXPIRED)
        self.assertGreaterEqual(sends, 17)
        self.assertLessEqual(sends, 19)

    def test_a_new_request_replaces_the_old_one_and_the_same_one_joins_it(self):
        book = c.CommandBook()
        first = book.power(1, 0)
        self.assertIs(book.power(1, 1), first)
        second = book.power(0, 2)
        self.assertEqual(first.state, c.REPLACED)
        self.assertEqual(book.pending_power(), 0)
        self.assertIsNot(first, second)

    def test_a_command_taken_during_a_reconnect_waits_for_the_first_answer(self):
        module = Module(synced=False)
        command = module.book.power(1, module.now)
        module.say({"_sync": "id", "_token": "t", "clMsgId": 0})
        module.sent()
        self.assertNotIn("CMD", names(module.run(3)))
        module.say({"B_CMD": 1, "clMsgId": 2})
        self.assertEqual(command.state, c.CONFIRMED)
        self.assertNotIn("CMD", names(module.run(5)))


class SettingTests(unittest.TestCase):
    def fresh(self, module, slot=67, value=67):
        module.say({f"PVAL_{slot}_0": value, "clMsgId": 3})
        module.sent()

    def test_written_read_back_and_confirmed(self):
        module = Module()
        command = module.book.setting(67, 66, 67, 10, module.now)
        module.session.tick(module.now)
        self.assertEqual(names(module.sent())[:2], ["PWR 67", "PRD 67"])
        module.say({"PVAL_67_0": 66, "clMsgId": 3})
        self.assertEqual(command.state, c.CONFIRMED)

    def test_the_value_the_controller_just_reported_is_not_written_again(self):
        module = Module()
        for _ in range(50):
            command = module.book.setting(67, 67, 67.0, 10, module.now)
            self.assertEqual(command.state, c.UNCHANGED)
        module.session.tick(module.now)
        self.assertNotIn("PWR 67", names(module.sent()))

    def test_an_old_reading_is_not_trusted(self):
        module = Module()
        command = module.book.setting(67, 67, 67, c.SETTING_TRUST_SECONDS + 1, module.now)
        self.assertEqual(command.state, c.PENDING)

    def test_requests_in_flight_join_instead_of_writing_again(self):
        module = Module()
        first = module.book.setting(67, 66, 67, 10, module.now)
        self.assertIs(module.book.setting(67, 66, 67, 10, module.now), first)

    def test_written_at_most_three_times_then_expired(self):
        module = Module()
        command = module.book.setting(67, 66, 67, 10, module.now)
        writes = 0
        for _ in range(95):
            module.now += 1
            module.session.tick(module.now)
            writes += names(module.sent()).count("PWR 67")
            module.say({"PING": 1, "clMsgId": 5})
            module.sent()
        self.assertEqual(writes, c.SETTING_ATTEMPTS)
        self.assertEqual(command.state, c.EXPIRED)

    def test_too_many_writes_are_refused(self):
        book = c.CommandBook()
        now = 0.0
        for value in range(c.WRITE_LIMIT):
            command = book.setting(67, 50 + value, None, None, now)
            book.mark_sent(command, now)
            book.observe({"PVAL_67_0": 50 + value}, now)
            now += 1
        self.assertEqual(book.setting(67, 70, None, None, now).state, c.RATE_LIMITED)
        self.assertEqual(book.setting(68, 10, None, None, now).state, c.PENDING, "another slot has its own count")
        self.assertEqual(book.setting(67, 70, None, None, now + c.WRITE_WINDOW_SECONDS + 1).state, c.PENDING)

    def test_k1_is_confirmed_by_its_state(self):
        module = Module()
        command = module.book.setting(p.K1_SWITCH_SLOT, 0, 1, 10, module.now)
        module.session.tick(module.now)
        self.assertEqual(names(module.sent())[:2], ["PWR 272", "REFRESH"])
        module.say({"K1B_onOff": 0, "clMsgId": 3})
        self.assertEqual(command.state, c.CONFIRMED)


class StreamTests(unittest.TestCase):
    """Whatever the WiFi chip does to the stream, and however TCP cuts it, the messages come out in order."""

    def test_real_messages_survive_at_text_and_any_cut(self):
        for seed in range(300):
            rng = random.Random(seed)
            chosen = rng.sample(MESSAGES, 12)
            stream = bytearray()
            for index, message in enumerate(chosen):
                stream += up({**message, "clMsgId": index})
                if rng.random() < .5:
                    stream += AT * rng.randint(1, 8) + b"AT+CW"[:rng.randint(0, 5)]
            frames = proto.ModuleFrames()
            seen = []
            position = 0
            while position < len(stream):
                size = rng.randint(1, 400)
                seen.extend(frames.feed(bytes(stream[position:position + size])))
                position += size
            verified = [proto.verify_payload(proto.get_publish(h, b)[1], KEY) for h, b in seen]
            self.assertEqual([v["clMsgId"] for v in verified if v], list(range(12)), f"seed {seed}")

    def test_at_text_inside_a_message_costs_at_most_that_message(self):
        for seed in range(300):
            rng = random.Random(seed)
            frames_in = [up({**message, "clMsgId": i}) for i, message in enumerate(rng.sample(MESSAGES, 6))]
            hit = rng.randrange(6)
            cut = rng.randint(1, len(frames_in[hit]) - 1)
            frames_in[hit] = frames_in[hit][:cut] + AT * 4
            frames = proto.ModuleFrames()
            seen = frames.feed(b"".join(frames_in) + up({"B_Tk1": 1, "clMsgId": 99}) * 3)
            got = {v["clMsgId"] for h, b in seen if (v := proto.verify_payload(proto.get_publish(h, b)[1], KEY))}
            self.assertLessEqual(set(range(6)) - got, {hit}, f"seed {seed}")

    def test_random_bytes_never_raise_and_the_stream_recovers(self):
        for seed in range(200):
            rng = random.Random(seed)
            frames = proto.ModuleFrames()
            frames.feed(rng.randbytes(rng.randint(1, 3000)))
            frames.feed(b"\x00" * 5000)  # whatever was waiting for more bytes runs out
            seen = frames.feed(up({"B_Tk1": 1, "clMsgId": 1}) + up({"B_Tk1": 2, "clMsgId": 2}))
            self.assertTrue(any(proto.verify_payload(proto.get_publish(h, b)[1], KEY) for h, b in seen), f"seed {seed}")

    def test_the_proxy_copy_reads_past_at_text_as_well(self):
        state = store.State()
        observer = g.MQTTObserver(state, store.Journal(None), g.Session(None), True)
        observer.feed(connect())
        observer.feed(up({"B_Tk1": 50, "clMsgId": 0}))
        observer.feed(AT * 3)
        for index in range(1, 5):
            observer.feed(up({"B_Tk1": 50 + index, "clMsgId": index}))
        self.assertEqual(state.verified_messages, 5)
        self.assertEqual(state.malformed_frames, 1)

    def test_real_messages_pass_the_session_and_every_value_is_accepted(self):
        module = Module()
        for index, message in enumerate(MESSAGES):
            module.say({**message, "clMsgId": index})
            module.sent()
        self.assertEqual(module.state.invalid_messages, 0)
        self.assertEqual(module.state.rejected_values, 0)
        self.assertGreater(len(module.state.values), 40)
        self.assertIsNone(module.session.close_reason)


class ATCommandTests(unittest.TestCase):
    def test_only_the_names_are_kept(self):
        text = b'AT+CWLIF\r\nAT+CWLIF\r\nAT\r\nAT+RST\r\nAT+CWJAP="my-network","my-password"\r\n+++AT+CW'
        self.assertEqual(proto.at_commands(text), {"AT+CWLIF": 2, "AT": 1, "AT+RST": 1, "AT+CWJAP": 1, "+++": 1})

    def test_ordinary_bytes_are_not_commands(self):
        self.assertEqual(proto.at_commands(b"CAT\r\nDATA\r\nATOM\r\nat+rst\r\n\x00\xff"), {})

    def test_nothing_but_the_names_reaches_the_state_or_the_journal(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "j.jsonl")
            state, seen = store.State(), []
            session = s.LocalSession(state, c.CommandBook(), store.Journal(path, debug=True),
                                     {"read_interval_seconds": 60, "first_poll_delay_seconds": 2.0}, 0.0,
                                     lambda name, **fields: seen.append((name, fields)))
            session.feed(connect() + subscribe(), 0.0)
            session.take_output()
            session.feed(b'AT+CWJAP="my-network","my-password"\r\n' + AT * 3 + up({"B_Tk1": 61, "clMsgId": 1}), 1.0)
            self.assertEqual(state.at_commands, {"AT+CWJAP": 1, "AT+CWLIF": 3})
            self.assertIn("AT text: AT+CWJAP x1, AT+CWLIF x3", state.last_malformed)
            self.assertEqual(seen[-1][1]["commands"], {"AT+CWJAP": 1, "AT+CWLIF": 3})
            self.assertEqual(state.values["B_Tk1"], 61)
            for text in (json.dumps(state.meta()), json.dumps(seen), state.values_payload()):
                self.assertNotIn("my-network", text)
                self.assertNotIn("my-password", text)

    def test_they_are_never_answered(self):
        module = Module()
        module.session.feed(AT * 40, module.now)
        module.session.tick(module.now)
        self.assertEqual(module.session.take_output(), b"")
        self.assertEqual(module.state.at_commands, {"AT+CWLIF": 40})

    def test_totals_survive_a_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "values.json")
            state = store.State(values_path=path)
            state.ingest({"B_Tk1": 60, "clMsgId": 1})
            state.note_skipped(1, AT * 2)
            state.save_values()
            self.assertEqual(store.State(values_path=path).at_commands, {"AT+CWLIF": 2})

    def test_the_proxy_copy_keeps_them_as_well(self):
        state = store.State()
        observer = g.MQTTObserver(state, store.Journal(None), g.Session(None), True)
        observer.feed(connect())
        observer.feed(AT * 2 + up({"B_Tk1": 50, "clMsgId": 0}))
        self.assertEqual(state.at_commands, {"AT+CWLIF": 2})


class SkippedBytesTests(unittest.TestCase):
    SECRET = b'AT+CWSAP="hotspot-name","hotspot-password",5,3\r\n'

    def test_they_reach_nothing_even_with_debug_on(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "j.jsonl")
            state = store.State(store.Journal(path, debug=True))
            state.note_skipped(1, b'{"_token":"abc"}' + self.SECRET)
            self.assertEqual(state.at_commands, {"AT+CWSAP": 1})
            journal = Path(path).read_text(encoding="utf-8") if os.path.exists(path) else ""
            for text in (journal, json.dumps(state.meta()), state.values_payload(), state.last_malformed):
                self.assertNotIn("abc", text)
                self.assertNotIn("hotspot", text)

    def test_a_run_that_continues_in_the_next_segment_is_reported(self):
        module = Module()
        module.session.feed(AT * 2 + b"AT+CW", module.now)
        first = module.state.last_malformed
        module.session.feed(b"LIF\r\n" + AT, module.now)
        self.assertEqual(module.state.malformed_frames, 1)
        self.assertEqual(module.session.frames.skipped, bytearray())
        self.assertIn("AT+CWLIF x2", first)
        self.assertIn("AT+CWLIF x1", module.state.last_malformed)


class RepeatedMessageTests(unittest.TestCase):
    """The numbers and stages are from the night of 06-07.11.2026: D5 at 04:18:19, then a repeat of the D4 from 04:17:19."""

    def test_an_old_stage_does_not_overwrite_the_new_one(self):
        state = store.State()
        self.assertTrue(state.ingest({"B_STATE": "D4", "clMsgId": 2041711}))
        self.assertTrue(state.ingest({"B_STATE": "D5", "clMsgId": 2041717}))
        moment = state.value_times["B_STATE"]
        self.assertFalse(state.ingest({"B_FILE": "x", "clMsgId": 2041695}))
        self.assertFalse(state.ingest({"B_STATE": "D4", "clMsgId": 2041711}))
        self.assertEqual(state.values["B_STATE"], "D5")
        self.assertEqual(state.value_times["B_STATE"], moment)
        self.assertNotIn("B_FILE", state.values)
        self.assertEqual(state.stale_messages, 2)
        self.assertEqual(state.meta()["stale_messages"], 2)
        self.assertTrue(state.ingest({"B_STATE": "D6", "clMsgId": 2041731}))
        self.assertEqual(state.values["B_STATE"], "D6")

    def test_the_same_number_twice_is_a_repeat(self):
        state = store.State()
        state.ingest({"B_Tk1": 60, "clMsgId": 7})
        self.assertFalse(state.ingest({"B_Tk1": 60, "clMsgId": 7}))
        self.assertEqual(state.stale_messages, 1)

    def test_a_numbering_that_started_over_is_accepted(self):
        state = store.State()
        state.ingest({"B_Tk1": 60, "clMsgId": 2041725})
        self.assertTrue(state.ingest({"B_Tk1": 61, "clMsgId": 2041725 - store.RETRANSMIT_WINDOW}))
        self.assertEqual(state.values["B_Tk1"], 61)
        self.assertTrue(state.ingest({"B_Tk1": 62, "clMsgId": 5}))
        self.assertTrue(state.ingest({"B_Tk1": 63, "clMsgId": 6}))
        self.assertEqual(state.values["B_Tk1"], 63)
        self.assertEqual(state.stale_messages, 0)

    def test_a_message_without_a_number_is_accepted(self):
        state = store.State()
        state.ingest({"B_Tk1": 60, "clMsgId": 10})
        self.assertTrue(state.ingest({"B_Tk1": 61}))
        self.assertTrue(state.ingest({"B_Tk1": 62, "clMsgId": "9"}))
        self.assertEqual(state.values["B_Tk1"], 62)

    def test_an_ended_alarm_does_not_come_back(self):
        state = store.State()
        state.ingest({"E12": "A", "clMsgId": 1})
        state.ingest({"E12": "P", "clMsgId": 2})
        state.ingest({"E12": "A", "clMsgId": 1})
        self.assertEqual(state.active_alarms, {})

    def test_the_count_survives_a_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "values.json")
            state = store.State(values_path=path)
            state.ingest({"B_Tk1": 60, "clMsgId": 2})
            state.ingest({"B_Tk1": 60, "clMsgId": 1})
            state.save_values()
            self.assertEqual(store.State(values_path=path).stale_messages, 1)

    def test_an_old_switch_state_does_not_turn_the_switch_back(self):
        module = Module()
        module.say({"B_CMD": 0, "clMsgId": 100})
        module.say({"B_CMD": 1, "clMsgId": 110})
        module.say({"B_CMD": 0, "clMsgId": 100})
        self.assertEqual(module.state.values["B_CMD"], 1)

    def test_an_old_echo_does_not_confirm_a_command(self):
        module = Module()
        module.say({"B_CMD": 1, "clMsgId": 100})
        module.say({"B_CMD": 0, "clMsgId": 110})
        command = module.book.power(1, module.now)
        module.session.tick(module.now)
        module.sent()
        module.say({"B_CMD": 1, "clMsgId": 100})
        self.assertEqual(command.state, c.PENDING)
        module.say({"B_CMD": 1, "clMsgId": 111})
        self.assertEqual(command.state, c.CONFIRMED)

    def test_a_repeated_ping_is_still_answered_and_counts_as_heard(self):
        module = Module()
        module.say({"PING": 1, "clMsgId": 100})
        module.say({"PING": 2, "clMsgId": 110})
        module.sent()
        module.now += 50
        module.say({"PING": 1, "clMsgId": 100})
        self.assertIn({"PING_ACK": 1}, [{k: v for k, v in m.items() if k == "PING_ACK"} for m in module.sent()])
        self.assertEqual(module.session.last_heard, module.now)

    def test_the_proxy_copy_ignores_them_as_well(self):
        state = store.State()
        observer = g.MQTTObserver(state, store.Journal(None), g.Session(None), True)
        observer.feed(connect())
        observer.feed(up({"B_STATE": "D5", "clMsgId": 20}) + up({"B_STATE": "D4", "clMsgId": 10}))
        self.assertEqual(state.values["B_STATE"], "D5")
        self.assertEqual(state.stale_messages, 1)


class ValueCheckTests(unittest.TestCase):
    def test_a_known_number_of_the_wrong_type_is_dropped_and_counted(self):
        state = store.State()
        state.ingest({"B_Tak1_1": "abc", "B_Tk1": "61.5", "B_STATE": "D3", "SE00": "whatever", "clMsgId": 1})
        self.assertEqual(state.rejected_values, 1)
        self.assertNotIn("B_Tak1_1", state.values)
        self.assertEqual((state.values["B_Tk1"], state.values["B_STATE"], state.values["SE00"]), ("61.5", "D3", "whatever"))

    def test_the_stop_buffer_tank_difference_is_a_setting(self):
        self.assertEqual(p.LIMITS[71], (3, 30))
        self.assertIn(71, p.SETTINGS)
        self.assertEqual(set(p.READ_SLOTS), set(p.LIMITS))
        self.assertFalse(p.is_entity("PVAL_71_0"))

    def test_a_setting_read_an_hour_ago_is_still_current(self):
        self.assertTrue(p.is_fresh(537, "PVAL_67_0"))
        self.assertTrue(p.is_fresh(3700, "PVAL_67_0"))
        self.assertFalse(p.is_fresh(5000, "PVAL_67_0"))
        self.assertFalse(p.is_fresh(537, "B_Tak1_1"))
        self.assertTrue(p.is_fresh(60))

    def test_steps_of_the_boiler_panel(self):
        self.assertEqual([p.setting_step(s) for s in (67, 68, 70, 71, 291, 292, 576, p.K1_SWITCH_SLOT)], [1] * 8)

    def test_mode_and_priority_are_read_from_pval_or_the_regular_message(self):
        self.assertEqual(p.setting_value({"PVAL_4_0": 3, "B_PTV/GRI_SEL": 0}, 4, "B_PTV/GRI_SEL"), 3)
        self.assertEqual(p.setting_value({"B_PTV/GRI_SEL": 2}, 4, "B_PTV/GRI_SEL"), 2)
        self.assertEqual(p.setting_value({"B_PTV_PRI": 1}, 898, "B_PTV_PRI"), 1)
        self.assertIsNone(p.setting_value({}, 898, "B_PTV_PRI"))

    def test_dhw_heating_mode_has_names(self):
        self.assertEqual([p.display_value("B_PTV/GRI_SEL", v) for v in (0, 1, 2, 3)],
                         ["DHW + heating", "DHW only", "Heating only", "Auto"])
        self.assertEqual(p.display_value("B_PTV/GRI", 0), "DHW + heating")
        self.assertEqual(p.display_value("B_PTV/GRI_SEL", 7), "7")

    def test_one_list_of_limits_for_the_app_and_the_integration(self):
        self.assertIs(g.SETTINGS, p.LIMITS)
        self.assertEqual(set(p.SETTINGS) | {p.K1_SWITCH_SLOT, p.DHW_MODE_SLOT, p.DHW_PRIORITY_SLOT}, set(p.LIMITS))
        self.assertEqual(p.setting_key(p.K1_SWITCH_SLOT), "K1B_onOff")
        self.assertEqual(p.setting_key(67), "PVAL_67_0")


class DeltaTests(unittest.TestCase):
    def test_only_what_changed_travels(self):
        state = store.State()
        state.ingest({"B_Tk1": 60, "B_Tak1_1": 55, "clMsgId": 1})
        since = state.revision
        state.ingest({"B_Tk1": 60, "B_Tak1_1": 56, "clMsgId": 2})
        delta = state.delta(since)
        self.assertEqual(delta["values"], {"B_Tak1_1": 56})
        self.assertEqual(delta["seen"], ["B_Tk1"])
        self.assertEqual(state.delta(state.revision)["values"], {})


class PersistenceTests(unittest.TestCase):
    def test_counters_survive_a_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "values.json")
            state = store.State(values_path=path)
            state.ingest({"B_Tk1": 60, "clMsgId": 1})
            state.malformed_frames = 3
            state.last_session_end = "ValueError: x"
            state.note_session_end()
            state.note_session_end()
            state.save_values()
            again = store.State(values_path=path)
            self.assertEqual((again.sessions_closed, again.malformed_frames), (2, 3))
            self.assertEqual(again.close_reasons, {"ValueError": 2})

    def test_a_write_interrupted_before_the_rename_leaves_the_old_file(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "f.json")
            store.atomic_write(path, "old")
            Path(path + ".tmp").write_text("half a fi")
            self.assertEqual(Path(path).read_text(), "old")
            store.atomic_write(path, "new")
            self.assertEqual(Path(path).read_text(), "new")
            self.assertFalse(Path(path + ".tmp").exists())


class IdentityTests(unittest.TestCase):
    def test_first_module_is_remembered_and_a_different_one_is_counted_not_refused(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "identity.json")
            state = store.State(identity_path=path)
            state.note_module_identity(b"BOXID", PASSWORD)
            state.note_module_identity(b"BOXID", PASSWORD)
            self.assertEqual(state.identity_changes, 0)
            again = store.State(identity_path=path)
            again.note_module_identity(b"BOXID", b"another")
            self.assertEqual(again.identity_changes, 1)
            text = Path(path).read_text()
            self.assertNotIn("BOXID", text)
            self.assertNotIn(PASSWORD.decode(), text)

    def test_a_session_with_another_password_still_works_for_now(self):
        module = Module()
        other = s.LocalSession(module.state, module.book, store.Journal(None),
                               {"read_interval_seconds": 60, "first_poll_delay_seconds": 2.0}, module.now, lambda *a, **k: None)
        other.feed(connect(b"changed"), module.now)
        self.assertEqual(module.state.identity_changes, 1)
        self.assertEqual(other.take_output(), proto.frame(0x20, b"\x00\x00"))


class AlarmsAreReadOnlyTests(unittest.TestCase):
    def test_no_way_to_end_an_alarm_from_here(self):
        self.assertFalse(hasattr(store.State, "clear_alarms"))
        self.assertNotIn(b'"/alarms', (ROOT / "centrometal_local_mqtt/gateway.py").read_bytes())
        state = store.State()
        state.ingest({"E8.1": "A", "clMsgId": 1})
        for _ in range(100):
            state.ingest({"B_Tk1": 60, "clMsgId": 2})
        self.assertEqual(state.active_alarms, {"E8.1": "A"})
        state.ingest({"E8.1": "P", "clMsgId": 3})
        self.assertEqual(state.active_alarms, {})


class NoAccessToHomeAssistantTests(unittest.TestCase):
    def test_the_app_touches_nothing_of_home_assistant(self):
        app = ROOT / "centrometal_local_mqtt"
        self.assertNotIn(b"homeassistant_config", (app / "config.yaml").read_bytes())
        self.assertNotIn(b"/homeassistant", (app / "apparmor.txt").read_bytes())
        self.assertNotIn(b"/homeassistant", (app / "gateway.py").read_bytes())

    def test_app_and_integration_and_hacs_agree(self):
        manifest = json.loads((ROOT / "custom_components/centrometal_local/manifest.json").read_text())
        config = (ROOT / "centrometal_local_mqtt/config.yaml").read_text()
        self.assertIn(f"\nversion: {manifest['version']}\n", config)
        self.assertEqual(g.Gateway({"connection_mode": "local", "read_interval_seconds": 60}).status()["api_version"], p.API_VERSION)
        self.assertEqual(sorted(path.name for path in (ROOT / "custom_components").iterdir() if "__pycache__" not in path.name), ["centrometal_local"])


class ApiTests(unittest.IsolatedAsyncioTestCase):
    def config(self, **overrides):
        return {"module_ip": "127.0.0.1", "connection_mode": "local", "read_interval_seconds": 60,
                "first_poll_delay_seconds": 60, **overrides}

    async def serve(self, gateway):
        server = await asyncio.start_server(lambda r, w: g.send_http_status(r, w, gateway), "127.0.0.1", 0)
        self.addCleanup(server.close)
        return server.sockets[0].getsockname()[1]

    async def call(self, port, raw):
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        self.addCleanup(writer.close)
        writer.write(raw)
        await writer.drain()
        return await asyncio.wait_for(reader.read(), 25)

    def post(self, path, obj):
        body = json.dumps(obj, separators=(",", ":")).encode()
        return b"POST %s HTTP/1.1\r\nContent-Length: %d\r\n\r\n%s" % (path, len(body), body)

    async def test_health_needs_no_token(self):
        gateway = g.Gateway(self.config(api_token="x"))
        port = await self.serve(gateway)
        self.assertIn(b"200 OK", await self.call(port, b"GET /health HTTP/1.1\r\n\r\n"))
        self.assertIn(b"401", await self.call(port, b"GET /events HTTP/1.1\r\n\r\n"))

    async def test_the_answer_says_what_came_of_the_command(self):
        gateway = g.Gateway(self.config())
        gateway.state.ingest({"B_CMD": 0, "clMsgId": 1})
        port = await self.serve(gateway)
        task = asyncio.create_task(self.call(port, self.post(b"/command", {"CMD": 1, "wait": 5})))
        await asyncio.sleep(.2)
        self.assertFalse(task.done(), "waits for the module")
        gateway.commands.observe({"B_CMD": 1}, 0)
        raw = await task
        self.assertEqual(json.loads(raw.split(b"\r\n\r\n", 1)[1])["state"], "confirmed")

    async def test_without_wait_the_answer_comes_at_once(self):
        gateway = g.Gateway(self.config())
        gateway.state.ingest({"B_CMD": 0, "clMsgId": 1})
        raw = await self.call(await self.serve(gateway), self.post(b"/command", {"CMD": 1}))
        self.assertEqual(json.loads(raw.split(b"\r\n\r\n", 1)[1]), {"accepted": True, "id": 1, "state": "pending"})

    async def test_a_setting_over_the_limit_is_refused_with_its_reason(self):
        gateway = g.Gateway(self.config())
        gateway.state.ingest({"B_Tk1": 60, "clMsgId": 1})
        gateway.commands._writes[67] = [__import__("time").monotonic()] * c.WRITE_LIMIT
        raw = await self.call(await self.serve(gateway), self.post(b"/setting", {"slot": 67, "value": 66}))
        self.assertIn(b"409", raw)
        self.assertEqual(json.loads(raw.split(b"\r\n\r\n", 1)[1])["state"], "rate_limited")

    async def test_expired_command_is_counted_and_shown(self):
        gateway = g.Gateway(self.config())
        gateway.state.ingest({"B_CMD": 0, "clMsgId": 1})
        gateway.commands.give_up_seconds = 0
        self.assertTrue(gateway.command(1))
        status = gateway.status()
        self.assertIsNone(status["pending_command"])
        self.assertEqual(status["commands"]["power"]["state"], "expired")
        self.assertEqual(status["commands_expired"], 1)

    async def test_events_give_everything_once_then_only_changes_and_a_heartbeat(self):
        gateway = g.Gateway(self.config(event_heartbeat_seconds=.2))
        gateway.state.ingest({"B_Tk1": 60, "B_Tak1_1": 55, "clMsgId": 1})
        reader, writer = await asyncio.open_connection("127.0.0.1", await self.serve(gateway))
        self.addCleanup(writer.close)
        writer.write(b"GET /events HTTP/1.1\r\n\r\n")
        await writer.drain()

        async def next_event():
            while True:
                line = await asyncio.wait_for(reader.readline(), 3)
                if line.startswith(b"data: "):
                    return json.loads(line[6:])
        first = await next_event()
        self.assertEqual((first["type"], first["values"]["B_Tk1"]), ("snapshot", 60))
        self.assertIn("value_age_seconds", first)
        gateway.state.ingest({"B_Tk1": 61, "B_Tak1_1": 55, "clMsgId": 2})
        event = await next_event()
        self.assertEqual((event["type"], event["values"], event["seen"]), ("delta", {"B_Tk1": 61}, ["B_Tak1_1"]))
        event = await next_event()
        self.assertEqual((event["type"], event["values"], event["seen"]), ("delta", {}, []))
        self.assertIn("commands", event)


class DiscoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_message_the_supervisor_expects(self):
        received = []

        async def supervisor(reader, writer):
            head = await reader.readuntil(b"\r\n\r\n")
            length = int(next(x for x in head.decode().split("\r\n") if x.lower().startswith("content-length")).split(":")[1])
            received.append((head, json.loads(await reader.readexactly(length))))
            writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}')
            await writer.drain()
            writer.close()
        server = await asyncio.start_server(supervisor, "127.0.0.1", 0)
        self.addCleanup(server.close)
        url = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/discovery"
        self.assertTrue(await asyncio.to_thread(g.send_discovery, url, "su", "5c24f09b-centrometal-local-mqtt", 8765, "tok"))
        head, body = received[0]
        self.assertIn(b"Authorization: Bearer su", head)
        self.assertEqual(body, {"service": "centrometal_local", "config": {"host": "5c24f09b-centrometal-local-mqtt", "port": 8765, "token": "tok"}})

    async def test_failure_is_only_a_warning(self):
        import logging
        logging.disable(logging.CRITICAL)
        self.assertFalse(await asyncio.to_thread(g.send_discovery, "http://127.0.0.1:1/discovery", "su", "h", 1, "t", 1))


if __name__ == "__main__":
    unittest.main()
