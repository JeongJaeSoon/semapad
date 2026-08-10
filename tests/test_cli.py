from __future__ import annotations

import io
import json
import sys
from pathlib import Path

from semapad import cli


def paths(tmp_path: Path) -> cli.Paths:
    return cli.Paths.from_env({
        "HOME": str(tmp_path),
        "SEMAPAD_HOME": str(tmp_path / ".semapad"),
        "SEMAPAD_CLAUDE_SETTINGS": str(tmp_path / "settings.json"),
    })


def test_paths_env_overrides(tmp_path: Path):
    p = cli.Paths.from_env({
        "HOME": "/h",
        "SEMAPAD_HOME": "/custom/home",
        "SEMAPAD_CLAUDE_SETTINGS": "/c/settings.json",
        "SEMAPAD_CLAUDE_SESSIONS": "/c/sessions",
        "SEMAPAD_MAPPING_DIR": "/c/mapping",
    })
    assert p.home == Path("/custom/home")
    assert p.state_dir == Path("/custom/home/state")
    assert p.config_path == Path("/custom/home/config.json")
    assert p.claude_settings == Path("/c/settings.json")
    assert p.sessions_dir == Path("/c/sessions")
    assert p.mapping_dir == Path("/c/mapping")


def test_paths_defaults(tmp_path: Path):
    p = cli.Paths.from_env({"HOME": str(tmp_path)})
    assert p.home == tmp_path / ".semapad"
    assert p.claude_settings == tmp_path / ".claude" / "settings.json"
    assert p.mapping_dir == (tmp_path / "Library" / "Application Support"
                             / "Claude" / "claude-code-sessions")


def _install(p: cli.Paths) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = cli.install_hooks(p, out, err)
    return code, out.getvalue(), err.getvalue()


def test_install_hooks_into_missing_settings(tmp_path: Path):
    p = paths(tmp_path)
    code, out, _ = _install(p)
    assert code == 0
    settings = json.loads(p.claude_settings.read_text())
    assert set(settings["hooks"]) == set(cli._HOOK_EVENTS)
    for event in cli._HOOK_EVENTS:
        (entry,) = settings["hooks"][event]
        (item,) = entry["hooks"]
        assert item["command"].endswith("-m semapad.cli hook")
        assert Path(item["command"].split()[0]).is_absolute()
    assert "installed 11 hooks" in out


def test_install_hooks_is_idempotent(tmp_path: Path):
    p = paths(tmp_path)
    _install(p)
    code, out, _ = _install(p)
    assert code == 0
    assert "already installed" in out


def test_install_hooks_claims_old_paneglow_entries(tmp_path: Path):
    p = paths(tmp_path)
    paneglow = {"hooks": [{"type": "command",
                           "command": "/old/venv/bin/python -m paneglow.cli hook"}]}
    foreign = {"hooks": [{"type": "command",
                          "command": "bash $HOME/.claude/hooks/auto-fmt.sh"}]}
    p.claude_settings.write_text(json.dumps({
        "hooks": {event: [paneglow] for event in cli._HOOK_EVENTS}
        | {"Stop": [foreign, paneglow]},
        "model": "opus",
    }))
    code, out, _ = _install(p)
    assert code == 0
    settings = json.loads(p.claude_settings.read_text())
    text = json.dumps(settings)
    # spec §10 contract: no old module invocation survives. The interpreter
    # path may legitimately contain "paneglow" (e.g. its venv), so match the
    # module-call shape, not the bare word.
    assert "-m paneglow.cli hook" not in text         # no double firing
    assert "auto-fmt.sh" in text                      # foreign hooks untouched
    assert settings["model"] == "opus"                # unrelated settings kept
    for event in cli._HOOK_EVENTS:
        commands = [item["command"] for entry in settings["hooks"][event]
                    for item in entry["hooks"]]
        assert sum("semapad.cli" in c for c in commands) == 1
    assert "claimed 11" in out
    backup = p.claude_settings.with_name("settings.json.semapad.bak")
    assert "paneglow" in backup.read_text()


