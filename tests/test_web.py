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
