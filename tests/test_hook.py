import io
import json
from dataclasses import fields
from pathlib import Path

import pytest

from semapad.sources import hooks as hook, hooks as store
from semapad.model import AgentState
from semapad.sources.hooks import SessionRecord


def event(name: object, **extra: object) -> dict[str, object]:
    return {
        "hook_event_name": name,
        "session_id": "session-1",
        "cwd": "/workspace",
        **extra,
    }


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("SessionStart", AgentState.IDLE),
        ("UserPromptSubmit", AgentState.WORKING),
        ("PreToolUse", AgentState.WORKING),
        ("PostToolUse", AgentState.WORKING),
        ("PreCompact", AgentState.WORKING),
        ("Stop", AgentState.DONE),
        ("StopFailure", AgentState.ERROR),
        ("PostToolUseFailure", AgentState.ERROR),
        # Spec §11.5: classifier denials are progress under auto permission
        # mode -- WORKING, not ERROR.
        ("PermissionDenied", AgentState.WORKING),
    ],
)
def test_classify_maps_state_changing_events(name: str, expected: AgentState):
    assert hook.classify(event(name)) is expected


def test_classify_ask_user_question_as_waiting():
    payload = event("PreToolUse", tool_name="AskUserQuestion")
    assert hook.classify(payload) is AgentState.WAITING


@pytest.mark.parametrize("tool_name", ["Bash", "Edit", "", None, ["AskUserQuestion"]])
def test_classify_other_pre_tool_use_as_working(tool_name: object):
    payload = event("PreToolUse")
    if tool_name is not None:
        payload["tool_name"] = tool_name
    assert hook.classify(payload) is AgentState.WORKING


@pytest.mark.parametrize("notification_type", ["permission_prompt", "agent_needs_input"])
def test_classify_whitelists_waiting_notifications(notification_type: str):
    assert hook.classify(
        event("Notification", notification_type=notification_type)
    ) is AgentState.WAITING


@pytest.mark.parametrize(
    "notification_type",
    ["idle_prompt", "agent_completed", "auth_success", "something_new", None, []],
)
def test_classify_ignores_other_notifications(notification_type: object):
    payload = event("Notification")
    if notification_type is not None:
        payload["notification_type"] = notification_type
    assert hook.classify(payload) is None


def _stored(tmp_path: Path, state: AgentState) -> Path:
    root = tmp_path / "state"
    record = SessionRecord(session_id="session-1", cwd="/workspace",
                           state=state, rev=1, updated_at=2.0)
    store.write(record, root)
    return root


def _run_idle_prompt(root: Path) -> None:
    payload = event("Notification", notification_type="idle_prompt")
    assert hook.run(io.StringIO(json.dumps(payload)), root) == 0


def test_idle_prompt_promotes_a_working_session_to_waiting(tmp_path: Path):
    root = _stored(tmp_path, AgentState.WORKING)
    _run_idle_prompt(root)
    assert store.read_all(root)[0].state is AgentState.WAITING


@pytest.mark.parametrize(
    "state", [AgentState.IDLE, AgentState.WAITING, AgentState.DONE, AgentState.ERROR]
)
def test_idle_prompt_leaves_non_working_states_alone(
    tmp_path: Path, state: AgentState
):
    root = _stored(tmp_path, state)
    _run_idle_prompt(root)
    assert store.read_all(root)[0].state is state


def test_idle_prompt_without_a_record_writes_nothing(tmp_path: Path):
    root = tmp_path / "state"
    _run_idle_prompt(root)
    assert store.read_all(root) == []


def test_idle_prompt_from_a_subagent_is_dropped(tmp_path: Path):
    root = _stored(tmp_path, AgentState.WORKING)
    payload = event("Notification", notification_type="idle_prompt",
                    agent_type="claude")
    assert hook.run(io.StringIO(json.dumps(payload)), root) == 0
    assert store.read_all(root)[0].state is AgentState.WORKING


@pytest.mark.parametrize("payload", [None, [], "event", 3, False])
def test_classify_non_dict_inputs_as_no_state(payload: object):
    assert hook.classify(payload) is None


def test_classify_malformed_or_unknown_event_names_as_no_state():
    assert hook.classify(event(["Stop"])) is None
    assert hook.classify(event("SomethingNew")) is None


@pytest.mark.parametrize("agent_type", ["claude", "", None, False, 0])
def test_every_public_classifier_drops_an_agent_type_key(agent_type: object):
    payload = event("PreToolUse", agent_type=agent_type)
    assert hook.classify(payload) is None
    assert hook.record_from(payload, rev=1, now=2.0) is None


@pytest.mark.parametrize(
    "name",
    ["SessionEnd", "PermissionRequest", "SubagentStart", "SubagentStop", "Unknown"],
)
def test_record_from_ignores_non_state_events(name: str):
    assert hook.record_from(event(name), rev=1, now=2.0) is None


def test_record_from_builds_the_exact_desktop_record():
    record = hook.record_from(event("Stop"), rev=123, now=45.5)

    assert record == SessionRecord(
        session_id="session-1",
        cwd="/workspace",
        state=AgentState.DONE,
        rev=123,
        updated_at=45.5,
    )
    assert [field.name for field in fields(record)] == [
        "session_id",
        "cwd",
        "state",
        "rev",
        "updated_at",
    ]


@pytest.mark.parametrize(
    "session_id",
    [None, "", " ", "\t\n", 123, [], {}],
)
def test_record_from_rejects_missing_or_blank_session_ids(session_id: object):
    payload = event("Stop")
    payload["session_id"] = session_id
    assert hook.record_from(payload, rev=1, now=2.0) is None


