import json
from pathlib import Path

import pytest

from semapad.sources import processes as sessions


def _write(root: Path, file_pid: int, filename: str | None = None, **overrides) -> None:
    payload = {
        "pid": file_pid,
        "sessionId": f"sid-{file_pid}",
        "cwd": "/workspace",
        "name": f"session-{file_pid}",
        "kind": "interactive",
        "entrypoint": "claude-desktop",
        "startedAt": 1_700_000_000_000 + file_pid,
    }
    payload.update(overrides)
    (root / (filename or f"{file_pid}.json")).write_text(json.dumps(payload))


def _alive(_pid: int, _signal: int) -> None:
    return None


def test_default_directory_is_the_live_claude_session_directory():
    assert sessions.SESSIONS_DIR == Path.home() / ".claude" / "sessions"


def test_scan_returns_interactive_sessions_and_converts_milliseconds(tmp_path: Path):
    _write(tmp_path, 1, startedAt=1_700_000_123_000)
    snapshot = sessions.scan(tmp_path, alive=_alive)

    assert snapshot.authoritative is True
    assert snapshot.diagnostics == ()
    assert len(snapshot.sessions) == 1
    assert snapshot.sessions[0].started_at == 1_700_000_123.0
    assert snapshot.sessions[0].entrypoint == "claude-desktop"


def test_cli_sessions_are_kept_and_unknown_fields_are_ignored(tmp_path: Path):
    _write(tmp_path, 1, entrypoint="cli", futureSchemaField={"anything": True})
    snapshot = sessions.scan(tmp_path, alive=_alive)
    assert [session.entrypoint for session in snapshot.sessions] == ["cli"]
    assert snapshot.authoritative is True


def test_missing_directory_is_not_authoritative(tmp_path: Path):
    snapshot = sessions.scan(tmp_path / "missing", alive=_alive)
    assert snapshot.sessions == ()
    assert snapshot.authoritative is False
    assert any("missing" in item for item in snapshot.diagnostics)


def test_normal_empty_directory_is_authoritative(tmp_path: Path):
    snapshot = sessions.scan(tmp_path, alive=_alive)
    assert snapshot == sessions.SessionSnapshot((), True, ())


def test_directory_iteration_failure_is_not_authoritative(tmp_path: Path, monkeypatch):
    def unreadable(_root: Path):
        raise PermissionError("denied")

    monkeypatch.setattr(Path, "iterdir", unreadable)
    snapshot = sessions.scan(tmp_path, alive=_alive)
    assert snapshot.sessions == ()
    assert snapshot.authoritative is False
    assert any("unreadable" in item for item in snapshot.diagnostics)


def test_background_jobs_are_a_valid_empty_result(tmp_path: Path):
    _write(tmp_path, 1, kind="bg")
    snapshot = sessions.scan(tmp_path, alive=_alive)
    assert snapshot.sessions == ()
    assert snapshot.authoritative is True


def test_process_lookup_error_means_dead_but_scan_is_authoritative(tmp_path: Path):
    _write(tmp_path, 1)

    def dead(_pid: int, _signal: int) -> None:
        raise ProcessLookupError

    snapshot = sessions.scan(tmp_path, alive=dead)
    assert snapshot.sessions == ()
    assert snapshot.authoritative is True


def test_permission_error_means_live_and_is_diagnostic(tmp_path: Path):
    _write(tmp_path, 1)

    def denied(_pid: int, _signal: int) -> None:
        raise PermissionError

    snapshot = sessions.scan(tmp_path, alive=denied)
    assert [session.session_id for session in snapshot.sessions] == ["sid-1"]
    assert snapshot.authoritative is True
    assert any("permission" in item for item in snapshot.diagnostics)


def test_other_pid_check_error_keeps_session_but_loses_authority(tmp_path: Path):
    _write(tmp_path, 1)

    def uncertain(_pid: int, _signal: int) -> None:
        raise OSError("temporary failure")

    snapshot = sessions.scan(tmp_path, alive=uncertain)
    assert [session.session_id for session in snapshot.sessions] == ["sid-1"]
    assert snapshot.authoritative is False
    assert any("PID" in item for item in snapshot.diagnostics)


def test_one_broken_file_keeps_valid_results_but_loses_authority(tmp_path: Path):
    _write(tmp_path, 1)
    (tmp_path / "broken.json").write_text("{not json")

    snapshot = sessions.scan(tmp_path, alive=_alive)
    assert [session.session_id for session in snapshot.sessions] == ["sid-1"]
    assert snapshot.authoritative is False
    assert any("invalid" in item for item in snapshot.diagnostics)


def test_partial_object_is_not_mistaken_for_an_authoritative_empty_scan(tmp_path: Path):
    (tmp_path / "partial.json").write_text(json.dumps({"pid": 3}))
    snapshot = sessions.scan(tmp_path, alive=_alive)
    assert snapshot.sessions == ()
    assert snapshot.authoritative is False


@pytest.mark.parametrize(
    ("field", "value"),
    [("pid", 10 ** 100), ("startedAt", 10 ** 309)],
)
def test_unrepresentable_numbers_are_invalid_not_fatal(tmp_path: Path, field, value):
    _write(tmp_path, 1, **{field: value})
    snapshot = sessions.scan(tmp_path, alive=_alive)
    assert snapshot.sessions == ()
    assert snapshot.authoritative is False
    assert any("invalid" in item for item in snapshot.diagnostics)


def test_valid_but_unknown_kind_is_non_authoritative(tmp_path: Path):
    _write(tmp_path, 1, kind="future-kind")
    snapshot = sessions.scan(tmp_path, alive=_alive)
    assert snapshot.sessions == ()
    assert snapshot.authoritative is False


def test_duplicate_session_ids_choose_newest_then_pid_deterministically(tmp_path: Path):
    _write(tmp_path, 1, sessionId="same", startedAt=1_700_000_001_000)
    _write(tmp_path, 2, sessionId="same", startedAt=1_700_000_002_000)
    snapshot = sessions.scan(tmp_path, alive=_alive)
    assert [(session.session_id, session.pid) for session in snapshot.sessions] == [("same", 2)]
    assert snapshot.authoritative is True
    assert any("duplicate" in item for item in snapshot.diagnostics)

    _write(tmp_path, 1, sessionId="same", startedAt=1_700_000_002_000)
    tied = sessions.scan(tmp_path, alive=_alive)
    assert [(session.session_id, session.pid) for session in tied.sessions] == [("same", 2)]


def test_scan_order_breaks_equal_start_times_by_session_id(tmp_path: Path):
    _write(tmp_path, 1, sessionId="z", startedAt=1_700_000_000_000)
    _write(tmp_path, 2, sessionId="a", startedAt=1_700_000_000_000)
    snapshot = sessions.scan(tmp_path, alive=_alive)
    assert [session.session_id for session in snapshot.sessions] == ["a", "z"]
