from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

from semapad import pad as pad_module
from semapad.config import Config
from semapad.daemon import Daemon, owner_for
from semapad.model import AgentState

CLAUDE = "com.anthropic.claudefordesktop"
CODEX = "com.openai.codex"


# --- owner_for ---------------------------------------------------------------

def test_owner_for_exact_transitions():
    cfg = Config()
    assert owner_for(CLAUDE, "none", cfg) == "claude"
    assert owner_for(CODEX, "claude", cfg) == "codex"
    assert owner_for("com.apple.finder", "codex", cfg) == "codex"  # 3rd app keeps
    assert owner_for(None, "claude", cfg) == "claude"
    assert owner_for("x", "bogus", cfg) == "none"


def test_owner_for_off_and_always_modes():
    assert owner_for(CODEX, "codex", Config(gate_mode="off")) == "none"
    assert owner_for(CODEX, "codex", Config(gate_mode="always")) == "claude"


# --- FakePad -----------------------------------------------------------------

class FakePad:
    def __init__(self) -> None:
        self.connected = True
        self.status_verified = False
        self.epoch = 1
        self.layer_index: int | None = None
        self.transport = "USB"
        self.firmware_version = "0.4.1"
        self.layer_for_status = 1
        self.status_fails = False
        self.send_fails = False
        self.queue: list[pad_module.ReceivedMessage] = []
        self.sent: list[dict] = []
        self.closes: list[tuple[bool, bool]] = []
        self.reconnects = 0

    def status(self, timeout: float = 3.0):
        self.status_verified = False
        self.layer_index = None
        if self.status_fails or not self.connected:
            return None
        self.layer_index = self.layer_for_status
        self.status_verified = True
        return {"method": "device.status", "id": 1,
                "result": {"layer_index": self.layer_for_status}}

    def reconnect(self, timeout: float = 3.0) -> bool:
        self.reconnects += 1
        if self.status_fails:
            return False
        self.connected = True
        self.epoch += 1
        return self.status() is not None

    def poll_received(self, seconds: float):
        messages, self.queue = self.queue, []
        return messages

    def discard_hid_inputs(self) -> int:
        return 0

    def send(self, message: dict) -> None:
        if self.send_fails:
            raise pad_module.PadIOError("IOHIDDeviceSetReport", 1)
        self.sent.append(message)

    def close(self, flush_seconds: float = 1.0, *, turn_off_keys: bool = True,
              turn_off_ambient: bool = True) -> None:
        self.closes.append((turn_off_keys, turn_off_ambient))
        self.connected = False

    def push(self, message: dict, epoch: int | None = None) -> None:
        self.queue.append(pad_module.ReceivedMessage(
            message=message, received_at=0.0,
            connection_epoch=self.epoch if epoch is None else epoch))

    def sent_methods(self) -> list[str]:
        return [message["m"] for message in self.sent]


class SeizablePad(FakePad):
    def __init__(self) -> None:
        super().__init__()
        self._seize = False
        self.exclusive_denied = False

    def set_exclusive(self, flag: bool) -> None:
        self._seize = bool(flag)

    @property
    def exclusive_requested(self) -> bool:
        return self._seize


def _conversation(mapping: Path, cli: str, title: str = "conv") -> str:
    local_id = f"local_{uuid.uuid4()}"
    directory = mapping / "org" / "acct"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{local_id}.json").write_text(json.dumps({
        "sessionId": local_id, "cliSessionId": cli, "isArchived": False,
        "cwd": "/w", "createdAt": 1_000_000, "lastActivityAt": 2_000_000,
        "title": title,
    }))
    return local_id


def _process(sessions: Path, cli: str) -> None:
    sessions.mkdir(parents=True, exist_ok=True)
    (sessions / f"{os.getpid()}.json").write_text(json.dumps({
        "kind": "interactive", "sessionId": cli, "cwd": "/w",
        "pid": os.getpid(), "startedAt": 1_000_000,
    }))


def make_daemon(tmp_path: Path, pad: FakePad | None, *,
                cfg: Config | None = None,
                frontmost: str | None = CLAUDE,
                opener=None) -> Daemon:
    opens: list = []

    def default_opener(local_id: str) -> bool:
        opens.append(local_id)
        return True

    daemon = Daemon(
        cfg or Config(),
        state_dir=tmp_path / "state",
        mapping_dir=tmp_path / "mapping",
        sessions_dir=tmp_path / "sessions",
        config_path=tmp_path / "config.json",
        snapshot_path=tmp_path / "runtime" / "snapshot.json",
        pad=pad,
        pad_factory=lambda: pad,
        opener=opener or default_opener,
        frontmost=lambda: frontmost,
    )
    daemon.opens = opens   # test convenience
    return daemon


