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
  race-free.
- A *subagent* holds no handle (the CLI appends its transcript and closes per
  event), but the transcript grows while the agent works: a recent mtime is
  work. Approximate -- an agent inside one long silent tool call can outlive
  the window -- but its own bash calls then hold handles of their own.

The directory layout is private schema; this module only ever *reads*, and
every failure degrades to "not busy", which is exactly the pre-#17 behaviour.
"""
from __future__ import annotations

import ctypes
import os
import time
from collections.abc import Callable, Iterable
from pathlib import Path

TASKS_ROOT = Path(f"/tmp/claude-{os.getuid()}")

#: How recently a handle-less output file must have grown to count as work.
#: ponytail: fixed window -- a subagent quiet longer than this (one long
#: non-bash tool call) reads idle until its next transcript append; widen or
#: make configurable only if that is ever observed to matter.
AGENT_WINDOW_SECONDS = 60.0

_PROC_ALL_PIDS = 1
_libproc: ctypes.CDLL | None | bool = None   # False = load failed, stay quiet


def _has_open_handle(path: Path) -> bool:
    """Does any process hold *path* open? Never raises; unknown is False."""
    global _libproc
    try:
        if _libproc is None:
            _libproc = ctypes.CDLL("/usr/lib/libproc.dylib")
        if _libproc is False:
            return False
        buffer = (ctypes.c_int * 4096)()
        used = _libproc.proc_listpidspath(
            _PROC_ALL_PIDS, 0, os.fsencode(path), 0,
            buffer, ctypes.sizeof(buffer))
        return used > 0 and any(buffer[:used // ctypes.sizeof(ctypes.c_int)])
    except OSError:
        _libproc = False
        return False
    except Exception:
        return False


def scan(session_ids: Iterable[str], now: float, *, root: Path | None = None,
         has_open_handle: Callable[[Path], bool] | None = None,
         ) -> frozenset[str]:
    """Session ids among *session_ids* that have a running background task.

    A session id appears under exactly one munged-cwd directory, but the
    munging rule is private -- so every project directory is searched rather
    than guessed (spec principle 5).
    """
    wanted = set(session_ids)
    root = TASKS_ROOT if root is None else root
    check = _has_open_handle if has_open_handle is None else has_open_handle
    if not wanted:
        return frozenset()
    try:
        project_dirs = [p for p in root.iterdir() if p.is_dir()]
    except OSError:
        return frozenset()

    busy: set[str] = set()
    for session_id in wanted:
        for project in project_dirs:
            tasks_dir = project / session_id / "tasks"
            try:
                entries = list(tasks_dir.iterdir())
            except OSError:
                continue
            for path in entries:
                if path.suffix != ".output":
                    continue
                try:
                    held = check(path)
                except Exception:
                    held = False
                if held:
                    busy.add(session_id)
                    break
                try:
                    age = now - path.stat().st_mtime
                except OSError:
                    continue
                if 0.0 <= age <= AGENT_WINDOW_SECONDS:
                    busy.add(session_id)
                    break
            if session_id in busy:
                break
    return frozenset(busy)


def demo() -> None:
    """Smallest self-check: a file this process holds open reads busy."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        directory = root / "-proj" / "sid" / "tasks"
        directory.mkdir(parents=True)
        path = directory / "b1.output"
        path.write_text("")
        old = time.time() - 3600
        os.utime(path, (old, old))
        assert scan({"sid"}, time.time(), root=root) == frozenset()
        with open(path, "a"):
            assert scan({"sid"}, time.time(), root=root) == {"sid"}
    print("tasks.demo: ok")


if __name__ == "__main__":
    demo()
