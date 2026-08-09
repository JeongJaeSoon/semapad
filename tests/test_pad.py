from __future__ import annotations

import gc
import inspect
import threading
from dataclasses import FrozenInstanceError, dataclass, fields

import pytest

from semapad import pad, protocol


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        # A zero-duration poll must still service one ready run-loop source.
        self.now += seconds if seconds > 0 else 0.001


@dataclass
class Token:
    name: str


class FakeBackend:
    def __init__(self, raw_transport: str = "USB") -> None:
        self.raw_transport = raw_transport
        self.present = True
        self.clock = FakeClock()
        self.events: list[object] = []
        self.writes: list[tuple[int, int, bytes]] = []
        self.incoming: list[bytes] = []
        self.sent_messages: list[dict] = []
        self._write_decoder = protocol.FrameDecoder()
        self.on_report = None
        self.on_remove = None
        self.registration = None
        self.fail_write_numbers: set[int] = set()
        self.fail_pump = False
        self.fail_cleanup: set[str] = set()
        self.close_result = 0
        self.auto_status = False
        self.status_script = None
        self._write_number = 0

    def acquire(self, vid: int, pid: int):
        self.events.append(("acquire", vid, pid))
        if not self.present:
            return None
        return pad._AcquiredDevice(Token("device"), self.raw_transport)

    def register(self, device, on_report, on_remove):
        self.events.append("register")
        self.on_report = on_report
        self.on_remove = on_remove
        self.registration = pad._Registration(
            input_buffer=Token("buffer"),
            input_callback=Token("input-callback"),
            removal_callback=Token("removal-callback"),
            run_loop=Token("run-loop"),
            mode=Token("mode"),
        )
        return self.registration

    def set_report(self, device, report_type: int, report_id: int,
                   packet: bytes) -> int:
        self._write_number += 1
        self.events.append(("write", self._write_number))
        self.writes.append((report_type, report_id, bytes(packet)))
        if self._write_number in self.fail_write_numbers:
            return 0xE00002ED

        for message in self._write_decoder.feed(packet):
            self.sent_messages.append(message)
            if message.get("m") == "device.status":
                if self.status_script is not None:
                    replies = self.status_script(message)
                    for reply in replies:
                        self.queue_message(reply)
                elif self.auto_status:
                    self.queue_message({
                        "result": {"version": "test", "layer_index": 1},
                        "id": message["id"],
                        "method": "device.status",
                    })
        return 0

    def queue_message(self, message: dict) -> None:
        self.incoming.extend(protocol.frame(message, self._transport()))

    def queue_raw(self, report: bytes) -> None:
        self.incoming.append(report)

    def _transport(self) -> str:
        return protocol.normalize_transport(self.raw_transport)

    def pump(self, registration, seconds: float) -> None:
        self.events.append(("pump", seconds))
        self.clock.advance(seconds)
        if self.fail_pump:
            raise RuntimeError("pump failed")
        incoming, self.incoming = self.incoming, []
        for report in incoming:
            self.on_report(0, pad.REPORT_ID, report)

    def remove(self, result: int = 0) -> None:
        assert self.on_remove is not None
        self.on_remove(result)

    def unregister(self, device, registration) -> None:
        self.events.append("unregister")
        if "unregister" in self.fail_cleanup:
            raise RuntimeError("unregister failed")

    def unschedule(self, device, registration) -> None:
        self.events.append("unschedule")
        if "unschedule" in self.fail_cleanup:
            raise RuntimeError("unschedule failed")

    def close(self, device) -> int:
        self.events.append("close")
        if "close" in self.fail_cleanup:
            raise RuntimeError("close failed")
        return self.close_result

    def release(self, device) -> None:
        self.events.append("release")
        if "release" in self.fail_cleanup:
            raise RuntimeError("release failed")


