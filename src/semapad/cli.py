"""semapad CLI -- Phase 1 surface: ``ui``, ``hook``, ``install-hooks``.

Thin by design (spec §7): no colour or slot logic lives here.
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import stat
import sys
import tempfile
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, TextIO

from semapad.sources import hooks

_MAX_SETTINGS_BYTES = 1 << 20

#: Every event the classifier understands (spec §2: hooks are the only signal).
_HOOK_EVENTS = (
    "SessionStart",
    "UserPromptSubmit",
    "PreToolUse",
    "PostToolUse",
    "PostToolUseFailure",
    "PermissionDenied",
    "Notification",
    "Stop",
    "StopFailure",
    "PreCompact",
    "SessionEnd",
)


class SettingsError(RuntimeError):
    """Claude settings could not be read or written safely."""


@dataclass(frozen=True)
class Paths:
    """All mutable and user-owned paths (spec §1 naming)."""

    home: Path
    state_dir: Path
    config_path: Path
    runtime_dir: Path
    snapshot_path: Path
    claude_settings: Path
    sessions_dir: Path
    mapping_dir: Path

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Paths":
        source = os.environ if env is None else env
        user_home = Path(source.get("HOME", str(Path.home()))).expanduser()
        home = Path(source.get("SEMAPAD_HOME", str(user_home / ".semapad")))
        return cls(
            home=home,
            state_dir=home / "state",
            config_path=home / "config.json",
            runtime_dir=home / "runtime",
            snapshot_path=home / "runtime" / "snapshot.json",
            claude_settings=Path(source.get(
                "SEMAPAD_CLAUDE_SETTINGS",
                str(user_home / ".claude" / "settings.json"))),
            sessions_dir=Path(source.get(
                "SEMAPAD_CLAUDE_SESSIONS",
                str(user_home / ".claude" / "sessions"))),
            mapping_dir=Path(source.get(
                "SEMAPAD_MAPPING_DIR",
                str(user_home / "Library" / "Application Support" / "Claude"
                    / "claude-code-sessions"))),
        )


def _private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, stat.S_IRWXU)


# --- Claude settings I/O (trust boundary: user file, other tools write it too) --

def _reject_constant(name: str) -> None:
    raise ValueError(f"JSON constant {name} is not allowed")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _read_settings(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    if path.is_symlink() or not path.is_file():
        raise SettingsError("Claude settings path is unsafe")
    raw = path.read_bytes()
    if len(raw) > _MAX_SETTINGS_BYTES:
        raise SettingsError("Claude settings file is too large")
    try:
        value = json.loads(raw, parse_constant=_reject_constant,
                           object_pairs_hook=_unique_object)
    except (UnicodeError, ValueError, RecursionError) as error:
        raise SettingsError("Claude settings JSON is malformed") from error
    if type(value) is not dict:
        raise SettingsError("Claude settings must be an object")
    return value


def _atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(value, f, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        Path(tmp).unlink(missing_ok=True)
        raise


# --- hook installation with paneglow migration (spec §10) --------------------

def _hook_command() -> str:
    return shlex.join((sys.executable, "-m", "semapad.cli", "hook"))


def _is_owned_hook_command(value: object) -> bool:
    """Recognize semapad's own hook command AND the old paneglow one.

    Spec §10: if the installer does not claim the paneglow-era entries, both
    generations stay installed and every event fires twice.
    """
    if type(value) is not str:
        return False
    try:
        arguments = shlex.split(value)
    except ValueError:
        return False
    return (
        len(arguments) == 4
        and Path(arguments[0]).is_absolute()
        and arguments[1] == "-m"
        and arguments[2] in ("semapad.cli", "paneglow.cli")
        and arguments[3] == "hook"
    )


def _owned_entry(value: object) -> bool:
    """Match the exact standalone entry shape this installer (or paneglow's) emits."""
    if type(value) is not dict or set(value) != {"hooks"}:
        return False
    entry_hooks = value.get("hooks")
    if type(entry_hooks) is not list or len(entry_hooks) != 1:
        return False
    item = entry_hooks[0]
    return (type(item) is dict and set(item) == {"type", "command"}
            and item.get("type") == "command"
            and _is_owned_hook_command(item.get("command")))


def install_hooks(paths: Paths, stdout: TextIO, stderr: TextIO) -> int:
    try:
        settings = _read_settings(paths.claude_settings)
        existing_hooks = settings.get("hooks")
        if existing_hooks is None:
            existing_hooks = {}
        if type(existing_hooks) is not dict:
            raise SettingsError("Claude hooks setting must be an object")

        canonical = {"hooks": [{"type": "command", "command": _hook_command()}]}
        migrated = 0
        changed = False
        merged = dict(existing_hooks)
        for event in _HOOK_EVENTS:
            entries = merged.get(event, [])
            if type(entries) is not list:
                raise SettingsError("Claude hook event must be a list")
            kept = [e for e in entries if not _owned_entry(e)]
            migrated += len(entries) - len(kept)
            kept.append(canonical)
            if kept != entries:
                changed = True
            merged[event] = kept
        if not changed:
            print("semapad: hooks already installed", file=stdout)
            return 0

        updated = dict(settings)
        updated["hooks"] = merged
        if paths.claude_settings.exists():
            backup = paths.claude_settings.with_name(
                paths.claude_settings.name + ".semapad.bak")
            backup.write_bytes(paths.claude_settings.read_bytes())
        _atomic_write_json(paths.claude_settings, updated)
    except (OSError, SettingsError) as error:
        print(f"semapad: hooks were not installed; settings are unchanged "
              f"({error})", file=stderr)
        return 1
    print(f"semapad: installed {len(_HOOK_EVENTS)} hooks"
          + (f" (claimed {migrated} previous semapad/paneglow entries)"
             if migrated else ""),
          file=stdout)
    return 0


def uninstall_hooks(paths: Paths, stdout: TextIO, stderr: TextIO) -> int:
    """Remove exactly the entries install_hooks owns; foreign hooks stay."""
    try:
        settings = _read_settings(paths.claude_settings)
        existing_hooks = settings.get("hooks")
        if type(existing_hooks) is not dict:
            print("semapad: no hooks installed", file=stdout)
            return 0
        removed = 0
        merged = {}
        for event, entries in existing_hooks.items():
            if type(entries) is not list:
                merged[event] = entries
                continue
            kept = [e for e in entries if not _owned_entry(e)]
            removed += len(entries) - len(kept)
            if kept:
                merged[event] = kept
        if not removed:
            print("semapad: no hooks installed", file=stdout)
            return 0
        updated = dict(settings)
        if merged:
            updated["hooks"] = merged
        else:
            updated.pop("hooks", None)
        backup = paths.claude_settings.with_name(
            paths.claude_settings.name + ".semapad.bak")
        backup.write_bytes(paths.claude_settings.read_bytes())
        _atomic_write_json(paths.claude_settings, updated)
    except (OSError, SettingsError) as error:
        print(f"semapad: hooks were not removed; settings are unchanged "
              f"({error})", file=stderr)
        return 1
    print(f"semapad: removed {removed} hook entries", file=stdout)
    return 0


# --- commands ----------------------------------------------------------------

def _cmd_hook(paths: Paths) -> int:
    try:
        _private_directory(paths.home)
        _private_directory(paths.state_dir)
    except OSError:
        return 0   # never interrupt a Claude turn
    return hooks.run(sys.stdin, paths.state_dir)


def _cmd_daemon(paths: Paths) -> int:
    import fcntl
    import signal
    import time

    from semapad import config as config_mod
    from semapad.daemon import Daemon

    # One daemon per machine: launchd and a manual run must never coexist
    # (three writers on one pad). The descriptor stays open for the lifetime
    # of the process; the OS releases the lock on any kind of exit.
    paths.runtime_dir.mkdir(parents=True, exist_ok=True)
    lock_file = open(paths.runtime_dir / "daemon.lock", "w")
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("semapad: daemon already running", file=sys.stderr)
        return 1

    cfg, warnings = config_mod.load(paths.config_path)
    for warning in warnings:
        print(f"semapad: config: {warning}", file=sys.stderr)
    daemon = Daemon(cfg, state_dir=paths.state_dir,
                    mapping_dir=paths.mapping_dir,
                    sessions_dir=paths.sessions_dir,
                    config_path=paths.config_path,
                    snapshot_path=paths.snapshot_path)
    daemon.async_scan = True   # Codex rec #3: keep the 58 ms scan off the HID thread
    stop = {"flag": False}

    def _terminate(_signum, _frame) -> None:
        stop["flag"] = True

    signal.signal(signal.SIGTERM, _terminate)
    signal.signal(signal.SIGINT, _terminate)
    print("semapad daemon: running (Ctrl-C to stop)")
    try:
        while not stop["flag"]:
            now = time.time()
            daemon.tick(now)
            # A verified pad already blocked inside poll_received for poll_ms;
            # only sleep when the tick had no pad to wait on.
            if daemon.verified_layer is None:
                time.sleep(daemon.cfg.poll_ms / 1000.0)
    finally:
        daemon.close()
    return 0


def _cmd_ui(paths: Paths, port: int, open_browser: bool) -> int:
    from semapad.web.server import Dashboard, make_server

    dashboard = Dashboard(state_dir=paths.state_dir,
                          mapping_dir=paths.mapping_dir,
                          sessions_dir=paths.sessions_dir,
                          config_path=paths.config_path,
                          daemon_snapshot_path=paths.snapshot_path)
    server = make_server(dashboard, port)
    url = f"http://127.0.0.1:{port}"
    print(f"semapad ui: {url}")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def _stable_interpreter() -> str:
    """Prefer Homebrew's version-stable opt path over the Cellar path.

    Under brew, sys.executable lives in Cellar/semapad/<version>/ -- a path
    that vanishes on `brew upgrade`, silently breaking the LaunchAgent at the
    next login. The opt/ symlink survives upgrades.
    """
    import re

    executable = sys.executable
    match = re.match(r"^(.*)/Cellar/([^/]+)/[^/]+/(.*)$", executable)
    if match:
        candidate = Path(f"{match.group(1)}/opt/{match.group(2)}/{match.group(3)}")
        if candidate.exists():
            return str(candidate)
    return executable


def _cmd_autostart(paths: Paths, action: str, stdout=None) -> int:
    import subprocess

    from semapad import launch_agent

    stdout = sys.stdout if stdout is None else stdout
    spec = launch_agent.build_spec(
        command_prefix=(_stable_interpreter(), "-m", "semapad.cli"),
        runtime_home=paths.home,
        log_path=paths.home / "logs" / "daemon.log",
        runtime_environment={"SEMAPAD_HOME": paths.home},
        account_home=paths.home.parent,
    )
    controller = launch_agent.Controller(spec)
    inspection = launch_agent.inspect_manifest(spec)

    if action == "status":
        loaded = controller.loaded() if inspection.status != "missing" else False
        print(f"autostart   {inspection.status}"
              + (" | loaded" if loaded else " | not loaded"), file=stdout)
        return 0

    if action == "install":
        launch_agent.ensure_install_directories(spec)
        launch_agent.validate_program(spec)
        launch_agent.ensure_private_log(spec)
        launch_agent.ensure_lock_directory(spec)
        with launch_agent.InstallLock(spec):
            launch_agent.atomic_write_manifest(spec)
            try:
                if controller.loaded():
                    controller.bootout()
            except launch_agent.LaunchAgentError:
                pass
            # A manually started daemon must hand over before launchd's
            # instance starts, or the new one exits on the daemon lock.
            subprocess.run(["/usr/bin/pkill", "-f", "semapad(\\.cli)? daemon"],
                           check=False)
            # launchd can refuse a bootstrap for a label it just booted out;
            # a short retry absorbs that window (observed live, 2026-08-10).
            import time as time_mod
            for attempt in range(3):
                try:
                    controller.bootstrap()
                    break
                except launch_agent.LaunchAgentError:
                    if attempt == 2:
                        raise
                    time_mod.sleep(1.0)
        print("semapad: installed login autostart", file=stdout)
        return 0

    if action == "uninstall":
        if inspection.status == "missing":
            print("semapad: autostart not installed", file=stdout)
            return 0
        with launch_agent.InstallLock(spec):
            try:
                if controller.loaded():
                    controller.bootout()
            except launch_agent.LaunchAgentError:
                pass
            launch_agent.remove_manifest(spec, inspection)
        print("semapad: uninstalled login autostart", file=stdout)
        return 0
    return 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="semapad")
    sub = parser.add_subparsers(dest="command", required=True)
    ui = sub.add_parser("ui", help="run the read-only dashboard")
    ui.add_argument("--port", type=int, default=None)
    ui.add_argument("--no-open", action="store_true")
    sub.add_parser("hook", help="consume one Claude hook event from stdin")
    sub.add_parser("install-hooks",
                   help="install Claude hooks, claiming old paneglow entries")
    sub.add_parser("uninstall-hooks",
                   help="remove semapad's Claude hooks, leaving others intact")
    sub.add_parser("daemon", help="run the pad daemon in the foreground")
    autostart = sub.add_parser("autostart", help="manage the login LaunchAgent")
    autostart.add_argument("action", choices=["install", "uninstall", "status"])

    args = parser.parse_args(argv)
    paths = Paths.from_env()
    if args.command == "hook":
        return _cmd_hook(paths)
    if args.command == "daemon":
        return _cmd_daemon(paths)
    if args.command == "autostart":
        return _cmd_autostart(paths, args.action)
    if args.command == "install-hooks":
        return install_hooks(paths, sys.stdout, sys.stderr)
    if args.command == "uninstall-hooks":
        return uninstall_hooks(paths, sys.stdout, sys.stderr)
    if args.command == "ui":
        from semapad.web.server import DEFAULT_PORT
        port = args.port if args.port is not None else DEFAULT_PORT
        return _cmd_ui(paths, port, open_browser=not args.no_open)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
