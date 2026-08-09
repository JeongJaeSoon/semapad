"""State vocabulary and priority. Knows nothing about hardware or files."""
from __future__ import annotations

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