class LowLevelIOKit:
    """Record raw callback lifecycle calls without loading a macOS framework."""

    def __init__(self, fail_at: str | None = None) -> None:
        self.fail_at = fail_at
        self.fail_clear_at: str | None = None
        self.events: list[tuple] = []

    def _fail_once_when_setting(self, operation: str, callback) -> None:
        if self.fail_at == operation and callback is not None:
            self.fail_at = None
            raise RuntimeError(f"{operation} failed")

    def IOHIDDeviceRegisterInputReportCallback(
            self, device, buffer, length, callback, context):
        action = "set" if callback is not None else "clear"
        self.events.append(("input", action, buffer, length))
        if self.fail_clear_at == "input" and callback is None:
            self.fail_clear_at = None
            raise RuntimeError("input clear failed")
        self._fail_once_when_setting("input", callback)

    def IOHIDDeviceRegisterRemovalCallback(
            self, device, callback, context):
        action = "set" if callback is not None else "clear"
        self.events.append(("removal", action))
        if self.fail_clear_at == "removal" and callback is None:
            self.fail_clear_at = None
            raise RuntimeError("removal clear failed")
        self._fail_once_when_setting("removal", callback)

    def IOHIDDeviceScheduleWithRunLoop(self, device, run_loop, mode):
        self.events.append(("schedule",))
        if self.fail_at == "schedule":
            self.fail_at = None
            raise RuntimeError("schedule failed")

    def IOHIDDeviceUnscheduleFromRunLoop(self, device, run_loop, mode):
        self.events.append(("unschedule",))


class LowLevelCF:
    @staticmethod
    def CFRunLoopGetCurrent():
        return 123


def low_level_backend(fail_at: str | None = None):
    backend = pad._IOKitBackend.__new__(pad._IOKitBackend)
    backend._iokit = LowLevelIOKit(fail_at)
    backend._cf = LowLevelCF()
    backend._default_mode = 456
    return backend


def open_fake(backend: FakeBackend | None = None) -> tuple[pad.Pad, FakeBackend]:
    backend = backend or FakeBackend()
    device = pad.Pad.open(backend=backend, clock=backend.clock)
    assert device is not None
    return device, backend


def decoded_writes(backend: FakeBackend) -> list[dict]:
    decoder = protocol.FrameDecoder()
    messages: list[dict] = []
    for _, _, packet in backend.writes:
        messages.extend(decoder.feed(packet))
    return messages


def test_constants_are_the_measured_vendor_channel():
    assert (pad.VID, pad.PID, pad.REPORT_ID) == (0x303A, 0x8360, 6)


def test_our_own_acks_are_not_vendor_writes():
    assert pad.is_vendor_write(
        {"result": {"ok": 1}, "id": None, "method": "v.oai.rgbcfg"}) is False


def test_id_bearing_acks_are_vendor_writes():
    assert pad.is_vendor_write(
        {"result": {"ok": 1}, "id": 679, "method": "v.oai.rgbcfg"}) is True
    assert pad.is_vendor_write(
        {"result": {"ok": 1}, "id": 177, "method": "v.oai.thstatus"}) is True


def test_key_events_and_status_replies_are_not_vendor_writes():
    assert pad.is_vendor_write(
        {"m": "v.oai.hid", "p": {"k": "AG00", "act": 1}}) is False
    assert pad.is_vendor_write({
        "result": {"version": "v0.4.1"}, "id": 1,
        "method": "device.status",
    }) is False


@pytest.mark.parametrize("message", [None, [], ["v.oai.rgbcfg"], 1, "reply"])
def test_non_object_json_is_not_a_vendor_write(message):
    assert pad.is_vendor_write(message) is False


@pytest.mark.parametrize("method", [None, [], {}, 1, True])
def test_malformed_vendor_method_is_rejected_without_raising(method):
    assert pad.is_vendor_write({"id": 1, "method": method}) is False


@pytest.mark.parametrize("request_id", [None, True, 1.0, "1", [], {}])
def test_non_integer_vendor_id_is_rejected_without_raising(request_id):
    assert pad.is_vendor_write({
        "id": request_id, "method": "v.oai.rgbcfg",
    }) is False


