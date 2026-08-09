"""AmbientPanel -- owner, alert and fault feedback on the border (spec §7). Pure.

Layer gating is deliberately absent here: whether this light may be written at
all is the compositor's decision.
"""
from __future__ import annotations

from semapad.config import Config
from semapad.model import LIGHT_OFF, Light
from semapad.view import View

_NOTABLE = ("waiting", "error")


def light(view: View, owner: str, cfg: Config, *, feedback: bool) -> Light:
    """The desired border light for this tick.

    Fault feedback (empty key / failed open) overrides everything briefly.
    The owner colour carries the alert effect when a conversation demanding a
    human is not visible: with scope ``outside`` that means bumped off the six
    keys; while Codex owns the pad, every Claude conversation is out of sight,
    so all states count (spec §9.1 "Codex 보는 동안 기다리면 blink").
    """
    if feedback:
        return Light(cfg.underglow_claude, cfg.effect_fault)
    if owner == "none" or cfg.underglow_scope == "off":
        return LIGHT_OFF

    colour = cfg.underglow_claude if owner == "claude" else cfg.underglow_codex
    if cfg.underglow_scope == "all_sessions" or owner == "codex":
        states = (row.state for row in view.conversations)
    else:   # outside: only conversations that did not get a key
        states = (row.state for row in view.conversations if row.key is None)
    alert = any(state in _NOTABLE for state in states)
    return Light(colour, cfg.effect_alert if alert else cfg.effect_normal)
