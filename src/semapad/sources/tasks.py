"""Detect running background tasks from Claude's task output files (#17).

Background tasks (bash commands, subagents) fire no hooks, so a session can
be hard at work while its hook record says done or idle. Claude writes one
output file per task under ``/tmp/claude-<uid>/<munged-cwd>/<session-id>/
tasks/<task-id>.output`` -- but the file is NOT removed when the task ends
(measured 2026-08-17), so existence alone proves nothing. Two liveness
signals do:

- A background *bash* task's shell keeps its output file open for write for
  the task's whole life. Asking the kernel who holds the file open
  (``libproc.proc_listpidspath`` via ctypes -- stdlib only) is exact and
  race-free -- but expensive: ~150 ms per call on a busy Mac (measured,
  1786 processes), because the kernel walks every process's fd table.
  ``TaskScanner`` therefore caches per-file verdicts so the steady state
  costs stats, not kernel walks.
- A *subagent* holds no handle (the CLI appends its transcript and closes
  per event), but the transcript grows while the agent works: a recent
  mtime is work. Approximate in both directions -- an agent inside one long
  silent tool call can outlive the window, and a just-finished task's final
  write keeps the window open a little longer -- but an agent's own bash
  calls hold handles, and a file once *seen* held and then closed is done
  for good, which caps the false-busy tail to tasks too short to be seen.

The directory layout is private schema; this module only ever *reads*, and
every failure degrades to "not busy", which is exactly the pre-#17
behaviour. Like absent hooks, an absent or unreadable root is not a fault
(spec §4 principle 4): the pad works without this enrichment.
"""
from __future__ import annotations

import ctypes
import os
from collections.abc import Callable, Iterable
from pathlib import Path

from semapad.sources.hooks import _checked_id

TASKS_ROOT = Path(f"/tmp/claude-{os.getuid()}")

#: How recently a handle-less output file must have grown to count as work.
#: ponytail: fixed window -- a subagent quiet longer than this reads idle
#: until its next transcript append, and a task too short to be seen held
#: reads busy for this long after finishing; tune only if observed to matter.
AGENT_WINDOW_SECONDS = 60.0

#: Tolerance for mtimes slightly ahead of the caller's ``now`` -- the tick
#: timestamp is taken before the scan runs, so an actively-writing file can
#: legitimately be "in the future" by the tick's own duration.
_FUTURE_TOLERANCE_SECONDS = 5.0

_PROC_ALL_PIDS = 1

try:
    _LISTPIDSPATH = ctypes.CDLL("/usr/lib/libproc.dylib").proc_listpidspath
except (OSError, AttributeError):        # non-Darwin or exotic build
    _LISTPIDSPATH = None


def _has_open_handle(path: Path) -> bool:
    """Does any process hold *path* open? Never raises; unknown is False."""
    if _LISTPIDSPATH is None:
        return False
    try:
        buffer = (ctypes.c_int * 4096)()
        used = _LISTPIDSPATH(_PROC_ALL_PIDS, 0, os.fsencode(path), 0,
                             buffer, ctypes.sizeof(buffer))
        return used > 0
    except Exception:
        return False


class TaskScanner:
    """Stateful scan: remembers per-file verdicts across polls.

    Task ids are unique and a finished task's file is never written or
    reopened again, so a verdict of "was held, now closed" (done) or "closed
    and stale" (quiet) is permanent -- only a fresh mtime can revive a quiet
    file (a subagent transcript resuming). That bounds the expensive kernel
    walk to newly appeared files plus one re-check per held file per
    ``HANDLE_TTL_SECONDS``.
    """

    #: How long a confirmed-held file stays busy before re-asking the kernel.
    HANDLE_TTL_SECONDS = 5.0
    #: ponytail: at most this many ~150 ms kernel walks per scan; unresolved
    #: files just stay "not busy" until a later scan reaches them. Raise if
    #: first-sighting latency is ever felt.
    MAX_HANDLE_CHECKS_PER_SCAN = 2

    def __init__(self, root: Path | None = None,
                 has_open_handle: Callable[[Path], bool] | None = None) -> None:
        self._root = TASKS_ROOT if root is None else root
        self._check = _has_open_handle if has_open_handle is None \
            else has_open_handle
        self._held_at: dict[str, float] = {}   # confirmed held, last asked at
        self._done: set[str] = set()           # held once, then closed: over
        self._quiet: set[str] = set()          # never held, stale: skip kernel

    def scan(self, session_ids: Iterable[str], now: float) -> frozenset[str]:
        """Session ids among *session_ids* with a running background task.

        Never raises: the callers treat busy-ness as enrichment, and this is
        the one place that enforces it.
        """
        try:
            return self._scan(session_ids, now)
        except Exception:
            return frozenset()

    def _scan(self, session_ids: Iterable[str], now: float) -> frozenset[str]:
        wanted = set()
        for session_id in session_ids:
            # A session id becomes a path component below: same trust
            # boundary rule as the hook store.
            try:
                wanted.add(_checked_id(session_id))
            except ValueError:
                continue
        if not wanted:
            return frozenset()
        try:
            project_dirs = [p for p in self._root.iterdir()
                            if not p.is_symlink() and p.is_dir()]
        except OSError:
            return frozenset()

        busy: set[str] = set()
        self._budget = self.MAX_HANDLE_CHECKS_PER_SCAN
        for project in project_dirs:
            remaining = wanted - busy
            if not remaining:
                break
            try:
                session_dirs = [p for p in project.iterdir()
                                if p.name in remaining]
            except OSError:
                continue
            for session_dir in session_dirs:
                if self._session_busy(session_dir / "tasks", now):
                    busy.add(session_dir.name)
        return frozenset(busy)

    def _session_busy(self, tasks_dir: Path, now: float) -> bool:
        try:
            entries = sorted(tasks_dir.iterdir())
        except OSError:
            return False
        for path in entries:
            if path.suffix != ".output" or path.is_symlink() \
                    or not path.is_file():
                continue
            key = str(path)
            if key in self._done:
                continue
            if key in self._held_at:
                # A file once seen held is a bash task: the handle is the
                # authority, and its closure means finished -- the fresh
                # mtime of the task's final write must not re-light it.
                if now - self._held_at[key] < self.HANDLE_TTL_SECONDS:
                    return True
                if self._budget <= 0:
                    return True          # held until proven closed
                self._budget -= 1
                if self._ask(path):
                    self._held_at[key] = now
                    return True
                del self._held_at[key]
                self._done.add(key)
                continue
            try:
                age = now - path.stat().st_mtime
            except OSError:
                continue
            if -_FUTURE_TOLERANCE_SECONDS <= age <= AGENT_WINDOW_SECONDS:
                return True
            if key in self._quiet:
                continue
            if self._budget <= 0:
                continue
            self._budget -= 1
            if self._ask(path):
                self._held_at[key] = now
                return True
            self._quiet.add(key)         # only a fresh mtime revives it
        return False

    def _ask(self, path: Path) -> bool:
        try:
            return self._check(path)
        except Exception:
            return False