def test_open_matches_only_exact_vid_pid_and_normalizes_transport():
    device, backend = open_fake(FakeBackend(" Bluetooth Low Energy "))
    assert backend.events[:2] == [
        ("acquire", 0x303A, 0x8360), "register",
    ]
    assert device.transport == protocol.BLE


def test_iokit_source_does_not_use_manager_or_product_or_usage_matching():
    source = inspect.getsource(pad._IOKitBackend.acquire)
    assert "IOHIDManager" not in source
    assert '"Product"' not in source
    assert "UsagePage" not in source
    assert '"VendorID"' in source and '"ProductID"' in source


def test_open_returns_none_when_device_is_absent():
    backend = FakeBackend()
    backend.present = False
    assert pad.Pad.open(backend=backend, clock=backend.clock) is None
    assert backend.events == [("acquire", pad.VID, pad.PID)]


def test_unknown_transport_fails_closed_and_releases_device():
    backend = FakeBackend("Serial")
    with pytest.raises(pad.PadTransportError, match="Serial"):
        pad.Pad.open(backend=backend, clock=backend.clock)
    assert backend.events[-2:] == ["close", "release"]
    assert backend.writes == []


@pytest.mark.parametrize(
    ("raw_transport", "transport", "size", "prefix"),
    [
        ("USB", protocol.USB, 63, b"\x02"),
        ("Bluetooth Low Energy", protocol.BLE, 64, b"\x06\x02"),
    ],
)
def test_send_uses_report_six_and_transport_specific_framing(
        raw_transport, transport, size, prefix):
    device, backend = open_fake(FakeBackend(raw_transport))
    device.send({"m": "x"})
    report_type, report_id, packet = backend.writes[0]
    assert device.transport == transport
    assert (report_type, report_id) == (pad._OUTPUT_REPORT, 6)
    assert len(packet) == size and packet.startswith(prefix)


def test_long_send_is_serialized_as_one_complete_packet_sequence():
    device, backend = open_fake()
    message = protocol.thstatus([0x010203] * 6)
    device.send(message)
    assert decoded_writes(backend) == [message]


def test_set_report_error_is_not_treated_as_delivery_success():
    device, backend = open_fake()
    backend.fail_write_numbers.add(1)
    with pytest.raises(pad.PadIOError, match="IOHIDDeviceSetReport") as error:
        device.send({"m": "x"})
    assert error.value.code == 0xE00002ED


def test_callbacks_buffer_runloop_and_mode_are_kept_alive_by_pad():
    device, backend = open_fake()
    registration = backend.registration
    gc.collect()
    assert device._input_buffer is registration.input_buffer
    assert device._input_callback is registration.input_callback
    assert device._removal_callback is registration.removal_callback
    assert device._run_loop is registration.run_loop
    assert device._run_loop_mode is registration.mode


def test_unregister_keeps_original_input_buffer_non_null():
    backend = low_level_backend()
    registration = backend.register(object(), lambda *_: None, lambda *_: None)
    backend.unregister(object(), registration)
    input_clear = [event for event in backend._iokit.events
                   if event[:2] == ("input", "clear")]
    assert len(input_clear) == 1
    assert input_clear[0][2] is registration.input_buffer
    assert input_clear[0][3] == len(registration.input_buffer)


@pytest.mark.parametrize("fail_clear_at", ["input", "removal"])
def test_unregister_attempts_both_callback_clears_when_one_raises(
        fail_clear_at):
    backend = low_level_backend()
    registration = backend.register(object(), lambda *_: None, lambda *_: None)
    backend._iokit.fail_clear_at = fail_clear_at
    with pytest.raises(RuntimeError, match=f"{fail_clear_at} clear failed"):
        backend.unregister(object(), registration)
    assert [event[:2] for event in backend._iokit.events[-2:]] == [
        ("input", "clear"), ("removal", "clear"),
    ]


