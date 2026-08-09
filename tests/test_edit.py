from __future__ import annotations

import dataclasses
import json
from pathlib import Path

from semapad.config import Config, load
from semapad.web import edit


def test_schema_covers_spec_targets_only():
    fields = edit.schema(Config())
    paths = {f["path"] for f in fields}
    assert {"colors.idle", "colors.working", "colors.waiting", "colors.done",
            "colors.error"} <= paths
    assert "gate.mode" in paths
    assert "underglow.claude" in paths
    assert "underglow.effects.alert" in paths
    # spec §6 targets only -- no timing/state keys leak into the form
    assert not any(p.startswith(("timing.", "state.")) for p in paths)


def test_schema_names_only_real_config_fields():
    field_names = {f.name for f in dataclasses.fields(Config)}
    assert set(edit._EDITABLE) <= field_names


def test_schema_carries_current_values(tmp_path: Path):
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"colors": {"idle": "#123456"}}))
    cfg, _ = load(p)
    (idle,) = [f for f in edit.schema(cfg) if f["path"] == "colors.idle"]
    assert idle["value"] == "#123456"
    assert idle["kind"] == "color"


def test_apply_valid_edit_writes_and_preserves_foreign_keys(tmp_path: Path):
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"state": {"working_max_seconds": 60},
                             "colors": {"error": "#ff0000"}}))
    errors = edit.apply_edits(p, {"colors.idle": "#ABCDEF",
                                  "gate.mode": "always",
                                  "underglow.effects.alert": "pulse",
                                  "gate.yield_to": ["com.example.app"]})
    assert errors == {}
    raw = json.loads(p.read_text())
    assert raw["colors"]["idle"] == "#abcdef"          # normalized lowercase
    assert raw["colors"]["error"] == "#ff0000"         # untouched sibling
    assert raw["gate"]["mode"] == "always"
    assert raw["underglow"]["effects"]["alert"] == "pulse"
    assert raw["state"]["working_max_seconds"] == 60   # foreign section kept
    cfg, warnings = load(p)
    assert warnings == []
    assert cfg.gate_mode == "always"


def test_apply_creates_missing_config_file(tmp_path: Path):
    p = tmp_path / "config.json"
    assert edit.apply_edits(p, {"colors.done": "#00ff00"}) == {}
    assert json.loads(p.read_text())["colors"]["done"] == "#00ff00"


def test_invalid_values_are_all_or_nothing(tmp_path: Path):
    p = tmp_path / "config.json"
    p.write_text("{}")
    errors = edit.apply_edits(p, {
        "colors.idle": "#ffffff",          # valid
        "colors.working": "blue",          # invalid hex
        "gate.mode": "sometimes",          # invalid enum
        "underglow.reclaim_delay_ms": -1,  # invalid int
        "gate.yield_to": "not-a-list",     # invalid strings
        "state.working_max_seconds": 5,    # not editable
    })
    assert set(errors) == {"colors.working", "gate.mode",
                           "underglow.reclaim_delay_ms", "gate.yield_to",
                           "state.working_max_seconds"}
    assert json.loads(p.read_text()) == {}   # untouched


def test_unknown_color_state_rejected(tmp_path: Path):
    errors = edit.apply_edits(tmp_path / "c.json", {"colors.purple": "#101010"})
    assert "colors.purple" in errors
