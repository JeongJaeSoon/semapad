from __future__ import annotations

import json
import threading
import urllib.request
import uuid
from pathlib import Path

from semapad.model import AgentState
from semapad.sources.hooks import SessionRecord, write
from semapad.web.server import Dashboard, make_server


def _conversation(mapping: Path, cli: str, title: str = "conv") -> str:
    local_id = f"local_{uuid.uuid4()}"
    directory = mapping / "org" / "acct"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{local_id}.json").write_text(json.dumps({
        "sessionId": local_id, "cliSessionId": cli, "isArchived": False,
        "cwd": "/w", "createdAt": 1_700_000_000_000,
        "lastActivityAt": 1_700_000_050_000, "title": title,
    }))
    return local_id


def _process(sessions: Path, cli: str, pid: int | None = None) -> None:
    pid = pid or __import__("os").getpid()   # a pid that is provably alive
    sessions.mkdir(parents=True, exist_ok=True)
    (sessions / f"{pid}.json").write_text(json.dumps({
        "kind": "interactive", "sessionId": cli, "cwd": "/w", "pid": pid,
        "startedAt": 1_700_000_000_000,
    }))


def _dashboard(tmp_path: Path, **kwargs) -> Dashboard:
    return Dashboard(
        state_dir=tmp_path / "state",
        mapping_dir=tmp_path / "mapping",
        sessions_dir=tmp_path / "sessions",
        config_path=tmp_path / "config.json",
        frontmost_reader=kwargs.pop("frontmost_reader",
                                    lambda: "com.anthropic.claudefordesktop"),
        **kwargs,
    )


def test_data_joins_conversation_process_and_hook(tmp_path: Path):
    cli = str(uuid.uuid4())
    lid = _conversation(tmp_path / "mapping", cli)
    _process(tmp_path / "sessions", cli)
    write(SessionRecord(session_id=cli, cwd="", state=AgentState.WAITING,
                        rev=1, updated_at=1_700_000_050.0),
          tmp_path / "state")
    dash = _dashboard(tmp_path, clock=lambda: 1_700_000_060.0)
    data = dash.data()
    slot = data["slots"][0]
    assert slot["local_id"] == lid
    assert slot["state"] == "waiting"
    assert slot["process_alive"] is True
    assert slot["color"] == 0xFF6D00
    assert data["device"]["connected"] is False
    assert data["frontmost"]["owner"] == "claude"
    assert data["schema"] == 1


def test_data_dead_conversation_is_idle_but_listed(tmp_path: Path):
    cli = str(uuid.uuid4())
    lid = _conversation(tmp_path / "mapping", cli)
    dash = _dashboard(tmp_path, clock=lambda: 1_700_000_060.0)
    data = dash.data()
    assert data["slots"][0]["local_id"] == lid
    assert data["slots"][0]["state"] == "idle"
    assert data["slots"][0]["reason"] == "no_process"
    assert data["conversations"][0]["process_alive"] is False


def test_slot_assignment_sticks_across_polls(tmp_path: Path):
    a = str(uuid.uuid4())
    b = str(uuid.uuid4())
    mapping = tmp_path / "mapping"
    _conversation(mapping, a, title="a")
    _conversation(mapping, b, title="b")
    dash = _dashboard(tmp_path, clock=lambda: 1_700_000_060.0)
    first = [s["local_id"] for s in dash.data()["slots"]]
    second = [s["local_id"] for s in dash.data()["slots"]]
    assert first == second


def test_frontmost_failure_is_reported_not_raised(tmp_path: Path):
    def boom() -> str:
        raise RuntimeError("no AppKit here")
    dash = _dashboard(tmp_path, frontmost_reader=boom,
                      clock=lambda: 1_700_000_060.0)
    data = dash.data()
    assert data["frontmost"]["error"] == "no AppKit here"
    assert data["frontmost"]["owner"] is None


def test_http_endpoints(tmp_path: Path):
    dash = _dashboard(tmp_path, clock=lambda: 1_700_000_060.0)
    server = make_server(dash, port=0)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/") as response:
            page = response.read().decode()
            assert response.status == 200
            assert "semapad" in page
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/data") as response:
            payload = json.loads(response.read())
            assert payload["schema"] == 1
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/etc/passwd")
            raise AssertionError("expected 404")
        except urllib.error.HTTPError as error:
            assert error.code == 404
    finally:
        server.shutdown()
        server.server_close()