@pytest.mark.parametrize(
    ("fail_at", "expected_tail"),
    [
        ("input", [("input", "clear")]),
        ("removal", [("input", "clear"), ("removal", "clear")]),
        ("schedule", [
            ("input", "clear"), ("removal", "clear"), ("unschedule",),
        ]),
    ],
)
def test_registration_failure_rolls_back_every_partial_step(
        fail_at, expected_tail):
    backend = low_level_backend(fail_at)
    with pytest.raises(RuntimeError, match=f"{fail_at} failed"):
        backend.register(object(), lambda *_: None, lambda *_: None)
    simplified = [event[:2] if event[0] == "input" else event
                  for event in backend._iokit.events]
    assert simplified[-len(expected_tail):] == expected_tail


def test_poll_services_runloop_and_decodes_input_report():
    device, backend = open_fake()
    backend.queue_message({"m": "v.oai.hid", "p": {"k": "AG00", "act": 1}})
    assert device.poll(0.1) == [
        {"m": "v.oai.hid", "p": {"k": "AG00", "act": 1}},
    ]
    pumps = [event for event in backend.events
             if isinstance(event, tuple) and event[0] == "pump"]
    assert pumps and sum(event[1] for event in pumps) == pytest.approx(0.1)


def test_poll_received_preserves_report_order_times_and_fixed_metadata_shape():
    device, backend = open_fake()
    messages = [
        {"m": "v.oai.hid", "p": {"k": "AG00", "act": 1}},
        {"m": "v.oai.hid", "p": {"k": "AG01", "act": 1}},
        {"m": "v.oai.hid", "p": {"k": "AG01", "act": 1}},
        {"m": "v.oai.hid", "p": {"k": "AG01", "act": 0}},
    ]
    received_at = [101.0, 101.04, 101.09, 101.12]

    for message, timestamp in zip(messages, received_at, strict=True):
        reports = protocol.frame(message, protocol.USB)
        assert len(reports) == 1
        backend.clock.now = timestamp
        backend.on_report(0, pad.REPORT_ID, reports[0])

    received = device.poll_received(0)
    assert [item.message for item in received] == messages
    assert [item.received_at for item in received] == received_at
    assert {item.connection_epoch for item in received} == {device.epoch}
    assert [field.name for field in fields(pad.ReceivedMessage)] == [
        "message", "received_at", "connection_epoch",
    ]
    assert not hasattr(received[0], "__dict__")
    with pytest.raises(FrozenInstanceError):
        received[0].received_at = 999.0


def test_poll_received_uses_the_last_fragment_time_for_multi_report_json():
    device, backend = open_fake()
    message = {
        "m": "v.oai.hid",
        "p": {"k": "AG00", "act": 1, "padding": "x" * 180},
    }
    reports = protocol.frame(message, protocol.USB)
    assert len(reports) > 1

    for index, report in enumerate(reports):
        backend.clock.now = 200.0 + index * 0.025
        backend.on_report(0, pad.REPORT_ID, report)

    received = device.poll_received(0)
    assert [item.message for item in received] == [message]
    assert received[0].received_at == pytest.approx(
        200.0 + (len(reports) - 1) * 0.025)
    assert received[0].connection_epoch == device.epoch


def test_legacy_poll_still_returns_plain_decoded_dicts():
    device, backend = open_fake()
    message = {"m": "v.oai.hid", "p": {"k": "AG00", "act": 1}}
    backend.queue_message(message)
    result = device.poll(0)
    assert result == [message]
    assert type(result[0]) is dict


def test_non_vendor_report_id_is_ignored():
    device, backend = open_fake()
    report = protocol.frame({"m": "foreign"}, protocol.USB)[0]
    backend.on_report(0, 5, report)
    assert device.poll(0) == []


@pytest.mark.parametrize("value", [None, [], [1, 2], 7, "valid json"])
def test_poll_drops_json_values_that_are_not_protocol_objects(value):
    device, backend = open_fake()
    backend.queue_message(value)
    assert device.poll(0) == []