def test_install_hooks_malformed_settings_leaves_file_alone(tmp_path: Path):
    p = paths(tmp_path)
    p.claude_settings.write_text("{broken")
    code, _, err = _install(p)
    assert code == 1
    assert p.claude_settings.read_text() == "{broken"
    assert "unchanged" in err


def test_hook_command_shape_matches_recognizer(tmp_path: Path):
    assert cli._is_owned_hook_command(cli._hook_command())
    assert cli._is_owned_hook_command(
        "/old/venv/bin/python -m paneglow.cli hook")
    assert not cli._is_owned_hook_command("python -m paneglow.cli hook")  # relative
    assert not cli._is_owned_hook_command(
        "/usr/bin/python -m otherpkg.cli hook")


def test_cmd_hook_writes_record(tmp_path: Path, monkeypatch):
    p = paths(tmp_path)
    event = {"hook_event_name": "Stop", "session_id": "abc", "cwd": "/w"}
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(json.dumps(event)))
    assert cli._cmd_hook(p) == 0
    from semapad.sources import hooks
    (record,) = hooks.read_all(p.state_dir)
    assert record.session_id == "abc"
    assert record.state.value == "done"


def test_uninstall_hooks_removes_only_owned_entries(tmp_path: Path):
    p = paths(tmp_path)
    foreign = {"hooks": [{"type": "command",
                          "command": "bash $HOME/.claude/hooks/auto-fmt.sh"}]}
    _install(p)
    settings = json.loads(p.claude_settings.read_text())
    settings["hooks"]["Stop"].insert(0, foreign)
    p.claude_settings.write_text(json.dumps(settings))

    import io
    out, err = io.StringIO(), io.StringIO()
    code = cli.uninstall_hooks(p, out, err)
    assert code == 0 and "removed 11" in out.getvalue()
    after = json.loads(p.claude_settings.read_text())
    assert after["hooks"] == {"Stop": [foreign]}    # foreign hook survives
    assert "-m semapad.cli hook" not in json.dumps(after)


def test_uninstall_hooks_when_none_is_a_noop(tmp_path: Path):
    p = paths(tmp_path)
    p.claude_settings.write_text(json.dumps({"model": "opus"}))
    import io
    out, err = io.StringIO(), io.StringIO()
    code = cli.uninstall_hooks(p, out, err)
    assert code == 0 and "no hooks installed" in out.getvalue()
    assert json.loads(p.claude_settings.read_text()) == {"model": "opus"}


def test_stable_interpreter_prefers_brew_opt_path(tmp_path: Path, monkeypatch):
    import sys as sys_mod

    cellar = tmp_path / "Cellar" / "semapad" / "0.1.2" / "libexec" / "bin"
    opt = tmp_path / "opt" / "semapad" / "libexec" / "bin"
    cellar.mkdir(parents=True)
    opt.mkdir(parents=True)
    (cellar / "python").write_text("")
    (opt / "python").write_text("")
    monkeypatch.setattr(sys_mod, "executable", str(cellar / "python"))
    assert cli._stable_interpreter() == str(opt / "python")


def test_stable_interpreter_keeps_non_brew_paths(tmp_path: Path, monkeypatch):
    import sys as sys_mod

    venv = tmp_path / "project" / ".venv" / "bin" / "python"
    venv.parent.mkdir(parents=True)
    venv.write_text("")
    monkeypatch.setattr(sys_mod, "executable", str(venv))
    assert cli._stable_interpreter() == str(venv)


def test_ui_port_in_use_prints_help_not_a_traceback(tmp_path: Path, capsys, monkeypatch):
    import errno

    from semapad import cli

    def busy(dashboard, port):
        raise OSError(errno.EADDRINUSE, "Address already in use")

    monkeypatch.setattr("semapad.web.server.make_server", busy)
    p = paths(tmp_path)
    code = cli._cmd_ui(p, 8642, open_browser=False)
    err = capsys.readouterr().err
    assert code == 1
    assert "already in use" in err and "--port" in err