def hid(key: str = "AG00") -> dict:
    return {"m": "v.oai.hid", "p": {"k": key, "act": 1}}


# --- lifecycle & painting ----------------------------------------------------

def test_startup_verifies_status_then_paints_conversation_colour(tmp_path):
    cli = str(uuid.uuid4())
    _conversation(tmp_path / "mapping", cli)
    pad = FakePad()
    daemon = make_daemon(tmp_path, pad)
    daemon.tick(1.0)
    assert daemon.verified_layer == 1
    thstatus = [m for m in pad.sent if m["m"] == "v.oai.thstatus"]
    # dead conversation -> idle white on key 1
    assert thstatus[0]["p"][0]["c"] == 0xFFFFFF
    assert thstatus[0]["p"][1]["e"] == 0    # empty key off
    ambient = [m for m in pad.sent if m["m"] == "v.oai.rgbcfg"]
    assert ambient[0]["p"]["ambient"]["c"] == 0xFF6D00   # claude owns


def test_key_press_opens_immediately_and_repaints(tmp_path):
    # The tap window is gone (2026-08-10 acceptance): claude:// raises Desktop
    # itself, so every valid press opens at once -- no 350 ms lag.
    cli = str(uuid.uuid4())
    lid = _conversation(tmp_path / "mapping", cli)
    pad = FakePad()
    daemon = make_daemon(tmp_path, pad)
    daemon.tick(1.0)
    pad.sent.clear()
    pad.push(hid("AG00"))
    daemon.tick(2.0)
    assert daemon.opens == [lid]
    assert daemon.last_input_result == "opened"
    assert "v.oai.thstatus" in pad.sent_methods()   # press repainted the keys


def test_codex_frontmost_yields_keys_and_ignores_input(tmp_path):
    cli = str(uuid.uuid4())
    _conversation(tmp_path / "mapping", cli)
    pad = FakePad()
    daemon = make_daemon(tmp_path, pad, frontmost=CODEX)
    daemon.tick(1.0)
    assert "v.oai.thstatus" not in pad.sent_methods()   # A-zone yielded
    ambient = [m for m in pad.sent if m["m"] == "v.oai.rgbcfg"]
    assert ambient[0]["p"]["ambient"]["c"] == 0x304FFE  # codex border
    pad.push(hid("AG00"))
    daemon.tick(2.0)
    assert daemon.opens == []
    assert daemon.last_input_result == "ignored_owner"


def test_layer_two_stops_input_and_display_until_layer_one(tmp_path):
    cli = str(uuid.uuid4())
    _conversation(tmp_path / "mapping", cli)
    pad = FakePad()
    pad.layer_for_status = 2
    daemon = make_daemon(tmp_path, pad, cfg=Config(status_poll_ms=1))
    daemon.tick(1.0)
    assert pad.sent_methods() == []      # keep: zero writes on layer 2
    pad.push(hid("AG00"))
    daemon.tick(2.0)
    assert daemon.last_input_result == "ignored_layer"
    pad.layer_for_status = 1
    daemon.tick(3.0)                     # status re-poll picks up layer 1
    assert "v.oai.thstatus" in pad.sent_methods()   # full repaint on return


def test_empty_key_press_flashes_fault_then_restores(tmp_path):
    cli = str(uuid.uuid4())
    _conversation(tmp_path / "mapping", cli)
    pad = FakePad()
    daemon = make_daemon(tmp_path, pad)
    daemon.tick(1.0)
    pad.sent.clear()
    pad.push(hid("AG05"))                # empty slot
    daemon.tick(2.0)
    assert daemon.last_input_result == "empty_slot"
    fault = [m for m in pad.sent if m["m"] == "v.oai.rgbcfg"]
    assert fault[0]["p"]["ambient"]["e"] == 3       # rainbow fault flash
    pad.sent.clear()
    daemon.tick(2.5)                     # feedback expired
    restored = [m for m in pad.sent if m["m"] == "v.oai.rgbcfg"]
    assert restored[0]["p"]["ambient"]["e"] == 1    # back to solid owner