def test_request_correlates_unique_ids_and_preserves_unrelated_messages():
    device, backend = open_fake()

    def scripted(request):
        req_id = request["id"]
        return [
            {"result": {"stale": True}, "id": req_id - 1,
             "method": "device.status"},
            {"result": {"wrong_method": True}, "id": req_id,
             "method": "other.method"},
            {"result": {"version": "ok", "layer_index": 1}, "id": req_id,
             "method": "device.status"},
        ]

    backend.status_script = scripted
    first = device.status(timeout=0.2)
    second = device.status(timeout=0.2)

    assert first["result"]["version"] == "ok"
    assert second["result"]["version"] == "ok"
    ids = [message["id"] for message in backend.sent_messages
           if message.get("m") == "device.status"]
    assert len(ids) == 2 and ids[1] > ids[0]
    leftovers = device.poll(0)
    assert len(leftovers) == 4
    assert any(item.get("method") == "other.method" for item in leftovers)


def test_request_id_does_not_equate_json_true_or_float_with_integer(
        monkeypatch):
    monkeypatch.setattr(pad, "_request_counter", iter([1]))
    device, backend = open_fake()

    def scripted(_request):
        return [
            {"result": {"version": "bool", "layer_index": 8},
             "id": True, "method": "device.status"},
            {"result": {"version": "float", "layer_index": 7},
             "id": 1.0, "method": "device.status"},
            {"result": {"version": "exact", "layer_index": 2},
             "id": 1, "method": "device.status"},
        ]

    backend.status_script = scripted
    reply = device.status(timeout=0.2)
    assert reply is not None and reply["result"]["version"] == "exact"
    assert device.layer_index == 2
    assert device.firmware_version == "exact"
    assert [message["id"] for message in device.poll(0)] == [True, 1.0]


def test_prequeued_vendor_status_id_one_cannot_prove_a_new_request():
    backend = FakeBackend()
    backend.auto_status = True
    device, backend = open_fake(backend)
    backend.queue_message({
        "result": {"version": "stale", "layer_index": 9},
        "id": 1, "method": "device.status",
    })
    reply = device.status(timeout=0.2)
    assert reply is not None and reply["result"]["version"] == "test"
    sent_id = backend.sent_messages[-1]["id"]
    assert type(sent_id) is int and 1 < sent_id < 2 ** 31
    assert device.layer_index == 1
    assert device.poll(0) == [{
        "result": {"version": "stale", "layer_index": 9},
        "id": 1, "method": "device.status",
    }]


def test_status_preserves_verified_layer_and_safe_firmware_string():
    backend = FakeBackend()
    backend.status_script = lambda request: [{
        "result": {"version": "v0.4.1", "layer_index": 2},
        "id": request["id"], "method": "device.status",
    }]
    device, _ = open_fake(backend)
    assert device.status(timeout=0.2) is not None
    assert device.status_verified is True
    assert device.layer_index == 2
    assert device.firmware_version == "v0.4.1"


def test_discard_hid_inputs_drops_prearm_keys_but_preserves_vendor_ack():
    backend = FakeBackend()
    backend.auto_status = True
    device, backend = open_fake(backend)
    key = {"m": "v.oai.hid", "p": {"k": "AG00", "act": 1}}
    ack = {"result": {"ok": 1}, "id": 7, "method": "v.oai.rgbcfg"}
    backend.queue_message(key)
    backend.queue_message(ack)
    assert device.status(timeout=0.2) is not None
    assert device.discard_hid_inputs() == 1
    assert device.poll(0) == [ack]


