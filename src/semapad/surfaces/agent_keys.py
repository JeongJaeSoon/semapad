"""AgentKeyStrip -- the six A-zone key lights (spec §7). Pure.

The view already computed each slot's final colour (spec §5.1); this surface
only casts it into :class:`Light`. If the dashboard and the pad ever disagree,
the bug is here or in the compositor -- never in two divergent computations.
"""
from __future__ import annotations

from semapad.model import LIGHT_OFF, Light
from semapad.view import View


def lights(view: View) -> tuple[Light, ...]:
    return tuple(
        Light(slot.color) if slot.color is not None else LIGHT_OFF
        for slot in view.slots
    )
