from __future__ import annotations

import io
import os
import plistlib
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from semapad import cli, launch_agent


def make_spec(tmp_path: Path, *, program: Path | None = None) -> launch_agent.Spec:
    account = tmp_path / "user"
    library = account / "Library"
    library.mkdir(parents=True, mode=0o700, exist_ok=True)
    library.chmod(0o700)
    runtime_home = account / ".semapad"
    selected_program = Path(sys.executable) if program is None else program
    return launch_agent.build_spec(
        command_prefix=(str(selected_program), "-m", "semapad.cli"),
        runtime_home=runtime_home,
        log_path=runtime_home / "logs" / "daemon.log",
        account_home=account,
        uid=os.getuid(),
    )


def prepare(spec: launch_agent.Spec) -> None:
    launch_agent.ensure_install_directories(spec)
    launch_agent.ensure_private_log(spec)


class FakeController:
    def __init__(self, *, loaded: bool = False, fail: str | None = None) -> None:
        self.is_loaded = loaded
        self.fail = fail
        self.calls: list[str] = []

    def loaded(self) -> bool:
        self.calls.append("loaded")
        return self.is_loaded

    def bootstrap(self) -> None:
        self.calls.append("bootstrap")
        if self.fail == "bootstrap":
            raise launch_agent.LaunchAgentError("failed")
        self.is_loaded = True

    def bootout(self) -> None:
        self.calls.append("bootout")
        if self.fail == "bootout":
            raise launch_agent.LaunchAgentError("failed")
        self.is_loaded = False

    def kickstart(self) -> None:
        self.calls.append("kickstart")
        if self.fail == "kickstart":
            raise launch_agent.LaunchAgentError("failed")


def test_manifest_is_exact_and_keeps_lexical_venv_interpreter(tmp_path: Path):
    target = Path(sys.executable)
    program = tmp_path / "venv" / "bin" / "python"
    program.parent.mkdir(parents=True)
    program.symlink_to(target)
    spec = make_spec(tmp_path, program=program)

    assert spec.plist_path == (
        tmp_path / "user" / "Library" / "LaunchAgents" /
        "io.github.jeongjaesoon.semapad.plist"
    )
    assert spec.manifest == {
        "Label": launch_agent.LABEL,
        "Program": str(program),
        "ProgramArguments": [str(program), "-m", "semapad.cli", "daemon"],
        "WorkingDirectory": "/",
        "EnvironmentVariables": {"HOME": str(tmp_path / "user")},
        "KeepAlive": {"SuccessfulExit": False},
        "Umask": "077",
        "ExitTimeOut": 5,
        "StandardOutPath": str(tmp_path / "user/.semapad/logs/daemon.log"),
        "StandardErrorPath": str(tmp_path / "user/.semapad/logs/daemon.log"),
    }
    assert plistlib.loads(spec.payload) == spec.manifest
    assert Path(spec.command[0]) == program
    assert Path(spec.command[0]).resolve() == target.resolve()
    launch_agent.validate_program(spec)


def test_manifest_rejects_non_semapad_command_and_environment(tmp_path: Path):
    with pytest.raises(launch_agent.LaunchAgentError):
        launch_agent.build_spec(
            command_prefix=(str(Path(sys.executable)), "script.py"),
            runtime_home=tmp_path,
            log_path=tmp_path / "log",
            account_home=tmp_path,
            uid=os.getuid(),
        )
    with pytest.raises(launch_agent.LaunchAgentError):
        launch_agent.build_spec(
            command_prefix=(str(Path(sys.executable)), "-m", "semapad.cli"),
            runtime_home=tmp_path,
            log_path=tmp_path / "log",
            runtime_environment={"PYTHONPATH": tmp_path},
            account_home=tmp_path,
            uid=os.getuid(),
        )


@pytest.mark.parametrize(
    "kind", ["missing", "directory", "not_executable", "shared_writable"]
)
def test_program_validation_requires_executable_regular_target(
    tmp_path: Path, kind: str
):
    program = tmp_path / "python"
    if kind == "directory":
        program.mkdir()
    elif kind == "not_executable":
        program.write_text("python")
        program.chmod(0o600)
    elif kind == "shared_writable":
        program.write_text("python")
        program.chmod(0o777)
    spec = make_spec(tmp_path, program=program)
    with pytest.raises(launch_agent.LaunchAgentError):
        launch_agent.validate_program(spec)


