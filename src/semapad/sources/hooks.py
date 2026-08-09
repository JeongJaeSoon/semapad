"""Hook classification + session record store (ported: paneglow hook.py + store.py).

Store half -- hooks fire overlapping and short-lived, so writes must be atomic.
Writing to a temp file, fsyncing it, then renaming within the same directory
means a reader either sees a whole file or does not see it at all -- never a
half-written one.

Rename alone is not enough for the writers, though. "Is my rev newer" and the
rename are two steps, and two hooks can both pass the check before either lands
-- then completion order decides, not rev order. The loss that matters is Stop
(done) being overwritten by a PostToolUse (working) that read stale: Stop is the
last event of a turn, so nothing corrects it and the session stays blue. Hence the
lock around check-and-write.

Hook half -- hook input is an external trust boundary. Unknown or malformed
events must not change state, and processing failures must never interrupt a
Claude turn.
"""
from __future__ import annotations

import fcntl
import json
import os
import tempfile
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TextIO

from semapad.model import AgentState

#: ponytail: one lock for the whole store, not one per session. Writes are a few
#: hundred bytes and only happen on state changes, so contention is not a concern
#: at this scale. Split it per session_id if that ever stops being true.
_LOCK_NAME = ".write.lock"


@dataclass(frozen=True)
class SessionRecord:
    session_id: str
    cwd: str
    state: AgentState
    rev: int
    updated_at: float


def _checked_id(session_id: str) -> str:
    """Refuse anything that would not stay a single file inside the store.

    session_id arrives in a hook's stdin JSON and ends up in os.replace() and
    unlink(). Claude Code sends a uuid, but a field read off stdin becoming a
    filesystem path is a trust boundary: '../escaped' writes outside the store.
    """
    if not session_id or session_id in (".", "..") or "/" in session_id \
            or "\\" in session_id or "\x00" in session_id:
        raise ValueError(f"unusable session_id: {session_id!r}")
    return session_id


def _path(root: Path, session_id: str) -> Path:
    return root / f"{_checked_id(session_id)}.json"


def _load(path: Path) -> SessionRecord | None:
    try:
        raw = json.loads(path.read_text())
        # The filename is the authority. Letting the contents declare their own
        # id turns that field into a pointer at another file: a record claiming
        # "important" makes prune() unlink important.json instead of this one.
        if raw["session_id"] != path.stem:
            return None
        _checked_id(path.stem)
        return SessionRecord(
            session_id=path.stem, cwd=raw["cwd"],
            state=AgentState(raw["state"]), rev=int(raw["rev"]),
            updated_at=float(raw["updated_at"]),
        )
    except Exception:
        # A broken file is normal -- it may be mid-write. Read again next tick.
        return None