def test_status_request_and_discard_preserve_unrelated_envelope_metadata():
    backend = FakeBackend()
    key = {"m": "v.oai.hid", "p": {"k": "AG00", "act": 1}}
    ack = {"result": {"ok": 1}, "id": 7, "method": "v.oai.rgbcfg"}

    def scripted(request):
        return [
            key,
            ack,
            {
                "result": {"version": "test", "layer_index": 1},
                "id": request["id"], "method": "device.status",
            },
        ]

    backend.status_script = scripted
    device, backend = open_fake(backend)
    assert device.status(timeout=0.2) is not None
    callback_time = backend.clock.now

    # request/status consumed only their matching reply.  The pre-arm key and
    # unrelated ACK retain callback-time metadata until the explicit discard.
    assert device.discard_hid_inputs() == 1
    remaining = device.poll_received(0)
    assert [item.message for item in remaining] == [ack]
    assert remaining[0].received_at == callback_time
    assert remaining[0].connection_epoch == device.epoch


def test_non_string_firmware_is_not_preserved_but_layer_stays_verified():
    backend = FakeBackend()
    backend.status_script = lambda request: [{
        "result": {"version": ["not", "a", "string"], "layer_index": 1},
        "id": request["id"], "method": "device.status",
    }]
    device, _ = open_fake(backend)
    assert device.status(timeout=0.2) is not None
    assert device.status_verified is True and device.layer_index == 1
    assert device.firmware_version is None


@pytest.mark.parametrize(
    "result",
    [
        {"version": "missing"},
        {"version": "bool", "layer_index": True},
        {"version": "string", "layer_index": "1"},
        {"version": "zero", "layer_index": 0},
        {"version": "negative", "layer_index": -1},
    ],
    ids=["missing", "bool", "string", "zero", "negative"],
)
def test_status_rejects_malformed_or_nonpositive_layer(result):
    backend = FakeBackend()
    backend.status_script = lambda request: [{
        "result": result,
        "id": request["id"], "method": "device.status",
    }]
    device, _ = open_fake(backend)
    assert device.status(timeout=0.2) is None
    assert device.status_verified is False
    assert device.layer_index is None
    assert device.firmware_version is None


def test_status_timeout_fails_closed():
    backend = FakeBackend()
    backend.auto_status = True
    device, backend = open_fake(backend)
    assert device.status(timeout=0.1) is not None
    assert device.layer_index == 1 and device.firmware_version == "test"
    backend.auto_status = False
    assert device.status(timeout=0.1) is None
    assert device.status_verified is False
    assert device.layer_index is None
    assert device.firmware_version is None


def test_removal_callback_invalidates_connection_and_status():
    backend = FakeBackend()
    backend.auto_status = True
    device, backend = open_fake(backend)
    assert device.status(timeout=0.1) is not None
    assert device.status_verified is True
    backend.remove()
    assert device.connected is False
    assert device.status_verified is False
    assert device.layer_index is None
    assert device.firmware_version is None
    with pytest.raises(pad.PadDisconnected):
        device.send({"m": "x"})


def test_callback_error_is_reported_instead_of_escaping_ctypes_callback():
    backend = FakeBackend()
    backend.auto_status = True
    device, backend = open_fake(backend)
    assert device.status(timeout=0.1) is not None
    backend.on_report(0xE00002ED, pad.REPORT_ID, b"")
    with pytest.raises(pad.PadIOError, match="input callback"):
        device.poll(0)
    assert device.connected is False
    assert device.layer_index is None
    assert device.firmware_version is None


def test_set_report_error_discards_previous_verified_layer():
    backend = FakeBackend()
    backend.auto_status = True
    device, backend = open_fake(backend)
    assert device.status(timeout=0.1) is not None
    backend.fail_write_numbers.add(backend._write_number + 1)
    with pytest.raises(pad.PadIOError):
        device.send({"m": "x"})
    assert device.status_verified is False
    assert device.layer_index is None
    assert device.firmware_version is None


def test_runloop_pump_error_invalidates_connection_and_verified_layer():
    backend = FakeBackend()
    backend.auto_status = True
    device, backend = open_fake(backend)
    assert device.status(timeout=0.1) is not None
    backend.fail_pump = True
    with pytest.raises(pad.PadError, match="CFRunLoopRunInMode"):
        device.poll(0.1)
    assert device.connected is False
    assert device.status_verified is False
    assert device.layer_index is None
    assert device.firmware_version is None


