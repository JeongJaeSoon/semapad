import json
from pathlib import Path

import pytest

from semapad.config import Config, load
from semapad.model import AgentState


def test_missing_file_gives_defaults(tmp_path: Path):
    cfg, warnings = load(tmp_path / "nope.json")
    assert cfg.gate_mode == "frontmost"
    assert warnings == []


def test_user_values_override(tmp_path: Path):
    p = tmp_path / "config.json"
    p.write_text(json.dumps({
        "gate": {"mode": "always"},
        "timing": {"poll_ms": 500},
    }))
    cfg, _ = load(p)
    assert cfg.gate_mode == "always"
    assert cfg.poll_ms == 500


def test_bad_value_falls_back_and_warns(tmp_path: Path):
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"gate": {"mode": "sideways"}}))
    cfg, warnings = load(p)
    assert cfg.gate_mode == "frontmost"
    assert any("mode" in w for w in warnings)


def test_broken_json_does_not_raise(tmp_path: Path):
    p = tmp_path / "config.json"
    p.write_text("{not json")
    cfg, warnings = load(p)
    assert cfg.gate_mode == "frontmost"
    assert warnings != []


def test_non_numeric_timing_falls_back_instead_of_crashing(tmp_path: Path):
    """One wrong setting must not stop startup."""
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"timing": {"poll_ms": "fast"}}))
    cfg, warnings = load(p)
    assert cfg.poll_ms == 250
    assert any("poll_ms" in w for w in warnings)


# JSON only guarantees syntax. Every one of these parses cleanly and used to
# either raise or silently produce a nonsense Config.
@pytest.mark.parametrize("body", [
    "[]",                                        # root is not an object
    '"just a string"',                           # root is a scalar
    '{"gate": []}',                              # section is not an object
    '{"gate": {"mode": []}}',                    # enum value is unhashable
    '{"gate": {"mode": 7}}',                     # enum has wrong scalar type
    '{"underglow": {"effects": []}}',             # nested section is not an object
    '{"timing": {"poll_ms": 1e400}}',            # overflows int()
    '{"timing": {"poll_ms": true}}',             # bool is an int in Python
    '{"gate": {"yield_to": "com.openai.chat"}}',  # bare string, not a list
    '{"gate": {"own_when": [1, 2]}}',            # list of non-strings
])
def test_any_shape_of_json_still_starts(tmp_path: Path, body: str):
    p = tmp_path / "config.json"
    p.write_text(body)
    cfg, warnings = load(p)          # must not raise
    assert isinstance(cfg, Config)
    assert warnings, f"{body} should have warned"


def test_bare_string_is_not_shredded_into_characters(tmp_path: Path):
    """tuple("abc") gives ('a','b','c') -- silently worse than crashing, because
    the gate would then compare bundle ids against single letters forever."""
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"gate": {"yield_to": "com.openai.chat"}}))
    cfg, _ = load(p)
    assert cfg.yield_to == ("com.openai.codex",)


@pytest.mark.parametrize("value", [0, -100, 0.5, -1])
def test_poll_ms_must_be_positive(tmp_path: Path, value):
    """poll_ms=0 turns the daemon into a busy loop, and 0.5 truncates into it."""
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"timing": {"poll_ms": value}}))
    cfg, warnings = load(p)
    assert cfg.poll_ms == 250
    assert any("poll_ms" in w for w in warnings)


def test_valid_string_list_is_kept(tmp_path: Path):
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"gate": {"yield_to": ["a.b", "c.d"]}}))
    cfg, warnings = load(p)
    assert cfg.yield_to == ("a.b", "c.d")
    assert warnings == []


def test_desktop_defaults_and_status_poll_cadence():
    cfg, warnings = load(None)
    assert cfg.own_when == ("com.anthropic.claudefordesktop",)
    assert cfg.yield_to == ("com.openai.codex",)
    assert cfg.status_poll_ms == 1000
    assert warnings == []


def test_removed_iterm_and_tab_settings_are_not_part_of_config():
    cfg = Config()
    for name in (
        "mod_key",
        "knob_tab_switch",
        "mod_direct_tab",
        "underglow_iterm",
        "legacy_underglow_codex_mode",
        "mod_release_timeout_ms",
        "slots_order",
    ):
        assert not hasattr(cfg, name)


def test_ownership_colours_and_effects_use_measured_defaults():
    cfg, warnings = load(None)
    assert (cfg.underglow_claude, cfg.underglow_codex) == (0xFF6D00, 0x304FFE)
    assert (cfg.effect_normal, cfg.effect_alert, cfg.effect_fault) == \
        ("solid", "blink", "rainbow")
    assert warnings == []


@pytest.mark.parametrize("value, expected", [
    ("#000000", 0x000000), ("#123456", 0x123456), ("#aBcDeF", 0xABCDEF),
    (0, 0), (0xFFFFFF, 0xFFFFFF),
])
def test_colours_accept_exact_hex_strings_or_rgb_integers(tmp_path: Path, value, expected):
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"underglow": {"claude": value}}))
    cfg, warnings = load(p)
    assert cfg.underglow_claude == expected
    assert warnings == []


