"""State vocabulary and priority. Knows nothing about hardware or files."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterable


class AgentState(str, Enum):
    IDLE = "idle"
    WORKING = "working"
    DONE = "done"
    ERROR = "error"
    WAITING = "waiting"


#: Higher wins. waiting is top because it is the only state that blocks a human.
PRIORITY: dict[AgentState, int] = {
    AgentState.IDLE: 0,
    AgentState.WORKING: 1,
    AgentState.DONE: 2,
    AgentState.ERROR: 3,
    AgentState.WAITING: 4,
}


def highest(states: Iterable[AgentState]) -> AgentState | None:
    """The state to surface first. None when there is nothing."""
    ranked = sorted(states, key=lambda s: PRIORITY[s], reverse=True)
    return ranked[0] if ranked else None


#: Factory colours, overridable per state through ``colors`` in the config.
#: Matching what the vendor uses for Codex keeps the eye honest.
PALETTE: dict[AgentState, int] = {
    AgentState.IDLE: 0xFFFFFF,
    AgentState.WORKING: 0x304FFE,
    AgentState.WAITING: 0xFF6D00,
    AgentState.DONE: 0x00FF4C,
    AgentState.ERROR: 0xFF0033,
}


#: Zone ownership vocabulary (spec §7). Plain strings on purpose -- these cross
#: process boundaries in snapshots and config.
OWNERS = frozenset({"none", "claude", "codex"})


class Zone(str, Enum):
    """The two writable surfaces of the pad semapad may own."""

    KEYS = "keys"          # A-zone agent keys
    AMBIENT = "ambient"    # border underglow


@dataclass(frozen=True)
class Light:
    """One desired light: unified for keys and border (spec §7).

    ``effect`` is a protocol effect name; ``off`` turns the light off and the
    colour is then meaningless.
    """

    colour: int | None
    effect: str = "solid"

    @property
    def off(self) -> bool:
        return self.effect == "off" or self.colour is None


LIGHT_OFF = Light(None, "off")
