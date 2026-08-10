"""Private per-user LaunchAgent support for the semapad daemon.

The plist is deliberately small and deterministic.  launchd runs the existing
foreground ``daemon`` command; it must never run ``start``, which detaches a child
process and violates launchd's process-lifetime contract.
"""
from __future__ import annotations

import fcntl
import os
import plistlib
import pwd
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal, Mapping, Sequence


LABEL = "io.github.jeongjaesoon.semapad"
PLIST_NAME = f"{LABEL}.plist"
LAUNCHCTL = "/bin/launchctl"
_MAX_PLIST_BYTES = 1 << 20
_ALLOWED_ENVIRONMENT = frozenset(
    {
        "HOME",
        "SEMAPAD_HOME",
        "PANEGLOW_CLAUDE_SETTINGS",
        "PANEGLOW_CLAUDE_SESSIONS",
        "PANEGLOW_MAPPING_DIR",
    }
)
_MANIFEST_KEYS = frozenset(
    {
        "Label",
        "Program",
        "ProgramArguments",
        "WorkingDirectory",
        "EnvironmentVariables",
        "KeepAlive",
        "Umask",
        "ExitTimeOut",
        "StandardOutPath",
        "StandardErrorPath",
    }
)


class LaunchAgentError(RuntimeError):
    """A manifest, private path, or launchctl operation was not trustworthy."""


@dataclass(frozen=True)
class Spec:
    uid: int
    account_home: Path
    plist_path: Path
    lock_path: Path
    log_path: Path
    command: tuple[str, ...]
    environment: dict[str, str]
    manifest: dict[str, object]
    payload: bytes

    @property
    def domain(self) -> str:
        return f"gui/{self.uid}"

    @property
    def target(self) -> str:
        return f"{self.domain}/{LABEL}"


ManifestStatus = Literal["missing", "current", "recognized", "unknown", "unsafe"]


@dataclass(frozen=True)
class ManifestInspection:
    status: ManifestStatus
    payload: bytes | None = None
    device: int | None = None
    inode: int | None = None

    @property
    def owned(self) -> bool:
        return self.status in {"current", "recognized"}


def current_account_home() -> Path:
    """Return the login account's home, independent of a spoofed HOME value."""
    return Path(pwd.getpwuid(os.getuid()).pw_dir).absolute()


