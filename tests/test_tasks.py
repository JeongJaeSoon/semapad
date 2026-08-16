"""Running-background-task detection (#17).

The fixture layout mirrors the real one: <root>/<munged-cwd>/<session-id>/
tasks/<task-id>.output. Sessions and munged directories are many-to-many on
disk, so scan() must find a session id under any project directory.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

from semapad.sources import tasks


def _output(root: Path, sid: str, name: str = "b0000001.output",
            project: str = "-Users-x-proj") -> Path:
    directory = root / project / sid / "tasks"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text("")
    return path


def test_held_open_output_marks_session_busy(tmp_path):
    # The test process itself holds the file open, exactly like the spawned
    # shell of a real background bash task -- exercises the real libproc path.
    path = _output(tmp_path, "sid-1")
    old = time.time() - 3600.0
    os.utime(path, (old, old))   # mtime alone would say "not busy"
    with open(path, "a"):
        assert tasks.scan({"sid-1"}, time.time(), root=tmp_path) == {"sid-1"}


def test_lingering_closed_output_is_not_busy(tmp_path):
    # Completed task files are never deleted (measured 2026-08-17): existence
    # alone must not light the key.
    path = _output(tmp_path, "sid-1")
    old = time.time() - 3600.0
    os.utime(path, (old, old))
    assert tasks.scan({"sid-1"}, time.time(), root=tmp_path) == frozenset()


def test_recent_mtime_is_busy_without_a_handle(tmp_path):
    # Subagent transcripts are appended-and-closed per event: no handle, but
    # a recent write means the agent is alive.
    _output(tmp_path, "sid-1", name="a0000001.output")
    now = time.time()
    assert tasks.scan({"sid-1"}, now, root=tmp_path,
                      has_open_handle=lambda p: False) == {"sid-1"}


def test_future_mtime_is_not_busy(tmp_path):
    # Clock skew must not invent work.
    path = _output(tmp_path, "sid-1")
    future = time.time() + 3600.0
    os.utime(path, (future, future))
    assert tasks.scan({"sid-1"}, time.time(), root=tmp_path,
                      has_open_handle=lambda p: False) == frozenset()


def test_missing_root_and_unknown_session_are_not_busy(tmp_path):
    assert tasks.scan({"sid-1"}, time.time(),
                      root=tmp_path / "absent") == frozenset()
    _output(tmp_path, "sid-1")
    assert tasks.scan({"other"}, time.time(), root=tmp_path,
                      has_open_handle=lambda p: False) == frozenset()


def test_non_output_files_are_ignored(tmp_path):
    _output(tmp_path, "sid-1", name="notes.txt")
    assert tasks.scan({"sid-1"}, time.time(), root=tmp_path,
                      has_open_handle=lambda p: True) == frozenset()


def test_handle_checker_failure_degrades_to_mtime(tmp_path):
    path = _output(tmp_path, "sid-1")
    old = time.time() - 3600.0
    os.utime(path, (old, old))

    def broken(p: Path) -> bool:
        raise OSError("libproc unavailable")

    assert tasks.scan({"sid-1"}, time.time(), root=tmp_path,
                      has_open_handle=broken) == frozenset()