def test_opened_press_echoes_key_colour_on_border_then_restores(tmp_path):
    cli = str(uuid.uuid4())
    _conversation(tmp_path / "mapping", cli)
    pad = FakePad()
    daemon = make_daemon(tmp_path, pad)
    daemon.tick(1.0)
    pad.sent.clear()
    pad.push(hid("AG00"))                # occupied slot, idle → white key
    daemon.tick(2.0)
    assert daemon.last_input_result == "opened"
    echo = [m for m in pad.sent if m["m"] == "v.oai.rgbcfg"]
    assert echo[0]["p"]["ambient"]["c"] == 0xFFFFFF   # pressed key's colour
    assert echo[0]["p"]["ambient"]["e"] == 1          # solid, not fault
    pad.sent.clear()
    daemon.tick(2.5)                     # echo expired
    restored = [m for m in pad.sent if m["m"] == "v.oai.rgbcfg"]
    assert restored[0]["p"]["ambient"]["c"] == 0xFF6D00   # owner border


def test_stale_epoch_messages_are_never_dispatched(tmp_path):
    cli = str(uuid.uuid4())
    _conversation(tmp_path / "mapping", cli)
    pad = FakePad()
    daemon = make_daemon(tmp_path, pad)
    daemon.tick(1.0)
    pad.push(hid("AG00"), epoch=99)      # from another connection
    daemon.tick(2.0)
    daemon.tick(3.0)
    assert daemon.opens == []
    assert daemon.last_input_result is None


def test_send_failure_backs_off_then_reconnects_with_full_repaint(tmp_path):
    cli = str(uuid.uuid4())
    _conversation(tmp_path / "mapping", cli)
    pad = FakePad()
    daemon = make_daemon(tmp_path, pad)
    daemon.tick(1.0)
    pad.send_fails = True
    pad.sent.clear()
    daemon.tick(6.5)                     # refresh due -> send fails
    assert daemon.pad_error_code == "send_failed"
    assert daemon.verified_layer is None
    pad.send_fails = False
    daemon.tick(8.0)                     # past retry backoff -> reconnect
    assert pad.reconnects == 1
    assert daemon.verified_layer == 1
    assert "v.oai.thstatus" in pad.sent_methods()


def test_close_flushes_only_claude_owned_zones(tmp_path):
    pad = FakePad()
    daemon = make_daemon(tmp_path, pad, frontmost=CODEX)
    daemon.tick(1.0)
    daemon.close()
    assert pad.closes == [(False, True)]     # codex keys preserved


def test_close_after_unverified_status_writes_nothing(tmp_path):
    pad = FakePad()
    daemon = make_daemon(tmp_path, pad, cfg=Config(status_poll_ms=1))
    daemon.tick(1.0)
    pad.status_fails = True
    daemon.tick(2.0)                     # status now unverified
    daemon.close()
    assert pad.closes == [(False, False)]


def test_working_hook_colours_the_live_conversation(tmp_path):
    from semapad.sources.hooks import SessionRecord, write

    cli = str(uuid.uuid4())
    _conversation(tmp_path / "mapping", cli)
    _process(tmp_path / "sessions", cli)
    write(SessionRecord(session_id=cli, cwd="", state=AgentState.WORKING,
                        rev=1, updated_at=1.0), tmp_path / "state")
    pad = FakePad()
    daemon = make_daemon(tmp_path, pad)
    daemon.tick(2.0)
    thstatus = [m for m in pad.sent if m["m"] == "v.oai.thstatus"]
    assert thstatus[0]["p"][0]["c"] == 0x304FFE      # working blue


def test_snapshot_written_with_device_and_finished_colours(tmp_path):
    cli = str(uuid.uuid4())
    _conversation(tmp_path / "mapping", cli)
    pad = FakePad()
    daemon = make_daemon(tmp_path, pad)
    daemon.tick(1.0)
    snap = json.loads((tmp_path / "runtime" / "snapshot.json").read_text())
    assert snap["schema"] == 1
    assert snap["device"]["connected"] is True
    assert snap["device"]["layer"] == 1
    assert snap["slots"][0]["color"] == 0xFFFFFF
    assert snap["frontmost"]["owner"] == "claude"


def test_config_change_applies_next_tick(tmp_path):
    cli = str(uuid.uuid4())
    _conversation(tmp_path / "mapping", cli)
    pad = FakePad()
    daemon = make_daemon(tmp_path, pad)
    daemon.tick(1.0)
    pad.sent.clear()
    (tmp_path / "config.json").write_text(json.dumps(
        {"colors": {"idle": "#123456"}}))
    daemon.tick(2.0)                     # spec §11.3: apply on save
    thstatus = [m for m in pad.sent if m["m"] == "v.oai.thstatus"]
    assert thstatus[0]["p"][0]["c"] == 0x123456


