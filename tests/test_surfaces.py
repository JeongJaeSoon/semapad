from __future__ import annotations

from semapad.config import Config
from semapad.model import LIGHT_OFF, PALETTE, AgentState, Light
from semapad.surfaces import agent_keys, ambient
from semapad import view

from test_view import _build, conv, rec   # reuse the view fixtures


def test_agent_keys_mirror_slot_colors_exactly():
    v = _build([conv("w", cli="cw", activity=2.0)], {"cw"},
               [rec("cw", AgentState.WAITING)])
    lights = agent_keys.lights(v)
    assert lights[0] == Light(PALETTE[AgentState.WAITING])
    assert lights[1:] == (LIGHT_OFF,) * 5
    # the surface never recomputes: the light IS the slot's snapshot colour
    assert lights[0].colour == v.slots[0].color


def _cfg(**kwargs) -> Config:
    return Config(**kwargs)


def test_ambient_feedback_overrides_everything():
    v = _build([], set(), [])
    light = ambient.light(v, "none", _cfg(), feedback=True)
    assert light == Light(0xFF6D00, "rainbow")


def test_ambient_off_without_owner_or_scope():
    v = _build([conv("a", activity=1.0)], set(), [])
    assert ambient.light(v, "none", _cfg(), feedback=False) is LIGHT_OFF
    assert ambient.light(v, "claude", _cfg(underglow_scope="off"),
                         feedback=False) is LIGHT_OFF


def test_ambient_owner_colours_and_normal_effect():
    v = _build([conv("a", activity=1.0)], set(), [])
    assert ambient.light(v, "claude", _cfg(), feedback=False) == \
        Light(0xFF6D00, "solid")
    assert ambient.light(v, "codex", _cfg(), feedback=False) == \
        Light(0x304FFE, "solid")


def test_ambient_outside_scope_alerts_only_for_bumped_conversations():
    convs = [conv(str(i), cli=f"c{i}", activity=float(i)) for i in range(7)]
    live = {f"c{i}" for i in range(7)}
    # a waiting conversation that won a key: no alert for claude owner
    v = _build(convs, live, [rec("c6", AgentState.WAITING)])
    assert ambient.light(v, "claude", _cfg(), feedback=False).effect == "solid"
    # all seven waiting: the bumped one alerts
    v = _build(convs, live, [rec(f"c{i}", AgentState.WAITING) for i in range(7)])
    assert ambient.light(v, "claude", _cfg(), feedback=False).effect == "blink"


def test_ambient_codex_owner_alerts_for_any_waiting_conversation():
    # spec §9.1: while Codex is watched, a waiting Claude conversation blinks
    convs = [conv("a", cli="ca", activity=1.0)]
    v = _build(convs, {"ca"}, [rec("ca", AgentState.WAITING)])
    light = ambient.light(v, "codex", _cfg(), feedback=False)
    assert light == Light(0x304FFE, "blink")
