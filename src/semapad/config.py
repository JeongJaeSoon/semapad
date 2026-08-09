"""Config loading. Bad values fall back to defaults and collect a warning --
nothing here may block startup."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from semapad.protocol import EFFECTS
from semapad.model import PALETTE
from semapad.model import AgentState

_GATE_MODES = {"frontmost", "always", "off"}
_SCOPES = {"outside", "all_sessions", "off"}
_LAYER_UNDERGLOW = {"keep", "off"}
_HEX_COLOUR = re.compile(r"#[0-9a-fA-F]{6}")


@dataclass(frozen=True)
class Config:
    gate_mode: str = "frontmost"
    yield_to: tuple[str, ...] = ("com.openai.codex",)
    own_when: tuple[str, ...] = ("com.anthropic.claudefordesktop",)
    colors: dict[AgentState, int] = field(default_factory=lambda: dict(PALETTE))
    underglow_claude: int = 0xFF6D00
    underglow_codex: int = 0x304FFE
    effect_normal: str = "solid"
    effect_alert: str = "blink"
    effect_fault: str = "rainbow"
    underglow_scope: str = "outside"
    reclaim_delay_ms: int = 200
    layer_underglow: str = "keep"
    ttl_minutes: int = 30
    working_max_seconds: int = 900
    poll_ms: int = 250
    status_poll_ms: int = 1000


def _reject(value, default, label: str, warnings: list[str]):
    warnings.append(f"{label}: {value!r} is not usable, fell back to {default!r}")
    return default


def _section(raw: dict, key: str, label: str, warnings: list[str]) -> dict:
    """A section must be an object. Anything else is ignored -- but say so, or a
    whole block of the user's config vanishes without a word."""
    value = raw.get(key)
    if value is None or isinstance(value, dict):
        return value or {}
    warnings.append(f"{label}: expected an object, got {type(value).__name__}; ignored")
    return {}


def _pick(value, allowed: set[str], default: str, label: str,
          warnings: list[str]) -> str:
    """Enum-ish string. Non-strings never reach the set -- `[] in {...}` raises."""
    if value is None:
        return default
    if not isinstance(value, str) or value not in allowed:
        return _reject(value, default, label, warnings)
    return value


def _int(source: dict, key: str, default: int, label: str,
         warnings: list[str], minimum: int = 0) -> int:
    """Coerce to int, falling back when that is impossible. Floats truncate
    (30.7 -> 30) rather than being rejected. One bad value must not block startup.

    Every setting here is a duration or a count, so ``minimum`` guards the values
    that are nonsense below it -- poll_ms=0 turns the daemon into a busy loop,
    and 0.5 truncates straight into it.
    """
    if key not in source:
        return default
    value = source[key]
    if isinstance(value, bool):     # bool is an int in Python; almost never intended here
        return _reject(value, default, label, warnings)
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return _reject(value, default, label, warnings)
    if number < minimum:
        return _reject(value, default, label, warnings)
    return number


def _strings(value, default: tuple[str, ...], label: str,
             warnings: list[str]) -> tuple[str, ...]:
    """A list of strings. A bare string would otherwise be shredded into characters."""
    if value is None:
        return default
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        return _reject(value, default, label, warnings)
    return tuple(value)


def _colour(value, default: int, label: str, warnings: list[str]) -> int:
    """Read a 24-bit integer or an exact ``#RRGGBB`` string."""
    if value is None:
        return default
    if isinstance(value, int) and not isinstance(value, bool):
        if 0 <= value <= 0xFFFFFF:
            return value
        return _reject(value, default, label, warnings)
    if isinstance(value, str) and _HEX_COLOUR.fullmatch(value):
        return int(value[1:], 16)
    return _reject(value, default, label, warnings)


def load(path: Path | None) -> tuple[Config, list[str]]:
    warnings: list[str] = []
    raw: dict = {}

    if path is not None and path.exists():
        try:
            raw = json.loads(path.read_text())
        except Exception as exc:
            warnings.append(f"config unreadable, using all defaults: {exc}")
            raw = {}

    # JSON only guarantees syntax. A file can parse cleanly and still be a list,
    # or hold a list where a string belongs -- every value below is shape-checked.
    if not isinstance(raw, dict):
        warnings.append(f"config must be an object, got {type(raw).__name__}; "
                        "using all defaults")
        raw = {}

    gate = _section(raw, "gate", "gate", warnings)
    glow = _section(raw, "underglow", "underglow", warnings)
    timing = _section(raw, "timing", "timing", warnings)
    state = _section(raw, "state", "state", warnings)
    layer = _section(raw, "layer_gate", "layer_gate", warnings)
    colors = _section(raw, "colors", "colors", warnings)

    effects_section = _section(glow, "effects", "underglow.effects", warnings)
    effects = {
        name: _pick(effects_section.get(name), set(EFFECTS), default,
                    f"underglow.effects.{name}", warnings)
        for name, default in (("normal", "solid"), ("alert", "blink"),
                              ("fault", "rainbow"))
    }
    for name in ("normal", "alert"):
        if effects[name] == "rainbow":
            warnings.append(
                f"underglow.effects.{name}: rainbow ignores the ownership colour")
    if len(set(effects.values())) < len(effects):
        warnings.append("underglow.effects: two states use the same effect")

    return Config(
        gate_mode=_pick(gate.get("mode"), _GATE_MODES, "frontmost", "gate.mode", warnings),
        yield_to=_strings(gate.get("yield_to"), ("com.openai.codex",),
                          "gate.yield_to", warnings),
        own_when=_strings(gate.get("own_when"), ("com.anthropic.claudefordesktop",),
                          "gate.own_when", warnings),
        colors={state: _colour(colors.get(state.value), default,
                               f"colors.{state.value}", warnings)
                for state, default in PALETTE.items()},
        underglow_claude=_colour(glow.get("claude"), 0xFF6D00,
                                 "underglow.claude", warnings),
        underglow_codex=_colour(glow.get("codex"), 0x304FFE,
                                "underglow.codex", warnings),
        effect_normal=effects["normal"],
        effect_alert=effects["alert"],
        effect_fault=effects["fault"],
        underglow_scope=_pick(glow.get("scope"), _SCOPES, "outside",
                              "underglow.scope", warnings),
        reclaim_delay_ms=_int(glow, "reclaim_delay_ms", 200,
                              "underglow.reclaim_delay_ms", warnings, minimum=0),
        layer_underglow=_pick(layer.get("underglow"), _LAYER_UNDERGLOW, "keep",
                              "layer_gate.underglow", warnings),
        ttl_minutes=_int(state, "ttl_minutes", 30, "state.ttl_minutes",
                         warnings, minimum=1),
        working_max_seconds=_int(state, "working_max_seconds", 900,
                                 "state.working_max_seconds", warnings, minimum=0),
        poll_ms=_int(timing, "poll_ms", 250, "timing.poll_ms",
                     warnings, minimum=1),
        status_poll_ms=_int(timing, "status_poll_ms", 1000,
                            "timing.status_poll_ms", warnings, minimum=1),
    ), warnings