def test_callback_clock_error_surfaces_from_next_poll_received_not_callback():
    device, backend = open_fake()
    report = protocol.frame(
        {"m": "v.oai.hid", "p": {"k": "AG00", "act": 1}},
        protocol.USB,
    )[0]

    def broken_clock():
        raise RuntimeError("clock unavailable")

    device._clock = broken_clock
    # ctypes callbacks cannot propagate Python exceptions.  _on_report records
    # the failure for the owner thread instead.
    backend.on_report(0, pad.REPORT_ID, report)
    with pytest.raises(pad.PadError, match="input callback failed: clock unavailable"):
        device.poll_received(0)
    assert device.connected is False


def test_reconnect_gets_new_epoch_and_is_usable_only_after_status_round_trip():
    backend = FakeBackend()
    backend.auto_status = True
    device, backend = open_fake(backend)
    old_epoch = device.epoch
    backend.remove()
    assert device.reconnect(timeout=0.2) is True
    assert device.epoch > old_epoch
    assert device.connected is True and device.status_verified is True
    assert device.layer_index == 1 and device.firmware_version == "test"
    assert backend.events.count("register") == 2


def test_reconnect_metadata_uses_new_epoch_and_rejects_stale_callback():
    backend = FakeBackend()
    backend.auto_status = True
    device, backend = open_fake(backend)
    message = {"m": "v.oai.hid", "p": {"k": "AG00", "act": 1}}
    report = protocol.frame(message, protocol.USB)[0]

    old_callback = backend.on_report
    backend.clock.now = 300.0
    old_callback(0, pad.REPORT_ID, report)
    first = device.poll_received(0)
    old_epoch = first[0].connection_epoch

    backend.remove()
    assert device.reconnect(timeout=0.2) is True
    assert device.epoch > old_epoch

    backend.clock.now = 301.0
    old_callback(0, pad.REPORT_ID, report)
    backend.clock.now = 302.0
    backend.on_report(0, pad.REPORT_ID, report)
    second = device.poll_received(0)

    assert [(item.received_at, item.connection_epoch) for item in second] == [
        (302.0, device.epoch),
    ]


def test_reconnect_ignores_delayed_removal_from_old_registration():
    backend = FakeBackend()
    backend.auto_status = True
    device, backend = open_fake(backend)
    old_removal_callback = backend.on_remove

    backend.remove()
    assert device.reconnect(timeout=0.2) is True
    new_epoch = device.epoch
    assert device.connected is True
    assert device.status_verified is True
    assert device.layer_index == 1

    old_removal_callback(0)

    assert device.epoch == new_epoch
    assert device.connected is True
    assert device.status_verified is True
    assert device.layer_index == 1


def test_reconnect_preserves_valid_layer_two_for_daemon_to_gate_later():
    backend = FakeBackend()
    device, backend = open_fake(backend)
    backend.remove()
    backend.status_script = lambda request: [{
        "result": {"version": "v0.4.1", "layer_index": 2},
        "id": request["id"], "method": "device.status",
    }]
    assert device.reconnect(timeout=0.2) is True
    assert device.connected is True and device.status_verified is True
    assert device.layer_index == 2


@pytest.mark.parametrize(
    "result",
    [
        {"version": "missing"},
        {"version": "bool", "layer_index": False},
        {"version": "string", "layer_index": "2"},
        {"version": "zero", "layer_index": 0},
        {"version": "negative", "layer_index": -2},
    ],
    ids=["missing", "bool", "string", "zero", "negative"],
)
def test_reconnect_disposes_connection_when_layer_is_not_valid(result):
    backend = FakeBackend()
    device, backend = open_fake(backend)
    backend.remove()
    backend.status_script = lambda request: [{
        "result": result,
        "id": request["id"], "method": "device.status",
    }]
    assert device.reconnect(timeout=0.2) is False
    assert device.connected is False and device.status_verified is False
    assert device.layer_index is None
    assert backend.events[-4:] == [
        "unregister", "unschedule", "close", "release",
    ]


