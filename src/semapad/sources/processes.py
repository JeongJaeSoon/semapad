"""Discover live interactive Claude sessions from the private session files.

The on-disk schema is private and may be partially written or change without
notice. A scan therefore returns both usable results and whether the result is
safe to use as an authoritative prune set.
"""
from __future__ import annotations

import json
import math
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SESSIONS_DIR = Path.home() / ".claude" / "sessions"
_PID_MAX = (1 << 31) - 1  # Darwin pid_t is a signed 32-bit integer.


@dataclass(frozen=True)
class Session:
    """One live interactive Claude session.

    ``started_at`` is always epoch seconds even though Claude currently stores
    ``startedAt`` as epoch milliseconds.
    """

    session_id: str
    cwd: str
    name: str
    entrypoint: str
    pid: int
    started_at: float


@dataclass(frozen=True)
class SessionSnapshot:
    """A session scan plus whether its ID set is safe for immediate pruning."""

    sessions: tuple[Session, ...]
    authoritative: bool
    diagnostics: tuple[str, ...]


@dataclass(frozen=True)
class _ParseResult:
    session: Session | None
    valid: bool


def _optional_string(raw: dict[str, Any], key: str) -> str:
    value = raw.get(key)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string")
    return value


def _parse(path: Path) -> _ParseResult:
    try:
        raw = json.loads(path.read_text())
        if not isinstance(raw, dict):
            raise ValueError("session file must be an object")

        kind = raw.get("kind")
        if kind == "bg":
            return _ParseResult(None, True)
        if kind != "interactive":
            raise ValueError("unknown session kind")

        session_id = raw["sessionId"]
        cwd = raw["cwd"]
        pid = raw["pid"]
        started_ms = raw["startedAt"]
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("sessionId must be a non-empty string")
        if not isinstance(cwd, str):
            raise ValueError("cwd must be a string")
        if (isinstance(pid, bool) or not isinstance(pid, int)
                or not 0 < pid <= _PID_MAX):
            raise ValueError("pid must fit a positive pid_t")
        if isinstance(started_ms, bool) or not isinstance(started_ms, (int, float)):
            raise ValueError("startedAt must be a number")
        started_ms = float(started_ms)
        if not math.isfinite(started_ms) or started_ms < 0:
            raise ValueError("startedAt must be finite and non-negative")

        return _ParseResult(Session(
            session_id=session_id,
            cwd=cwd,
            name=_optional_string(raw, "name"),
            entrypoint=_optional_string(raw, "entrypoint"),
            pid=pid,
            started_at=started_ms / 1000.0,
        ), True)
    except (OSError, OverflowError, json.JSONDecodeError, KeyError, TypeError,
            ValueError):
        return _ParseResult(None, False)


def scan(root: Path | None = None,
         alive: Callable[[int, int], None] = os.kill) -> SessionSnapshot:
    """Return live sessions and whether their ID set is authoritative.

    ``ProcessLookupError`` proves a PID is dead. ``PermissionError`` proves the
    PID exists but cannot be signalled, so that session stays live. Other OS
    errors are uncertain: the session is kept, but the snapshot cannot drive an
    immediate prune.
    """
    root = SESSIONS_DIR if root is None else root
    if not root.is_dir():
        return SessionSnapshot((), False, ("session directory missing",))

    try:
        # Path.glob() suppresses directory-scanning OSErrors on modern Python,
        # which can turn an unreadable directory into an authoritative empty
        # result. iterdir() keeps that trust signal observable.
        paths = sorted(
            (path for path in root.iterdir() if path.name.endswith(".json")),
            key=lambda path: path.name,
        )
    except OSError:
        return SessionSnapshot((), False, ("session directory unreadable",))

    invalid_files = 0
    permission_checks = 0
    uncertain_checks = 0
    duplicate_ids = 0
    candidates: dict[str, Session] = {}

    for path in paths:
        parsed = _parse(path)
        if not parsed.valid:
            invalid_files += 1
            continue
        session = parsed.session
        if session is None:
            continue

        try:
            alive(session.pid, 0)
        except ProcessLookupError:
            continue
        except PermissionError:
            permission_checks += 1
        except OSError:
            uncertain_checks += 1

        previous = candidates.get(session.session_id)
        if previous is not None:
            duplicate_ids += 1
            if (session.started_at, session.pid) <= (previous.started_at, previous.pid):
                continue
        candidates[session.session_id] = session

    ordered = tuple(sorted(
        candidates.values(),
        key=lambda session: (-session.started_at, session.session_id, session.pid),
    ))
    diagnostics: list[str] = []
    if invalid_files:
        diagnostics.append(f"{invalid_files} invalid session file(s)")
    if permission_checks:
        diagnostics.append(
            f"{permission_checks} PID permission check(s) kept as live")
    if uncertain_checks:
        diagnostics.append(
            f"{uncertain_checks} PID check error(s) kept as live")
    if duplicate_ids:
        diagnostics.append(f"{duplicate_ids} duplicate session id(s) collapsed")

    authoritative = invalid_files == 0 and uncertain_checks == 0
    return SessionSnapshot(ordered, authoritative, tuple(diagnostics))