@contextmanager
def _write_lock(root: Path):
    """Serialise check-and-write across hook processes. flock is released when
    the fd closes, so a hook that dies mid-write cannot wedge the store."""
    with open(root / _LOCK_NAME, "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def write(record: SessionRecord, root: Path) -> bool:
    """Write atomically if rev is newer than what is stored. Otherwise False."""
    root.mkdir(parents=True, exist_ok=True)
    target = _path(root, record.session_id)

    with _write_lock(root):
        existing = _load(target)
        if existing is not None and record.rev <= existing.rev:
            return False

        payload = asdict(record) | {"state": record.state.value}
        fd, tmp = tempfile.mkstemp(dir=root, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(payload, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, target)   # atomic: same directory
        except Exception:
            Path(tmp).unlink(missing_ok=True)
            raise
    return True


def read_all(root: Path) -> list[SessionRecord]:
    if not root.exists():
        return []
    out = []
    for p in sorted(root.glob("*.json")):
        rec = _load(p)
        if rec is not None:
            out.append(rec)
    return out


def prune(root: Path, live_ids: set[str] | None,
          ttl_seconds: float, now: float) -> int:
    """Delete records for dead sessions.

    A known ``live_ids`` set is authoritative, including an empty set. TTL is
    only the fallback for a session scan that could not produce authoritative
    results and therefore passes ``None``.

    Runs under the write lock: otherwise a hook can replace a record between the
    read and the unlink, and the brand new state gets deleted.
    """
    if not root.exists():
        return 0

    with _write_lock(root):
        records = read_all(root)
        removed = 0
        for rec in records:
            dead = (rec.session_id not in live_ids) if live_ids is not None \
                else (now - rec.updated_at > ttl_seconds)
            if dead:
                _path(root, rec.session_id).unlink(missing_ok=True)
                removed += 1
    return removed


# --- hook classification (ported hook.py, store.* calls now module-local) ---

_WORKING_EVENTS = frozenset(
    {"UserPromptSubmit", "PreToolUse", "PostToolUse", "PreCompact"}
)
_ERROR_EVENTS = frozenset(
    {"StopFailure", "PostToolUseFailure"}
)
# A denylist would silently turn newly introduced notification types into a
# human-action state. Only the two observed interactive types are actionable.
_WAITING_NOTIFICATIONS = frozenset({"permission_prompt", "agent_needs_input"})


def classify(event: object) -> AgentState | None:
    """Return the state represented by *event*, or ``None`` for no change."""
    if not isinstance(event, dict) or "agent_type" in event:
        return None

    name = event.get("hook_event_name")
    if not isinstance(name, str):
        return None
    if name == "SessionStart":
        return AgentState.IDLE
    if name in _WORKING_EVENTS:
        # AskUserQuestion blocks on a dialog the moment PreToolUse fires, so
        # it is a human-input state, not work. Elicitations that happen inside
        # other tool calls emit no hook and cannot be classified here.
        if name == "PreToolUse" and event.get("tool_name") == "AskUserQuestion":
            return AgentState.WAITING
        return AgentState.WORKING
    if name == "Stop":
        return AgentState.DONE
    if name == "PermissionDenied":
        # Spec §11.5 (2026-08-09): classifier denials are part of progress under
        # auto permission mode -- WORKING, not ERROR. Real failures stay ERROR.
        return AgentState.WORKING
    if name in _ERROR_EVENTS:
        return AgentState.ERROR
    if name == "Notification":
        notification_type = event.get("notification_type")
        if (
            isinstance(notification_type, str)
            and notification_type in _WAITING_NOTIFICATIONS
        ):
            return AgentState.WAITING
    return None


def record_from(event: object, rev: int, now: float) -> SessionRecord | None:
    """Build a record from a state-changing top-level session event.

    Claude subagent events can reuse ordinary hook event names while carrying a
    separate session id. Presence of ``agent_type`` is therefore authoritative,
    regardless of the field's value.
    """
    if not isinstance(event, dict) or "agent_type" in event:
        return None

    session_id = event.get("session_id")
    if not isinstance(session_id, str) or not session_id.strip():
        return None

    state = classify(event)
    if state is None:
        return None

    cwd = event.get("cwd")
    if not isinstance(cwd, str):
        cwd = ""
    return SessionRecord(
        session_id=session_id,
        cwd=cwd,
        state=state,
        rev=rev,
        updated_at=now,
    )


def _idle_promotion(event: object, state_dir: Path, rev: int,
                    now: float) -> SessionRecord | None:
    """Turn an idle_prompt into waiting, but only for a working session.

    Elicitations inside tool calls (credential prompts, browser pickers) emit
    no hook of their own, so a blocked session sits at working until Claude's
    60s idle notification arrives. A completed turn already moved to done via
    Stop, which keeps finished sessions from lighting up as waiting.
    """
    if not isinstance(event, dict) or "agent_type" in event:
        return None
    if event.get("hook_event_name") != "Notification":
        return None
    if event.get("notification_type") != "idle_prompt":
        return None
    session_id = event.get("session_id")
    if not isinstance(session_id, str) or not session_id.strip():
        return None

    for current in read_all(state_dir):
        if current.session_id == session_id:
            if current.state is not AgentState.WORKING:
                return None
            return SessionRecord(
                session_id=session_id,
                cwd=current.cwd,
                state=AgentState.WAITING,
                rev=rev,
                updated_at=now,
            )
    return None


def run(stdin: TextIO, state_dir: Path) -> int:
    """Consume one stdin JSON event and persist it, always returning success.

    The hook is on Claude's critical path. Input, classification, clock, path,
    and storage failures are deliberately fail-closed and produce no output.
    """
    try:
        event = json.load(stdin)
        record = record_from(event, rev=time.time_ns(), now=time.time())
        if record is None:
            record = _idle_promotion(
                event, state_dir, rev=time.time_ns(), now=time.time()
            )
        if record is not None:
            write(record, state_dir)
    except Exception:
        pass
    return 0
