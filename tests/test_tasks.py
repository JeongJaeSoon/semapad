"""Running-background-task detection (#17).

The fixture layout mirrors the real one: <root>/<munged-cwd>/<session-id>/
tasks/<task-id>.output. Sessions and munged directories are many-to-many on
disk, so the scanner must find a session id under any project directory.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

from semapad.sources import tasks


def _output(root: Path, sid: str, name: str = "b0000001.output",
            project: str = "-Users-x-proj", age_seconds: float = 3600.0) -> Path:
    directory = root / project / sid / "tasks"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text("")
    if age_seconds:
        old = time.time() - age_seconds
        os.utime(path, (old, old))
    return path


def _scanner(root: Path, check=None) -> tasks.TaskScanner:
    return tasks.TaskScanner(root=root, has_open_handle=check)


def test_held_open_output_marks_session_busy(tmp_path):
    # The test process itself holds the file open, exactly like the spawned
    # shell of a real background bash task -- exercises the real libproc path.
    path = _output(tmp_path, "sid-1")   # stale mtime: only the handle says busy
    with open(path, "a"):
        assert _scanner(tmp_path).scan({"sid-1"}, time.time()) == {"sid-1"}


def test_lingering_closed_output_is_not_busy(tmp_path):
    # Completed task files are never deleted (measured 2026-08-17): existence
    # alone must not light the key.
    _output(tmp_path, "sid-1")
    assert _scanner(tmp_path).scan({"sid-1"}, time.time()) == frozenset()


def test_recent_mtime_is_busy_without_a_handle(tmp_path):
    # Subagent transcripts are appended-and-closed per event: no handle, but
    # a recent write means the agent is alive.
    _output(tmp_path, "sid-1", name="a0000001.output", age_seconds=0.0)
    scanner = _scanner(tmp_path, check=lambda p: False)
    assert scanner.scan({"sid-1"}, time.time()) == {"sid-1"}


def test_slightly_future_mtime_is_still_busy(tmp_path):
    # The tick's `now` predates the scan; a file written in between is ahead
    # of it and must not flicker to idle.
    path = _output(tmp_path, "sid-1", age_seconds=0.0)
    ahead = time.time() + 2.0
    os.utime(path, (ahead, ahead))
    scanner = _scanner(tmp_path, check=lambda p: False)
    assert scanner.scan({"sid-1"}, time.time()) == {"sid-1"}


def test_far_future_mtime_is_not_busy(tmp_path):
    # Real clock skew must not invent work.
    path = _output(tmp_path, "sid-1", age_seconds=0.0)
    future = time.time() + 3600.0
    os.utime(path, (future, future))
    scanner = _scanner(tmp_path, check=lambda p: False)
    assert scanner.scan({"sid-1"}, time.time()) == frozenset()


def test_missing_root_and_unknown_session_are_not_busy(tmp_path):
    assert _scanner(tmp_path / "absent").scan(
        {"sid-1"}, time.time()) == frozenset()
    _output(tmp_path, "sid-1")
    scanner = _scanner(tmp_path, check=lambda p: False)
    assert scanner.scan({"other"}, time.time()) == frozenset()


def test_non_output_files_are_ignored(tmp_path):
    _output(tmp_path, "sid-1", name="notes.txt")
    scanner = _scanner(tmp_path, check=lambda p: True)
    assert scanner.scan({"sid-1"}, time.time()) == frozenset()


def test_traversal_session_id_is_refused(tmp_path):
    # A session id becomes a path component; the sessions file is private
    # schema and could carry anything.
    _output(tmp_path, "sid-1")
    scanner = _scanner(tmp_path, check=lambda p: True)
    assert scanner.scan({"../../etc", "", "a/b"}, time.time()) == frozenset()


def test_handle_checker_failure_degrades_to_not_busy(tmp_path):
    _output(tmp_path, "sid-1")

    def broken(p: Path) -> bool:
        raise OSError("libproc unavailable")

    assert _scanner(tmp_path, check=broken).scan(
        {"sid-1"}, time.time()) == frozenset()


# --- the caches that keep the ~150 ms kernel walk off the steady state ------

def test_held_verdict_is_cached_within_ttl(tmp_path):
    _output(tmp_path, "sid-1")
    calls = []
    scanner = _scanner(tmp_path, check=lambda p: calls.append(p) or True)
    now = time.time()
    assert scanner.scan({"sid-1"}, now) == {"sid-1"}
    assert scanner.scan({"sid-1"}, now + 1.0) == {"sid-1"}
    assert len(calls) == 1                      # second scan hit the cache
    assert scanner.scan(
        {"sid-1"}, now + scanner.HANDLE_TTL_SECONDS + 1.0) == {"sid-1"}
    assert len(calls) == 2                      # TTL expiry re-asks the kernel


def test_once_held_then_closed_is_done_despite_fresh_mtime(tmp_path):
    # A finishing bash task's last write stamps a fresh mtime; having *seen*
    # the file held, its closure means finished -- no 60 s false-busy tail.
    path = _output(tmp_path, "sid-1")
    verdict = {"held": True}
    scanner = _scanner(tmp_path, check=lambda p: verdict["held"])
    now = time.time()
    assert scanner.scan({"sid-1"}, now) == {"sid-1"}
    verdict["held"] = False
    os.utime(path, (now, now))                  # final output write
    later = now + scanner.HANDLE_TTL_SECONDS + 1.0
    assert scanner.scan({"sid-1"}, later) == frozenset()
    assert scanner.scan({"sid-1"}, later + 1.0) == frozenset()


def test_stale_closed_file_is_asked_only_once(tmp_path):
    _output(tmp_path, "sid-1")
    calls = []
    scanner = _scanner(tmp_path, check=lambda p: calls.append(p) or False)
    now = time.time()
    assert scanner.scan({"sid-1"}, now) == frozenset()
    assert scanner.scan({"sid-1"}, now + 1.0) == frozenset()
    assert len(calls) == 1                      # quiet files skip the kernel


def test_quiet_file_revives_on_fresh_mtime(tmp_path):
    # An idle subagent transcript that resumes appending must come back.
    path = _output(tmp_path, "sid-1", name="a0000001.output")
    scanner = _scanner(tmp_path, check=lambda p: False)
    now = time.time()
    assert scanner.scan({"sid-1"}, now) == frozenset()
    os.utime(path, (now, now))
    assert scanner.scan({"sid-1"}, now + 1.0) == {"sid-1"}


def test_handle_checks_are_budgeted_per_scan(tmp_path):
    for index in range(5):
        _output(tmp_path, "sid-1", name=f"b000000{index}.output")
    calls = []
    scanner = _scanner(tmp_path, check=lambda p: calls.append(p) or False)
    scanner.scan({"sid-1"}, time.time())
    assert len(calls) == scanner.MAX_HANDLE_CHECKS_PER_SCAN