@pytest.mark.parametrize("value", ["123456", "#fff", "##123456", "#12345g", -1, 0x1000000, True])
def test_bad_colours_fall_back_and_warn(tmp_path: Path, value):
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"underglow": {"claude": value}}))
    cfg, warnings = load(p)
    assert cfg.underglow_claude == 0xFF6D00
    assert any("underglow.claude" in warning for warning in warnings)


def test_scope_layer_and_timing_values_are_loaded(tmp_path: Path):
    p = tmp_path / "config.json"
    p.write_text(json.dumps({
        "layer_gate": {"underglow": "off"},
        "underglow": {"scope": "all_sessions", "reclaim_delay_ms": 0},
        "state": {"working_max_seconds": 0},
        "timing": {"status_poll_ms": 1500},
    }))
    cfg, warnings = load(p)
    assert cfg.layer_underglow == "off"
    assert cfg.underglow_scope == "all_sessions"
    assert cfg.reclaim_delay_ms == 0
    assert cfg.working_max_seconds == 0
    assert cfg.status_poll_ms == 1500
    assert warnings == []


@pytest.mark.parametrize("section, key, value, expected", [
    ("layer_gate", "underglow", "flash", "keep"),
    ("underglow", "scope", "current", "outside"),
])
def test_new_enum_values_fall_back_and_warn(tmp_path: Path, section, key, value, expected):
    p = tmp_path / "config.json"
    p.write_text(json.dumps({section: {key: value}}))
    cfg, warnings = load(p)
    attr = {"underglow": "layer_underglow", "scope": "underglow_scope"}[key]
    assert getattr(cfg, attr) == expected
    assert warnings


def test_effect_validation_and_semantic_warnings(tmp_path: Path):
    p = tmp_path / "config.json"
    p.write_text(json.dumps({
        "underglow": {"effects": {"normal": "rainbow", "alert": "rainbow",
                                      "fault": "sparkle"}}
    }))
    cfg, warnings = load(p)
    assert cfg.effect_normal == "rainbow"
    assert cfg.effect_alert == "rainbow"
    assert cfg.effect_fault == "rainbow"
    assert any("sparkle" in warning for warning in warnings)
    assert any("same effect" in warning for warning in warnings)
    assert any("rainbow" in warning for warning in warnings)


@pytest.mark.parametrize("section, key, value, default", [
    ("timing", "status_poll_ms", 0, 1000),
    ("timing", "status_poll_ms", True, 1000),
    ("timing", "status_poll_ms", 0.5, 1000),
    ("underglow", "reclaim_delay_ms", -1, 200),
    ("state", "working_max_seconds", -1, 900),
])
def test_new_numeric_bounds_fall_back_and_warn(tmp_path: Path, section, key, value, default):
    p = tmp_path / "config.json"
    p.write_text(json.dumps({section: {key: value}}))
    cfg, warnings = load(p)
    attr = key
    assert getattr(cfg, attr) == default
    assert any(key in warning for warning in warnings)


def test_state_colours_default_to_the_factory_palette():
    cfg, warnings = load(None)
    assert cfg.colors == {
        AgentState.IDLE: 0xFFFFFF,
        AgentState.WORKING: 0x304FFE,
        AgentState.WAITING: 0xFF6D00,
        AgentState.DONE: 0x00FF4C,
        AgentState.ERROR: 0xFF0033,
    }
    assert warnings == []


def test_state_colours_are_customisable_one_state_at_a_time(tmp_path: Path):
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"colors": {"working": "#00AAFF", "done": 0x101010}}))
    cfg, warnings = load(p)
    assert cfg.colors[AgentState.WORKING] == 0x00AAFF
    assert cfg.colors[AgentState.DONE] == 0x101010
    assert cfg.colors[AgentState.IDLE] == 0xFFFFFF   # untouched states keep defaults
    assert warnings == []


@pytest.mark.parametrize("value", ["FF0000", "#f00", -1, 0x1000000, True])
def test_bad_state_colour_falls_back_and_warns(tmp_path: Path, value):
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"colors": {"error": value}}))
    cfg, warnings = load(p)
    assert cfg.colors[AgentState.ERROR] == 0xFF0033
    assert any("colors.error" in warning for warning in warnings)


def test_colors_section_of_the_wrong_shape_is_ignored_with_a_warning(tmp_path: Path):
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"colors": ["#FFFFFF"]}))
    cfg, warnings = load(p)
    assert cfg.colors[AgentState.IDLE] == 0xFFFFFF
    assert any("colors" in warning for warning in warnings)


def test_slot_order_is_no_longer_a_setting(tmp_path: Path):
    """Keys compact on lifecycle events only (spec §5, §11.2). The old
    recent/recent_sticky/priority orders are gone; an old config section is
    ignored without blocking startup."""
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"slots": {"order": "recent_sticky"}}))
    cfg, warnings = load(p)
    assert not hasattr(cfg, "slots_order")
    assert warnings == []


def test_gate_exclusive_parses_and_rejects_nonbool(tmp_path: Path):
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"gate": {"exclusive": True}}))
    cfg, warnings = load(p)
    assert cfg.exclusive is True and warnings == []
    p.write_text(json.dumps({"gate": {"exclusive": "yes"}}))
    cfg, warnings = load(p)
    assert cfg.exclusive is False
    assert any("gate.exclusive" in w for w in warnings)
