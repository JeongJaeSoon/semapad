from __future__ import annotations

import json
import uuid
from pathlib import Path

from semapad.sources import conversations


def _write(root: Path, payload: dict, local_id: str | None = None) -> str:
    local_id = local_id or f"local_{uuid.uuid4()}"
    directory = root / "org" / "account"
    directory.mkdir(parents=True, exist_ok=True)
    body = {"sessionId": local_id, "isArchived": False,
            "cliSessionId": str(uuid.uuid4()), "cwd": "/tmp/w",
            "createdAt": 1_700_000_000_000, "lastActivityAt": 1_700_000_050_000,
            "title": "a title"} | payload
    (directory / f"{local_id}.json").write_text(json.dumps(body))
    return local_id


def test_scan_missing_root(tmp_path: Path):
    convs, _arch, diags = conversations.scan(tmp_path / "nope")
    assert convs == ()
    assert diags


def test_scan_returns_non_archived_only(tmp_path: Path):
    keep = _write(tmp_path, {})
    _write(tmp_path, {"isArchived": True})
    convs, _arch, diags = conversations.scan(tmp_path)
    assert [c.local_id for c in convs] == [keep]
    assert diags == ()


def test_scan_converts_milliseconds(tmp_path: Path):
    _write(tmp_path, {"lastActivityAt": 1_700_000_050_000,
                      "createdAt": 1_700_000_000_000,
                      "lastFocusedAt": 1_700_000_040_000})
    (conv,), _arch, _ = conversations.scan(tmp_path)
    assert conv.last_activity_at == 1_700_000_050.0
    assert conv.created_at == 1_700_000_000.0
    assert conv.last_focused_at == 1_700_000_040.0


def test_scan_missing_last_focused_is_zero(tmp_path: Path):
    _write(tmp_path, {})
    (conv,), _arch, _ = conversations.scan(tmp_path)
    assert conv.last_focused_at == 0.0


def test_scan_missing_title_and_cli_session(tmp_path: Path):
    lid = _write(tmp_path, {"title": None, "cliSessionId": None})
    (conv,), _arch, _ = conversations.scan(tmp_path)
    assert conv.local_id == lid
    assert conv.title == ""
    assert conv.cli_session_id is None


def test_scan_filename_is_the_authority(tmp_path: Path):
    _write(tmp_path, {"sessionId": f"local_{uuid.uuid4()}"})
    convs, _arch, diags = conversations.scan(tmp_path)
    assert convs == ()
    assert len(diags) == 1


def test_scan_malformed_file_is_a_diagnostic(tmp_path: Path):
    directory = tmp_path / "org" / "account"
    directory.mkdir(parents=True)
    (directory / f"local_{uuid.uuid4()}.json").write_text("{broken")
    convs, _arch, diags = conversations.scan(tmp_path)
    assert convs == ()
    assert len(diags) == 1


def test_scan_missing_is_archived_is_a_diagnostic(tmp_path: Path):
    lid = f"local_{uuid.uuid4()}"
    directory = tmp_path / "org" / "account"
    directory.mkdir(parents=True)
    (directory / f"{lid}.json").write_text(json.dumps({"sessionId": lid}))
    convs, _arch, diags = conversations.scan(tmp_path)
    assert convs == ()
    assert len(diags) == 1


def test_scan_skips_symlinked_entries(tmp_path: Path):
    real = _write(tmp_path, {})
    outside = tmp_path.parent / "outside.json"
    outside.write_text("{}")
    link = tmp_path / "org" / "account" / f"local_{uuid.uuid4()}.json"
    link.symlink_to(outside)
    convs, _arch, _ = conversations.scan(tmp_path)
    assert [c.local_id for c in convs] == [real]


def test_scan_pinned_defaults_false(tmp_path: Path):
    _write(tmp_path, {})
    (conv,), _arch, _ = conversations.scan(tmp_path)
    assert conv.pinned is False