def build_spec(
    *,
    command_prefix: Sequence[str],
    runtime_home: Path,
    log_path: Path,
    runtime_environment: Mapping[str, Path] | None = None,
    account_home: Path | None = None,
    uid: int | None = None,
) -> Spec:
    """Build one canonical manifest without resolving the venv interpreter."""
    command = tuple(command_prefix)
    if len(command) != 3 or command[1:] != ("-m", "semapad.cli"):
        raise LaunchAgentError("unsupported semapad command prefix")
    program = Path(command[0])
    if not program.is_absolute():
        raise LaunchAgentError("LaunchAgent program must be absolute")

    login_home = (current_account_home() if account_home is None
                  else Path(account_home).absolute())
    current_uid = os.getuid() if uid is None else uid
    if current_uid < 1:
        raise LaunchAgentError("LaunchAgent installation as root is unsupported")

    environment = {"HOME": str(login_home)}
    for key, value in (runtime_environment or {}).items():
        if key not in _ALLOWED_ENVIRONMENT or key == "HOME":
            raise LaunchAgentError("unsupported LaunchAgent environment key")
        path = Path(value)
        if not path.is_absolute():
            raise LaunchAgentError("LaunchAgent environment paths must be absolute")
        environment[key] = str(path)

    runtime_root = Path(runtime_home)
    output = Path(log_path)
    if not runtime_root.is_absolute() or not output.is_absolute():
        raise LaunchAgentError("LaunchAgent mutable paths must be absolute")
    try:
        runtime_relative = runtime_root.relative_to(login_home)
    except ValueError as error:
        raise LaunchAgentError(
            "LaunchAgent runtime home must stay inside the login home") from error
    if not runtime_relative.parts or ".." in runtime_relative.parts:
        raise LaunchAgentError(
            "LaunchAgent runtime home must stay inside the login home")
    if output != runtime_root / "logs" / "daemon.log":
        raise LaunchAgentError("LaunchAgent log path is inconsistent")
    configured_runtime = environment.get(
        "SEMAPAD_HOME", str(login_home / ".semapad")
    )
    if Path(configured_runtime) != runtime_root:
        raise LaunchAgentError("LaunchAgent runtime environment is inconsistent")

    arguments = [*command, "daemon"]
    manifest: dict[str, object] = {
        "Label": LABEL,
        "Program": command[0],
        "ProgramArguments": arguments,
        "WorkingDirectory": "/",
        "EnvironmentVariables": environment,
        # A clean SIGTERM returns zero and remains stopped. Runtime failures
        # return non-zero and are restarted under launchd throttling.
        "KeepAlive": {"SuccessfulExit": False},
        "Umask": "077",
        "ExitTimeOut": 5,
        "StandardOutPath": str(output),
        "StandardErrorPath": str(output),
    }
    payload = plistlib.dumps(manifest, fmt=plistlib.FMT_XML, sort_keys=True)
    return Spec(
        uid=current_uid,
        account_home=login_home,
        plist_path=login_home / "Library" / "LaunchAgents" / PLIST_NAME,
        # The label and plist are account-global, so the installer lock must
        # not split when SEMAPAD_HOME changes.
        lock_path=login_home / "Library" / "LaunchAgents" / f".{LABEL}.lock",
        log_path=output,
        command=command,
        environment=environment,
        manifest=manifest,
        payload=payload,
    )


def _generated_manifest(value: object, *, account_home: Path) -> bool:
    """Recognize only the exact safe shape emitted by semapad.

    The interpreter and explicit semapad path overrides may differ so an old
    editable-install location remains removable and migratable.
    """
    if type(value) is not dict or set(value) != _MANIFEST_KEYS:
        return False
    program = value.get("Program")
    arguments = value.get("ProgramArguments")
    environment = value.get("EnvironmentVariables")
    output = value.get("StandardOutPath")
    if not isinstance(program, str) or not Path(program).is_absolute():
        return False
    if arguments != [program, "-m", "semapad.cli", "daemon"]:
        return False
    if type(environment) is not dict or set(environment) - _ALLOWED_ENVIRONMENT:
        return False
    if environment.get("HOME") != str(account_home):
        return False
    for key, path in environment.items():
        if not isinstance(key, str) or not isinstance(path, str):
            return False
        if key != "HOME" and not Path(path).is_absolute():
            return False
    runtime_home = environment.get(
        "SEMAPAD_HOME", str(account_home / ".semapad")
    )
    keep_alive = value.get("KeepAlive")
    if type(keep_alive) is not dict \
            or set(keep_alive) != {"SuccessfulExit"} \
            or type(keep_alive.get("SuccessfulExit")) is not bool \
            or keep_alive["SuccessfulExit"] is not False:
        return False
    runtime_path = Path(runtime_home)
    if not runtime_path.is_absolute():
        return False
    try:
        runtime_relative = runtime_path.relative_to(account_home)
    except ValueError:
        return False
    if not runtime_relative.parts or ".." in runtime_relative.parts:
        return False
    expected_log = str(Path(runtime_home) / "logs" / "daemon.log")
    return bool(
        value.get("Label") == LABEL
        and value.get("WorkingDirectory") == "/"
        and type(value.get("Umask")) is str
        and value.get("Umask") == "077"
        and type(value.get("ExitTimeOut")) is int
        and value.get("ExitTimeOut") == 5
        and isinstance(output, str)
        and output == expected_log
        and value.get("StandardErrorPath") == output
    )