@pytest.mark.parametrize("cwd", [None, 123, [], {}, False])
def test_record_from_uses_empty_cwd_for_non_strings(cwd: object):
    record = hook.record_from(event("Stop", cwd=cwd), rev=1, now=2.0)
    assert record is not None
    assert record.cwd == ""


def test_run_writes_a_record_with_injected_clocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    monkeypatch.setattr(hook.time, "time_ns", lambda: 987654321)
    monkeypatch.setattr(hook.time, "time", lambda: 1234.5)
    stream = io.StringIO(json.dumps(event("Stop")))

    assert hook.run(stream, tmp_path / "state") == 0
    assert store.read_all(tmp_path / "state") == [
        SessionRecord(
            session_id="session-1",
            cwd="/workspace",
            state=AgentState.DONE,
            rev=987654321,
            updated_at=1234.5,
        )
    ]
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("name", ["SessionEnd", "SubagentStop", "Unknown"])
def test_run_ignored_events_write_nothing_and_emit_nothing(
    name: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    assert hook.run(io.StringIO(json.dumps(event(name))), tmp_path / "state") == 0
    assert store.read_all(tmp_path / "state") == []
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("payload", [None, [], "event", 3, False])
def test_run_non_object_json_is_fail_closed(
    payload: object, tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    assert hook.run(io.StringIO(json.dumps(payload)), tmp_path / "state") == 0
    assert store.read_all(tmp_path / "state") == []
    assert capsys.readouterr() == ("", "")


def test_run_malformed_json_is_fail_closed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    assert hook.run(io.StringIO("{not json"), tmp_path / "state") == 0
    assert store.read_all(tmp_path / "state") == []
    assert capsys.readouterr() == ("", "")


def test_run_store_failure_is_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    def fail_write(record: SessionRecord, root: Path) -> bool:
        raise OSError("disk unavailable")

    monkeypatch.setattr(hook, "write", fail_write)

    assert hook.run(
        io.StringIO(json.dumps(event("Stop"))), tmp_path / "state"
    ) == 0
    assert capsys.readouterr() == ("", "")


def test_run_path_traversal_session_id_is_fail_closed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    payload = event("Stop", session_id="../escaped")

    assert hook.run(io.StringIO(json.dumps(payload)), tmp_path / "state") == 0
    assert not (tmp_path / "escaped.json").exists()
    assert list(tmp_path.rglob("*.json")) == []
    assert capsys.readouterr() == ("", "")


def test_run_unexpected_processing_error_is_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    def fail_record(event: object, rev: int, now: float) -> SessionRecord | None:
        raise RuntimeError("unexpected")

    monkeypatch.setattr(hook, "record_from", fail_record)

    assert hook.run(
        io.StringIO(json.dumps(event("Stop"))), tmp_path / "state"
    ) == 0
    assert store.read_all(tmp_path / "state") == []
    assert capsys.readouterr() == ("", "")


# --- health (installed but unrunnable) ---------------------------------------

def _settings(path: Path, command: str) -> Path:
    path.write_text(json.dumps({
        "hooks": {
            "Stop": [{"hooks": [
                {"type": "command", "command": "bash ~/.claude/hooks/auto-fmt.sh"},
                {"type": "command", "command": command},
            ]}],
            "SessionStart": [{"hooks": [{"type": "command", "command": command}]}],
        }
    }))
    return path


def test_health_names_a_hook_interpreter_that_cannot_run(tmp_path: Path):
    """Hooks fail open, so a stale interpreter is silent -- every session goes
    white with nothing to read anywhere. This is the only place that says so."""
    gone = tmp_path / "Cellar" / "semapad" / "0.1.0" / "bin" / "python"
    settings = _settings(tmp_path / "settings.json",
                         f"{gone} -m semapad.cli hook")

    (diagnostic,) = hook.health(settings)
    assert str(gone) in diagnostic
    assert "install-hooks" in diagnostic


def test_health_is_quiet_when_the_interpreter_runs(tmp_path: Path):
    live = tmp_path / "python"
    live.write_text("")
    live.chmod(0o755)
    settings = _settings(tmp_path / "settings.json",
                         f"{live} -m semapad.cli hook")

    assert hook.health(settings) == ()


def test_health_ignores_absent_hooks_and_foreign_ones(tmp_path: Path):
    """No hooks is a supported setup, not a fault (spec principle 4), and a
    broken hook that is not ours is not ours to report."""
    missing = tmp_path / "settings.json"
    assert hook.health(missing) == ()

    missing.write_text(json.dumps({"model": "opus"}))
    assert hook.health(missing) == ()

    missing.write_text(json.dumps({"hooks": {"Stop": [{"hooks": [
        {"type": "command", "command": "/nowhere/python -m otherpkg.cli hook"}]}]}}))
    assert hook.health(missing) == ()


def test_health_reports_each_broken_interpreter_once(tmp_path: Path):
    """11 events share one command; the reader needs one line, not eleven."""
    gone = tmp_path / "gone" / "python"
    settings = _settings(tmp_path / "settings.json",
                         f"{gone} -m semapad.cli hook")
    assert len(hook.health(settings)) == 1


def test_health_survives_a_malformed_settings_file(tmp_path: Path):
    """Diagnosing a problem must never become one."""
    bad = tmp_path / "settings.json"
    bad.write_text("{broken")
    assert hook.health(bad) == ()
    bad.write_text(json.dumps({"hooks": {"Stop": "not-a-list"}}))
    assert hook.health(bad) == ()
    bad.write_text(json.dumps({"hooks": {"Stop": [{"hooks": [
        {"type": "command", "command": ['not', 'a', 'string']}]}]}}))
    assert hook.health(bad) == ()