def test_failed_reconnect_is_disposed_and_not_marked_verified():
    device, backend = open_fake()
    backend.remove()
    assert device.reconnect(timeout=0.1) is False
    assert device.connected is False and device.status_verified is False
    assert device.layer_index is None
    assert backend.events[-4:] == [
        "unregister", "unschedule", "close", "release",
    ]


def test_open_schedule_poll_and_close_must_stay_on_owner_thread():
    device, _ = open_fake()
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            device.poll(0)
        except BaseException as error:
            errors.append(error)

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join()
    assert len(errors) == 1 and isinstance(errors[0], pad.PadThreadError)


def test_close_turns_both_zones_off_pumps_then_cleans_up_in_exact_order():
    device, backend = open_fake()
    device.close(flush_seconds=0.2)

    messages = decoded_writes(backend)
    assert protocol.thstatus([None] * 6) in messages
    assert protocol.rgbcfg(ambient=None) in messages
    pump_index = max(i for i, event in enumerate(backend.events)
                     if isinstance(event, tuple) and event[0] == "pump")
    assert backend.events[pump_index + 1:] == [
        "unregister", "unschedule", "close", "release",
    ]
    event_count = len(backend.events)
    device.close()
    assert len(backend.events) == event_count


@pytest.mark.parametrize(
    ("turn_off_keys", "turn_off_ambient", "has_keys", "has_ambient"),
    [
        (False, True, False, True),
        (True, False, True, False),
        (False, False, False, False),
    ],
)
def test_close_only_clears_zones_the_caller_owns(
        turn_off_keys, turn_off_ambient, has_keys, has_ambient):
    device, backend = open_fake()
    device.close(
        flush_seconds=0,
        turn_off_keys=turn_off_keys,
        turn_off_ambient=turn_off_ambient,
    )
    messages = decoded_writes(backend)
    assert (protocol.thstatus([None] * 6) in messages) is has_keys
    assert (protocol.rgbcfg(ambient=None) in messages) is has_ambient
    assert backend.events[-4:] == [
        "unregister", "unschedule", "close", "release",
    ]


def test_close_cleanup_runs_even_when_write_and_pump_fail():
    device, backend = open_fake()
    backend.fail_write_numbers.add(1)
    backend.fail_pump = True
    with pytest.raises(pad.PadError):
        device.close(flush_seconds=0.1)
    assert backend.events[-4:] == [
        "unregister", "unschedule", "close", "release",
    ]
    # The ambient-off write and pump are attempted after the key-off failure.
    assert len(backend.writes) >= 2
    assert any(isinstance(event, tuple) and event[0] == "pump"
               for event in backend.events)


@pytest.mark.parametrize(
    "failure", ["unregister", "unschedule", "close", "close-code", "release"])
def test_each_cleanup_failure_still_runs_all_later_cleanup_steps(failure):
    device, backend = open_fake()
    if failure == "close-code":
        backend.close_result = 0xE00002ED
    else:
        backend.fail_cleanup.add(failure)
    with pytest.raises(pad.PadError):
        device.close(flush_seconds=0)
    assert backend.events[-4:] == [
        "unregister", "unschedule", "close", "release",
    ]
    assert device.connected is False
    assert device.layer_index is None


def test_context_manager_closes_device():
    backend = FakeBackend()
    with pad.Pad.open(backend=backend, clock=backend.clock) as device:
        assert device.connected
    assert backend.events[-4:] == [
        "unregister", "unschedule", "close", "release",
    ]
    assert device.layer_index is None


@pytest.mark.integration
def test_round_trip_on_real_hardware():
    device = pad.Pad.open()
    if device is None:
        pytest.skip("Codex Micro not connected")
    try:
        reply = device.status()
        assert reply is not None, "device.status did not round-trip"
        assert reply["result"]["layer_index"] == 1
        assert device.transport in (protocol.USB, protocol.BLE)
        assert device.status_verified is True
    finally:
        device.close()