def inspect_manifest(spec: Spec) -> ManifestInspection:
    """Classify the on-disk plist without following links or fixing modes."""
    try:
        if not validate_manifest_ancestors(spec):
            return ManifestInspection("missing")
    except LaunchAgentError:
        return ManifestInspection("unsafe")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(spec.plist_path, flags)
    except FileNotFoundError:
        return ManifestInspection("missing")
    except OSError:
        return ManifestInspection("unsafe")
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) \
                or metadata.st_uid != spec.uid \
                or stat.S_IMODE(metadata.st_mode) != 0o600 \
                or metadata.st_nlink != 1 \
                or metadata.st_size < 0 \
                or metadata.st_size > _MAX_PLIST_BYTES:
            return ManifestInspection("unsafe")
        chunks: list[bytes] = []
        remaining = metadata.st_size
        while remaining:
            chunk = os.read(descriptor, min(65536, remaining))
            if not chunk:
                return ManifestInspection("unsafe")
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
    finally:
        os.close(descriptor)
    try:
        value = plistlib.loads(payload)
    except (
        plistlib.InvalidFileException,
        ValueError,
        TypeError,
        OverflowError,
        RecursionError,
    ):
        return ManifestInspection("unsafe")
    status: ManifestStatus
    if value == spec.manifest \
            and _generated_manifest(value, account_home=spec.account_home):
        status = "current"
    elif _generated_manifest(value, account_home=spec.account_home):
        status = "recognized"
    else:
        status = "unknown"
    return ManifestInspection(status, payload, metadata.st_dev, metadata.st_ino)


def _check_directory(path: Path, *, uid: int, exact_mode: int | None = None) -> None:
    try:
        metadata = os.lstat(path)
    except OSError as error:
        raise LaunchAgentError("private directory is unavailable") from error
    mode = stat.S_IMODE(metadata.st_mode)
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode) \
            or metadata.st_uid != uid or mode & 0o022 \
            or (exact_mode is not None and mode != exact_mode):
        raise LaunchAgentError("private directory is unsafe")


def _check_optional_directory(
    path: Path, *, uid: int, exact_mode: int | None = None
) -> bool:
    """Validate an existing directory without following links or creating it."""
    try:
        metadata = os.lstat(path)
    except FileNotFoundError:
        return False
    except OSError as error:
        raise LaunchAgentError("private directory is unavailable") from error
    mode = stat.S_IMODE(metadata.st_mode)
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode) \
            or metadata.st_uid != uid or mode & 0o022 \
            or (exact_mode is not None and mode != exact_mode):
        raise LaunchAgentError("private directory is unsafe")
    return True


def validate_manifest_ancestors(spec: Spec) -> bool:
    """Validate the account LaunchAgents chain without creating any path.

    ``False`` means an ancestor is simply absent, which is equivalent to an
    uninstalled manifest. Existing links, foreign owners, or shared-writable
    directories fail closed.
    """
    _check_directory(spec.account_home, uid=spec.uid)
    library = spec.account_home / "Library"
    if not _check_optional_directory(library, uid=spec.uid):
        return False
    return _check_optional_directory(spec.plist_path.parent, uid=spec.uid)


def _check_home_ancestors(spec: Spec) -> None:
    """Reject links and shared-writable components below the account home."""
    current = spec.account_home
    _check_directory(current, uid=spec.uid)
    runtime_home = Path(spec.environment.get(
        "SEMAPAD_HOME", str(spec.account_home / ".semapad")
    ))
    relative = runtime_home.relative_to(spec.account_home)
    for component in relative.parts:
        current = current / component
        if not _check_optional_directory(current, uid=spec.uid):
            break


def ensure_lock_directory(spec: Spec) -> None:
    _check_directory(spec.account_home, uid=spec.uid)
    library = spec.account_home / "Library"
    _check_directory(library, uid=spec.uid)
    agents = spec.plist_path.parent
    try:
        agents.mkdir(mode=0o700)
    except FileExistsError:
        pass
    except OSError as error:
        raise LaunchAgentError("LaunchAgents directory is unavailable") from error
    _check_directory(agents, uid=spec.uid)