def test_pad_factory_none_retries_with_backoff(tmp_path):
    calls = {"n": 0}

    def factory():
        calls["n"] += 1
        return None

    daemon = Daemon(Config(), state_dir=tmp_path / "s",
                    mapping_dir=tmp_path / "m", sessions_dir=tmp_path / "p",
                    config_path=tmp_path / "c.json",
                    snapshot_path=tmp_path / "r" / "snap.json",
                    pad_factory=factory, frontmost=lambda: CLAUDE)
    daemon.tick(0.0)
    assert daemon.pad_error_code == "unavailable"
    daemon.tick(0.5)                     # inside backoff: no new attempt
    assert calls["n"] == 1
    daemon.tick(1.5)                     # past backoff
    assert calls["n"] == 2


def test_press_records_last_input_and_event_log(tmp_path):
    cli = str(uuid.uuid4())
    lid = _conversation(tmp_path / "mapping", cli)
    pad = FakePad()
    daemon = make_daemon(tmp_path, pad)
    daemon.tick(1.0)
    pad.push(hid("AG00"))
    daemon.tick(2.0)

    snap = json.loads((tmp_path / "runtime" / "snapshot.json").read_text())
    li = snap["device"]["last_input"]
    assert li["key"] == 0 and li["result"] == "opened" and li["at"] == 2.0

    lines = [json.loads(l) for l in
             (tmp_path / "logs" / "events.jsonl").read_text().splitlines()]
    inputs = [l for l in lines if l["event"] == "input"]
    assert inputs[-1]["key"] == 0
    assert inputs[-1]["result"] == "opened"
    assert inputs[-1]["local_id"] == lid
    assert "title" not in json.dumps(lines)   # ids only, never titles


def test_snapshot_carries_the_package_version(tmp_path):
    pad = FakePad()
    daemon = make_daemon(tmp_path, pad)
    daemon.tick(1.0)
    snap = json.loads((tmp_path / "runtime" / "snapshot.json").read_text())
    assert isinstance(snap["version"], str) and snap["version"]


def test_async_scan_serves_cached_results_and_refreshes_off_thread(tmp_path):
    """Codex rec #3: the 58 ms mapping scan must not run on the HID thread
    every tick. With async_scan, ticks swap in the latest completed result."""
    import time as time_mod

    cli = str(uuid.uuid4())
    _conversation(tmp_path / "mapping", cli, "first")
    daemon = make_daemon(tmp_path, FakePad())
    daemon.async_scan = True
    daemon.tick(1.0)                     # first tick scans synchronously once
    assert len(daemon._scan_result[0]) == 1

    _conversation(tmp_path / "mapping", str(uuid.uuid4()), "second")
    daemon._SCAN_INTERVAL_SECONDS = 0.0  # make the refresh due immediately
    daemon.tick(2.0)                     # serves cache, kicks the worker
    deadline = time_mod.time() + 5.0
    while time_mod.time() < deadline:
        with daemon._scan_lock:
            if daemon._scan_result and len(daemon._scan_result[0]) == 2:
                break
        time_mod.sleep(0.02)
    assert len(daemon._scan_result[0]) == 2   # worker delivered the refresh
    daemon.tick(3.0)
    assert sum(1 for c in daemon._scan_result[0]) == 2


def test_exclusive_mode_seizes_for_claude_and_releases_for_codex(tmp_path):
    cli = str(uuid.uuid4())
    _conversation(tmp_path / "mapping", cli)
    pad = SeizablePad()
    front = {"id": CLAUDE}
    daemon = make_daemon(tmp_path, pad, cfg=Config(exclusive=True))
    daemon._frontmost = lambda: front["id"]
    daemon.tick(1.0)
    daemon.tick(2.0)                     # flip applies on the tick after owner
    assert pad.exclusive_requested       # Claude owns -> seized
    reconnects = pad.reconnects
    front["id"] = CODEX
    daemon.tick(3.0)
    daemon.tick(4.0)
    assert not pad.exclusive_requested   # Codex owns -> shared again
    assert pad.reconnects > reconnects   # each flip reopens the device


def test_exclusive_off_never_touches_the_pad_mode(tmp_path):
    cli = str(uuid.uuid4())
    _conversation(tmp_path / "mapping", cli)
    pad = SeizablePad()
    daemon = make_daemon(tmp_path, pad)      # default config: exclusive off
    daemon.tick(1.0)
    daemon.tick(2.0)
    assert not pad.exclusive_requested
    assert pad.reconnects == 0
