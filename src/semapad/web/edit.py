"""Phase 2 config editing (spec §6).

The editable set is generated from the Config dataclass fields so a dead
config key can never appear in the form. Validation here REJECTS bad values
with inline errors -- deliberately different from config.load(), whose job is
to fall back and warn so startup never blocks.
"""
from __future__ import annotations

import dataclasses
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from semapad.config import Config
from semapad.model import AgentState
from semapad.protocol import EFFECTS

_HEX = re.compile(r"#[0-9a-fA-F]{6}")

#: field name in Config -> (config.json path, input kind, allowed/None)
#: Only the spec §6 targets: colors (5), gate.*, underglow.*.
_EDITABLE: dict[str, tuple[tuple[str, ...], str, tuple[str, ...] | None]] = {
    "gate_mode": (("gate", "mode"), "enum", ("frontmost", "always", "off")),
    "yield_to": (("gate", "yield_to"), "strings", None),
    "own_when": (("gate", "own_when"), "strings", None),
    "underglow_claude": (("underglow", "claude"), "color", None),
    "underglow_codex": (("underglow", "codex"), "color", None),
    "underglow_scope": (("underglow", "scope"), "enum",
                        ("outside", "all_sessions", "off")),
    "reclaim_delay_ms": (("underglow", "reclaim_delay_ms"), "int", None),
    "effect_normal": (("underglow", "effects", "normal"), "enum",
                      tuple(sorted(EFFECTS))),
    "effect_alert": (("underglow", "effects", "alert"), "enum",
                     tuple(sorted(EFFECTS))),
    "effect_fault": (("underglow", "effects", "fault"), "enum",
                     tuple(sorted(EFFECTS))),
}

_FIELD_NAMES = {f.name for f in dataclasses.fields(Config)}
assert set(_EDITABLE) <= _FIELD_NAMES, "editable table names a dead Config field"


def _color_str(value: int) -> str:
    return f"#{value:06x}"


def schema(cfg: Config) -> list[dict[str, Any]]:
    """Editable fields with their current values, formwise."""
    out: list[dict[str, Any]] = []
    for state in AgentState:
        out.append({
            "path": f"colors.{state.value}", "kind": "color",
            "value": _color_str(cfg.colors[state]), "options": None,
        })
    for name, (path, kind, allowed) in _EDITABLE.items():
        value = getattr(cfg, name)
        if kind == "color":
            value = _color_str(value)
        elif kind == "strings":
            value = list(value)
        out.append({"path": ".".join(path), "kind": kind, "value": value,
                    "options": list(allowed) if allowed else None})
    return out


_STATE_VALUES = tuple(s.value for s in AgentState)


def _validate(path: str, value: object) -> tuple[Any | None, str | None]:
    """Return (normalized value, None) or (None, inline error message)."""
    parts = tuple(path.split("."))
    if parts[0] == "colors":
        if len(parts) != 2 or parts[1] not in _STATE_VALUES:
            return None, "unknown colour state"
        kind, allowed = "color", None
    else:
        for field_path, field_kind, field_allowed in _EDITABLE.values():
            if field_path == parts:
                kind, allowed = field_kind, field_allowed
                break
        else:
            return None, "not an editable setting"

    if kind == "color":
        if isinstance(value, str) and _HEX.fullmatch(value):
            return value.lower(), None
        return None, "colour must be #RRGGBB"
    if kind == "enum":
        if isinstance(value, str) and allowed and value in allowed:
            return value, None
        return None, f"must be one of: {', '.join(allowed or ())}"
    if kind == "int":
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return None, "must be a non-negative integer"
        return value, None
    if kind == "strings":
        if (isinstance(value, list)
                and all(isinstance(v, str) and v.strip() for v in value)):
            return [v.strip() for v in value], None
        return None, "must be a list of non-empty strings"
    return None, "unknown kind"      # unreachable while the table is sound


def apply_edits(config_path: Path,
                edits: dict[str, object]) -> dict[str, str]:
    """Validate all edits; on success atomically rewrite config.json.

    All-or-nothing: any inline error leaves the file untouched. Unknown
    sections a user keeps in the file (timing, state, ...) are preserved.
    """
    errors: dict[str, str] = {}
    normalized: dict[str, Any] = {}
    for path, value in edits.items():
        good, error = _validate(path, value)
        if error is not None:
            errors[path] = error
        else:
            normalized[path] = good
    if errors:
        return errors

    try:
        raw = json.loads(config_path.read_text())
        if not isinstance(raw, dict):
            raw = {}
    except (OSError, ValueError):
        raw = {}

    for path, value in normalized.items():
        node = raw
        parts = path.split(".")
        for part in parts[:-1]:
            if not isinstance(node.get(part), dict):
                node[part] = {}
            node = node[part]
        node[parts[-1]] = value

    config_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=config_path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(raw, f, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, config_path)
    except Exception:
        Path(tmp).unlink(missing_ok=True)
        raise
    return {}