def _harden_owned_directory(path: Path, *, uid: int, what: str) -> None:
    """mkdir 0700, or harden a pre-existing 0755 one after inode validation.

    Manual runs (nohup semapad daemon/ui) create these with the default
    umask; they are owner-controlled and non-writable by others, so install
    may tighten them in place instead of rejecting an otherwise safe setup.
    """
    try:
        path.mkdir(mode=0o700, exist_ok=True)
    except OSError as error:
        raise LaunchAgentError(f"{what} is unavailable") from error
    _check_directory(path, uid=uid)
    before = os.lstat(path)
    try:
        os.chmod(path, 0o700, follow_symlinks=False)
    except OSError as error:
        raise LaunchAgentError(f"could not harden {what}") from error
    after = os.lstat(path)
    if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
        raise LaunchAgentError(f"{what} changed while hardening")
    _check_directory(path, uid=uid, exact_mode=0o700)


def _ensure_runtime_directories(spec: Spec) -> None:
    _check_home_ancestors(spec)
    runtime_home = Path(spec.environment.get(
        "SEMAPAD_HOME", str(spec.account_home / ".semapad")
    ))
    _harden_owned_directory(runtime_home, uid=spec.uid, what="semapad home")
    _harden_owned_directory(runtime_home / "runtime", uid=spec.uid,
                            what="semapad runtime directory")
    _harden_owned_directory(runtime_home / "logs", uid=spec.uid,
                            what="semapad log directory")


def ensure_install_directories(spec: Spec) -> None:
    """Create only semapad-owned directories; never chmod shared Library."""
    ensure_lock_directory(spec)
    _ensure_runtime_directories(spec)
    for directory in (spec.log_path.parent,):
        try:
            directory.mkdir(mode=0o700, exist_ok=True)
        except OSError as error:
            raise LaunchAgentError("semapad private directory is unavailable") from error
        _check_directory(directory, uid=spec.uid, exact_mode=0o700)


def validate_program(spec: Spec) -> None:
    """Keep the lexical venv path but require its current target to be runnable."""
    try:
        metadata = os.stat(spec.command[0])
    except OSError as error:
        raise LaunchAgentError("LaunchAgent interpreter is unavailable") from error
    if not stat.S_ISREG(metadata.st_mode) \
            or stat.S_IMODE(metadata.st_mode) & 0o022 \
            or not os.access(spec.command[0], os.X_OK):
        raise LaunchAgentError("LaunchAgent interpreter is not executable")


def ensure_private_log(spec: Spec) -> None:
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) \
        | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(spec.log_path, flags, 0o600)
    except OSError as error:
        raise LaunchAgentError("private daemon log is unavailable") from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != spec.uid \
                or metadata.st_nlink != 1:
            raise LaunchAgentError("private daemon log is unsafe")
        if stat.S_IMODE(metadata.st_mode) != 0o600:
            # A manual `nohup ... >> daemon.log` creates this with the default
            # umask. Same owned-inode rationale as the directories: tighten in
            # place on the descriptor we already validated.
            try:
                os.fchmod(descriptor, 0o600)
            except OSError as error:
                raise LaunchAgentError("could not harden the daemon log") from error
            if stat.S_IMODE(os.fstat(descriptor).st_mode) != 0o600:
                raise LaunchAgentError("private daemon log is unsafe")
    finally:
        os.close(descriptor)