def test_atomic_manifest_is_private_current_and_byte_idempotent(tmp_path: Path):
    spec = make_spec(tmp_path)
    prepare(spec)
    launch_agent.atomic_write_manifest(spec)
    first = spec.plist_path.read_bytes()
    launch_agent.atomic_write_manifest(spec)

    metadata = spec.plist_path.stat()
    assert stat.S_IMODE(metadata.st_mode) == 0o600
    assert metadata.st_uid == os.getuid()
    assert metadata.st_nlink == 1
    assert spec.plist_path.read_bytes() == first == spec.payload
    assert launch_agent.inspect_manifest(spec).status == "current"


def test_old_interpreter_manifest_is_recognized_for_migration(tmp_path: Path):
    spec = make_spec(tmp_path)
    prepare(spec)
    old = dict(spec.manifest)
    old_program = "/old/location/.venv/bin/python"
    old["Program"] = old_program
    old["ProgramArguments"] = [old_program, "-m", "semapad.cli", "daemon"]
    payload = plistlib.dumps(old, fmt=plistlib.FMT_XML, sort_keys=True)
    launch_agent.atomic_write_manifest(spec, payload)

    inspection = launch_agent.inspect_manifest(spec)
    assert inspection.status == "recognized"
    assert inspection.owned


def test_unknown_manifest_is_not_owned_or_replaced(tmp_path: Path):
    spec = make_spec(tmp_path)
    prepare(spec)
    unknown = dict(spec.manifest)
    unknown["ProgramArguments"] = ["/bin/sh", "-c", "payload"]
    payload = plistlib.dumps(unknown, fmt=plistlib.FMT_XML, sort_keys=True)
    launch_agent.atomic_write_manifest(spec, payload)

    inspection = launch_agent.inspect_manifest(spec)
    assert inspection.status == "unknown"
    assert not inspection.owned
    assert spec.plist_path.read_bytes() == payload


def test_manifest_recognition_rejects_boolean_type_confusion(tmp_path: Path):
    spec = make_spec(tmp_path)
    prepare(spec)
    confused = dict(spec.manifest)
    confused["KeepAlive"] = {"SuccessfulExit": 0}
    payload = plistlib.dumps(confused, fmt=plistlib.FMT_XML, sort_keys=True)
    launch_agent.atomic_write_manifest(spec, payload)

    assert launch_agent.inspect_manifest(spec).status == "unknown"


@pytest.mark.parametrize("kind", ["symlink", "mode", "hardlink", "malformed"])
def test_unsafe_manifest_is_never_recognized(tmp_path: Path, kind: str):
    spec = make_spec(tmp_path)
    prepare(spec)
    if kind == "symlink":
        target = tmp_path / "target"
        target.write_bytes(spec.payload)
        spec.plist_path.symlink_to(target)
    else:
        spec.plist_path.write_bytes(
            b"not plist" if kind == "malformed" else spec.payload
        )
        spec.plist_path.chmod(0o600)
        if kind == "mode":
            spec.plist_path.chmod(0o644)
        elif kind == "hardlink":
            os.link(spec.plist_path, tmp_path / "second-link")
    assert launch_agent.inspect_manifest(spec).status == "unsafe"


def test_remove_manifest_is_bound_to_inspected_inode(tmp_path: Path):
    spec = make_spec(tmp_path)
    prepare(spec)
    launch_agent.atomic_write_manifest(spec)
    inspection = launch_agent.inspect_manifest(spec)
    launch_agent.atomic_write_manifest(spec)

    with pytest.raises(launch_agent.LaunchAgentError, match="changed"):
        launch_agent.remove_manifest(spec, inspection)
    assert spec.plist_path.exists()


def test_remove_owned_manifest_and_missing_inspection(tmp_path: Path):
    spec = make_spec(tmp_path)
    prepare(spec)
    launch_agent.atomic_write_manifest(spec)
    launch_agent.remove_manifest(spec, launch_agent.inspect_manifest(spec))
    assert launch_agent.inspect_manifest(spec).status == "missing"


def test_private_log_and_directories_reject_links(tmp_path: Path):
    spec = make_spec(tmp_path)
    launch_agent.ensure_install_directories(spec)
    target = tmp_path / "target-log"
    target.write_text("safe")
    spec.log_path.symlink_to(target)
    with pytest.raises(launch_agent.LaunchAgentError):
        launch_agent.ensure_private_log(spec)
    assert target.read_text() == "safe"


def test_install_lock_rejects_hardlink(tmp_path: Path):
    spec = make_spec(tmp_path)
    prepare(spec)
    spec.lock_path.touch(mode=0o600)
    os.link(spec.lock_path, tmp_path / "lock-link")
    with pytest.raises(launch_agent.LaunchAgentError, match="lock"):
        with launch_agent.InstallLock(spec):
            pass