def test_config_endpoints_with_csrf(tmp_path: Path):
    dash = _dashboard(tmp_path, clock=lambda: 1_700_000_060.0)
    server = make_server(dash, port=0)
    port = server.server_address[1]
    token = server.RequestHandlerClass.csrf_token
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    try:
        # page embeds the token
        with urllib.request.urlopen(f"{base}/") as response:
            assert token in response.read().decode()
        # schema endpoint
        with urllib.request.urlopen(f"{base}/config") as response:
            fields = json.loads(response.read())["fields"]
            assert any(f["path"] == "colors.idle" for f in fields)
        # POST without token -> 403, file untouched
        request = urllib.request.Request(
            f"{base}/config", method="POST",
            data=json.dumps({"colors.idle": "#101010"}).encode(),
            headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(request)
            raise AssertionError("expected 403")
        except urllib.error.HTTPError as error:
            assert error.code == 403
        assert not (tmp_path / "config.json").exists()
        # POST with token -> saved, next /data uses the new colour immediately
        request = urllib.request.Request(
            f"{base}/config", method="POST",
            data=json.dumps({"colors.idle": "#101010"}).encode(),
            headers={"Content-Type": "application/json",
                     "X-Semapad-Token": token})
        with urllib.request.urlopen(request) as response:
            assert json.loads(response.read()) == {"ok": True}
        _conversation(tmp_path / "mapping", str(uuid.uuid4()))
        with urllib.request.urlopen(f"{base}/data") as response:
            data = json.loads(response.read())
            assert data["palette"]["idle"] == 0x101010
            assert data["slots"][0]["color"] == 0x101010
        # invalid value -> 400 with inline error, file keeps old value
        request = urllib.request.Request(
            f"{base}/config", method="POST",
            data=json.dumps({"colors.idle": "nope"}).encode(),
            headers={"Content-Type": "application/json",
                     "X-Semapad-Token": token})
        try:
            urllib.request.urlopen(request)
            raise AssertionError("expected 400")
        except urllib.error.HTTPError as error:
            assert error.code == 400
            assert "colors.idle" in json.loads(error.read())["errors"]
        assert json.loads((tmp_path / "config.json").read_text())["colors"]["idle"] == "#101010"
    finally:
        server.shutdown()
        server.server_close()


def test_fresh_daemon_snapshot_wins_over_local_compute(tmp_path: Path):
    snapshot_path = tmp_path / "runtime" / "snapshot.json"
    snapshot_path.parent.mkdir(parents=True)
    daemon_snap = {
        "schema": 1, "generated_at": 1_700_000_058.0,
        "config_fingerprint": "feedbeefcafe0000",
        "alert": "normal", "palette": {"idle": 1}, "diagnostics": [],
        "slots": [], "conversations": [],
        "device": {"connected": True, "layer": 1},
        "frontmost": {"bundle_id": "x", "owner": "claude", "error": None},
        "processes": {"count": 0, "authoritative": True, "diagnostics": []},
        "config": {"path": "", "warnings": []},
    }
    snapshot_path.write_text(json.dumps(daemon_snap))
    dash = Dashboard(
        state_dir=tmp_path / "state", mapping_dir=tmp_path / "mapping",
        sessions_dir=tmp_path / "sessions", config_path=tmp_path / "config.json",
        daemon_snapshot_path=snapshot_path,
        frontmost_reader=lambda: None, clock=lambda: 1_700_000_060.0)
    data = dash.data()
    assert data["source"] == "daemon"
    assert data["device"]["connected"] is True
    # fingerprint mismatch (no config on disk -> "absent") -> pending banner
    assert data["config_pending"] is True

    # stale snapshot -> ui computes for itself
    daemon_snap["generated_at"] = 1_700_000_010.0
    snapshot_path.write_text(json.dumps(daemon_snap))
    data = dash.data()
    assert data["source"] == "ui"
    assert data["device"]["connected"] is False


def test_transient_unreadable_mapping_does_not_move_keys(tmp_path: Path):
    """End to end: one corrupted poll must not shuffle key assignment."""
    lid_a = _conversation(tmp_path / "mapping", str(uuid.uuid4()), "first")
    lid_b = _conversation(tmp_path / "mapping", str(uuid.uuid4()), "second")
    dash = _dashboard(tmp_path, clock=lambda: 1_700_000_060.0)
    first = dash.data()
    before = [s["local_id"] for s in first["slots"]]
    assert before[0] is not None and before[1] is not None

    target = tmp_path / "mapping" / "org" / "acct" / f"{before[0]}.json"
    original = target.read_text()
    target.write_text("{ half-written")          # Desktop rewrite race
    during = [s["local_id"] for s in dash.data()["slots"]]
    target.write_text(original)
    after = [s["local_id"] for s in dash.data()["slots"]]

    assert during == before
    assert after == before


def test_data_long_poll_returns_when_the_snapshot_advances(tmp_path: Path):
    """/data?since=N holds until the daemon writes a newer snapshot, so a key
    press reaches the page within one daemon tick, not one browser poll."""
    import threading
    import time as time_mod
    import urllib.request

    from semapad import view as view_module
    from semapad.web import server as server_module

    snap_path = tmp_path / "runtime" / "snapshot.json"
    snap_path.parent.mkdir(parents=True)
    base = {"schema": view_module.SNAPSHOT_SCHEMA, "generated_at": 100.0,
            "slots": [], "conversations": [], "palette": {}, "alert": "normal",
            "diagnostics": [], "config_fingerprint": "x",
            "device": {}, "frontmost": {}, "processes": {"count": 0,
            "authoritative": True, "diagnostics": []},
            "config": {"warnings": []}}
    snap_path.write_text(json.dumps(base))

    dash = _dashboard(tmp_path, daemon_snapshot_path=snap_path,
                      clock=lambda: 101.0)
    dash.DAEMON_SNAPSHOT_FRESH_SECONDS = 1e9   # keep the fixture snapshot fresh
    server = make_server(dash, port=0)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        def advance():
            time_mod.sleep(0.3)
            newer = dict(base, generated_at=100.5)
            snap_path.write_text(json.dumps(newer))
        threading.Thread(target=advance, daemon=True).start()

        started = time_mod.time()
        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/data?since=100.0", timeout=10) as r:
            payload = json.loads(r.read())
        waited = time_mod.time() - started
        assert payload["generated_at"] == 100.5
        assert 0.2 < waited < 5.0          # held, then released promptly
    finally:
        server.shutdown()
        server.server_close()