def atomic_write_manifest(spec: Spec, payload: bytes | None = None) -> None:
    """Atomically install canonical or previously captured recognized bytes."""
    data = spec.payload if payload is None else payload
    if len(data) > _MAX_PLIST_BYTES:
        raise LaunchAgentError("LaunchAgent plist is too large")
    if not validate_manifest_ancestors(spec):
        raise LaunchAgentError("LaunchAgent directory is unavailable")
    if spec.plist_path.is_symlink():
        raise LaunchAgentError("refusing to replace a linked LaunchAgent plist")
    descriptor = -1
    temporary: Path | None = None
    try:
        descriptor, name = tempfile.mkstemp(
            prefix=f".{PLIST_NAME}.", dir=spec.plist_path.parent
        )
        temporary = Path(name)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, spec.plist_path)
        temporary = None
        directory_fd = os.open(
            spec.plist_path.parent,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError as error:
        raise LaunchAgentError("could not atomically install LaunchAgent plist") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def remove_manifest(spec: Spec, inspection: ManifestInspection) -> None:
    """Unlink exactly the inspected, recognized inode."""
    if not inspection.owned or inspection.device is None or inspection.inode is None:
        raise LaunchAgentError("LaunchAgent plist is not owned by semapad")
    if not validate_manifest_ancestors(spec):
        raise LaunchAgentError("LaunchAgent directory is unavailable")
    try:
        metadata = os.lstat(spec.plist_path)
    except OSError as error:
        raise LaunchAgentError("LaunchAgent plist changed before removal") from error
    if stat.S_ISLNK(metadata.st_mode) or metadata.st_dev != inspection.device \
            or metadata.st_ino != inspection.inode:
        raise LaunchAgentError("LaunchAgent plist changed before removal")
    try:
        os.unlink(spec.plist_path)
        directory_fd = os.open(
            spec.plist_path.parent,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError as error:
        raise LaunchAgentError("could not remove LaunchAgent plist") from error


class InstallLock:
    """Serialize install/uninstall without trusting a pre-existing lock file."""

    def __init__(self, spec: Spec) -> None:
        self.spec = spec
        self.descriptor = -1

    def __enter__(self) -> "InstallLock":
        if not validate_manifest_ancestors(self.spec):
            raise LaunchAgentError("LaunchAgent directory is unavailable")
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) \
            | getattr(os, "O_NOFOLLOW", 0)
        try:
            self.descriptor = os.open(self.spec.lock_path, flags, 0o600)
            metadata = os.fstat(self.descriptor)
            if not stat.S_ISREG(metadata.st_mode) \
                    or metadata.st_uid != self.spec.uid \
                    or stat.S_IMODE(metadata.st_mode) != 0o600 \
                    or metadata.st_nlink != 1:
                raise LaunchAgentError("autostart lock is unsafe")
            fcntl.flock(self.descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            if self.descriptor >= 0:
                os.close(self.descriptor)
                self.descriptor = -1
            raise
        return self

    def __exit__(self, *_exc: object) -> None:
        if self.descriptor >= 0:
            try:
                fcntl.flock(self.descriptor, fcntl.LOCK_UN)
            finally:
                os.close(self.descriptor)
                self.descriptor = -1


Runner = Callable[..., subprocess.CompletedProcess[bytes]]


class Controller:
    """Tiny launchctl adapter. Only exit status is consumed, never print output."""

    def __init__(self, spec: Spec, *, runner: Runner = subprocess.run) -> None:
        self.spec = spec
        self.runner = runner

    def _call(self, *arguments: str, timeout: float = 10.0) -> bool:
        try:
            completed = self.runner(
                [LAUNCHCTL, *arguments],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=timeout,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise LaunchAgentError("launchctl operation failed") from error
        return getattr(completed, "returncode", 1) == 0

    def domain_available(self) -> bool:
        return self._call("print", self.spec.domain)

    def loaded(self) -> bool:
        if not self.domain_available():
            raise LaunchAgentError("GUI launchd domain is unavailable")
        return self._call("print", self.spec.target)

    def bootstrap(self) -> None:
        if not self._call("bootstrap", self.spec.domain, str(self.spec.plist_path)):
            raise LaunchAgentError("could not bootstrap LaunchAgent")

    def bootout(self) -> None:
        if not self._call("bootout", self.spec.target):
            raise LaunchAgentError("could not boot out LaunchAgent")

    def kickstart(self) -> None:
        if not self._call("kickstart", self.spec.target):
            raise LaunchAgentError("could not start LaunchAgent")