def test_install_lock_is_account_global_across_runtime_homes(tmp_path: Path):
    account = tmp_path / "user"
    (account / "Library").mkdir(parents=True, mode=0o700)
    first_home = account / ".semapad"
    second_home = account / ".semapad-alt"
    common = {
        "command_prefix": (str(Path(sys.executable)), "-m", "semapad.cli"),
        "account_home": account,
        "uid": os.getuid(),
    }
    first = launch_agent.build_spec(
        runtime_home=first_home,
        log_path=first_home / "logs" / "daemon.log",
        **common,
    )
    second = launch_agent.build_spec(
        runtime_home=second_home,
        log_path=second_home / "logs" / "daemon.log",
        runtime_environment={"SEMAPAD_HOME": second_home},
        **common,
    )
    launch_agent.ensure_lock_directory(first)

    assert first.plist_path == second.plist_path
    assert first.lock_path == second.lock_path
    with launch_agent.InstallLock(first):
        with pytest.raises(BlockingIOError):
            with launch_agent.InstallLock(second):
                pass


def test_install_hardens_legacy_runtime_home_mode(tmp_path: Path):
    spec = make_spec(tmp_path)
    runtime_home = spec.account_home / ".semapad"
    runtime_home.mkdir(mode=0o755)
    runtime_home.chmod(0o755)

    launch_agent.ensure_install_directories(spec)

    assert stat.S_IMODE(runtime_home.stat().st_mode) == 0o700


def test_recognized_manifest_cannot_move_runtime_outside_account(tmp_path: Path):
    spec = make_spec(tmp_path)
    prepare(spec)
    outside = tmp_path / "outside"
    manifest = dict(spec.manifest)
    manifest["EnvironmentVariables"] = {
        "HOME": str(spec.account_home),
        "SEMAPAD_HOME": str(outside),
    }
    manifest["StandardOutPath"] = str(outside / "logs" / "daemon.log")
    manifest["StandardErrorPath"] = str(outside / "logs" / "daemon.log")
    launch_agent.atomic_write_manifest(
        spec, plistlib.dumps(manifest, fmt=plistlib.FMT_XML, sort_keys=True)
    )

    assert launch_agent.inspect_manifest(spec).status == "unknown"


@pytest.mark.parametrize("kind", ["library_symlink", "agents_symlink", "shared"])
def test_manifest_ancestor_chain_is_validated_without_following_links(
    tmp_path: Path, kind: str
):
    account = tmp_path / "user"
    account.mkdir(mode=0o700)
    runtime_home = account / ".semapad"
    spec = launch_agent.build_spec(
        command_prefix=(str(Path(sys.executable)), "-m", "semapad.cli"),
        runtime_home=runtime_home,
        log_path=runtime_home / "logs" / "daemon.log",
        account_home=account,
        uid=os.getuid(),
    )
    if kind == "library_symlink":
        real_library = tmp_path / "real-library"
        agents = real_library / "LaunchAgents"
        agents.mkdir(parents=True, mode=0o700)
        (account / "Library").symlink_to(real_library, target_is_directory=True)
    else:
        library = account / "Library"
        library.mkdir(mode=0o700)
        if kind == "agents_symlink":
            agents = tmp_path / "real-agents"
            agents.mkdir(mode=0o700)
            (library / "LaunchAgents").symlink_to(
                agents, target_is_directory=True
            )
        else:
            library.chmod(0o777)
            agents = library / "LaunchAgents"
            agents.mkdir(mode=0o700)
    manifest_path = agents / launch_agent.PLIST_NAME
    manifest_path.write_bytes(spec.payload)
    manifest_path.chmod(0o600)

    assert launch_agent.inspect_manifest(spec).status == "unsafe"
    with pytest.raises(launch_agent.LaunchAgentError):
        launch_agent.ensure_lock_directory(spec)


def test_controller_uses_exact_launchctl_argv_and_discards_output(tmp_path: Path):
    spec = make_spec(tmp_path)
    calls = []

    def runner(arguments, **kwargs):
        calls.append((arguments, kwargs))
        return SimpleNamespace(returncode=0)

    controller = launch_agent.Controller(spec, runner=runner)
    assert controller.loaded() is True
    controller.bootstrap()
    controller.bootout()
    controller.kickstart()

    assert [call[0] for call in calls] == [
        ["/bin/launchctl", "print", spec.domain],
        ["/bin/launchctl", "print", spec.target],
        ["/bin/launchctl", "bootstrap", spec.domain, str(spec.plist_path)],
        ["/bin/launchctl", "bootout", spec.target],
        ["/bin/launchctl", "kickstart", spec.target],
    ]
    assert all(kwargs["stdin"] is subprocess.DEVNULL for _, kwargs in calls)
    assert all(kwargs["stdout"] is subprocess.DEVNULL for _, kwargs in calls)
    assert all(kwargs["stderr"] is subprocess.DEVNULL for _, kwargs in calls)
    assert all("shell" not in kwargs for _, kwargs in calls)


@pytest.mark.parametrize("kind", ["symlink", "shared"])
def test_runtime_home_unsafe_ancestor_is_rejected(tmp_path: Path, kind: str):
    account = tmp_path / "user"
    (account / "Library").mkdir(parents=True, mode=0o700)
    spec = make_spec(tmp_path)
    runtime_home = account / ".semapad"
    if kind == "symlink":
        target = account / "target"
        target.mkdir(mode=0o700)
        runtime_home.symlink_to(target, target_is_directory=True)
    else:
        runtime_home.mkdir(mode=0o777)
        runtime_home.chmod(0o777)
    with pytest.raises(launch_agent.LaunchAgentError):
        launch_agent.ensure_install_directories(spec)




# --- semapad cli glue (thin by design; the paneglow migration flows were not
# --- ported, so these cover exactly what _cmd_autostart and the daemon lock do)

def test_autostart_status_missing(tmp_path, monkeypatch, capsys):
    from semapad import cli
    paths = cli.Paths.from_env({"HOME": str(tmp_path),
                                "SEMAPAD_HOME": str(tmp_path / ".semapad")})
    code = cli._cmd_autostart(paths, "status")
    assert code == 0
    assert "missing" in capsys.readouterr().out


def test_autostart_uninstall_when_missing_is_a_noop(tmp_path, capsys):
    from semapad import cli
    paths = cli.Paths.from_env({"HOME": str(tmp_path),
                                "SEMAPAD_HOME": str(tmp_path / ".semapad")})
    assert cli._cmd_autostart(paths, "uninstall") == 0
    assert "not installed" in capsys.readouterr().out


def test_daemon_lock_rejects_a_second_instance(tmp_path):
    import fcntl

    from semapad import cli
    paths = cli.Paths.from_env({"HOME": str(tmp_path),
                                "SEMAPAD_HOME": str(tmp_path / ".semapad")})
    paths.runtime_dir.mkdir(parents=True)
    holder = open(paths.runtime_dir / "daemon.lock", "w")
    fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        assert cli._cmd_daemon(paths) == 1   # exits before touching hardware
    finally:
        holder.close()


def test_install_hardens_preexisting_0755_runtime_and_logs(tmp_path):
    """Manual nohup runs create runtime/ and logs/ with the default umask;
    install must tighten them in place, not reject the setup."""
    import os

    from semapad import launch_agent
    home = tmp_path / ".semapad"
    for sub in ("runtime", "logs"):
        (home / sub).mkdir(parents=True)
        os.chmod(home / sub, 0o755)
    os.chmod(home, 0o755)
    spec = launch_agent.build_spec(
        command_prefix=(str(tmp_path / "venv" / "python"), "-m", "semapad.cli"),
        runtime_home=home,
        log_path=home / "logs" / "daemon.log",
        runtime_environment={"SEMAPAD_HOME": home},
        account_home=tmp_path,
        uid=os.getuid(),
    )
    launch_agent._ensure_runtime_directories(spec)
    for path in (home, home / "runtime", home / "logs"):
        assert (os.lstat(path).st_mode & 0o777) == 0o700


def test_install_hardens_a_preexisting_0644_daemon_log(tmp_path):
    """`nohup ... >> daemon.log` leaves an umask-mode log behind; install
    tightens it on the validated descriptor instead of rejecting."""
    import os

    from semapad import launch_agent
    home = tmp_path / ".semapad"
    (home / "logs").mkdir(parents=True)
    log = home / "logs" / "daemon.log"
    log.write_text("old manual-run output\n")
    os.chmod(log, 0o644)
    spec = launch_agent.build_spec(
        command_prefix=(str(tmp_path / "venv" / "python"), "-m", "semapad.cli"),
        runtime_home=home,
        log_path=log,
        runtime_environment={"SEMAPAD_HOME": home},
        account_home=tmp_path,
        uid=os.getuid(),
    )
    launch_agent.ensure_private_log(spec)
    assert (os.lstat(log).st_mode & 0o777) == 0o600
