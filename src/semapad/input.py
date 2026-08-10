"""KeyRouter -- physical key input to deep links, behind the owner gate (spec §7).

A valid press opens the conversation immediately, foreground. The vendor's
single-tap-background / double-tap-raise split was tried and dropped
(2026-08-10 acceptance): the claude:// deeplink makes Desktop raise itself, so
a "background" open does not exist for us -- the tap window added 350 ms of
lag for a distinction the platform cannot deliver. Research continues in
semapad#12; if a no-activate path is found, the window logic lives in git
history.
"""
from __future__ import annotations

import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from semapad import deeplink

KEY_NAMES = {f"AG{i:02d}": i for i in range(6)}
_OPEN_TIMEOUT_SECONDS = 5.0

#: Terminal dispositions surfaced as ``last_input_result`` (spec §5.2 D).
RESULTS = frozenset({
    "opened", "open_failed", "empty_slot",
    "ignored_owner", "ignored_layer", "ignored_input",
})


_children: list = []   # fire-and-forget opens awaiting reaping


def _reap_children() -> None:
    _children[:] = [child for child in _children if child.poll() is None]


def open_local(local_id: str,
               spawner: Callable[..., object] = subprocess.Popen) -> bool:
    """Open a conversation by its own local_id -- dead conversations included.

    Fire-and-forget: /usr/bin/open can block for hundreds of milliseconds
    waiting on LaunchServices, and a synchronous wait both delayed the switch
    and serialized rapid presses behind each other. The URL is validated
    before spawning, so a successful spawn is the success signal.
    """
    try:
        url = deeplink.url_for(local_id)
    except ValueError:
        return False
    _reap_children()
    try:
        child = spawner(["/usr/bin/open", url],
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL)
    except OSError:
        return False
    _children.append(child)
    return True


@dataclass(frozen=True)
class Press:
    """One validated A-key press. Anything else never reaches dispatch."""

    key_index: int


@dataclass(frozen=True)
class Outcome:
    result: str
    feedback: bool = False       # brief fault flash on the border (P6)
    press_seen: bool = False     # the pad self-paints on any press (#60)


def parse(message: object) -> Press | None | str:
    """Return a Press, ``None``, ``"release"``, or ``"invalid"``.

    A release (act 0 on a known key) is expected traffic, not noise: it must
    never overwrite the last meaningful press in the ui, so it gets its own
    disposition instead of "invalid".
    """
    if not isinstance(message, dict) or message.get("m") != "v.oai.hid":
        return None
    params = message.get("p")
    key = params.get("k") if isinstance(params, dict) else None
    action = params.get("act") if isinstance(params, dict) else None
    index = KEY_NAMES.get(key) if type(key) is str else None
    if index is None or isinstance(action, bool):
        return "invalid"
    if action == 0:
        return "release"
    if action != 1:
        return "invalid"
    return Press(key_index=index)


class KeyRouter:
    """Dispatch validated presses through the owner and layer gates."""

    def __init__(self, opener: Callable[[str], bool] | None = None) -> None:
        self._opener = opener if opener is not None else open_local

    def dispatch(self, press: Press, now: float, *, owner: str,
                 layer_one: bool, slots: Sequence[str | None]) -> Outcome:
        """Route one validated press.

        Every outcome here has ``press_seen`` -- the pad lights the whole
        A-zone by itself while a key is down, so any physical press must dirty
        the keys regardless of what the gate decided (#60).
        """
        if owner != "claude":
            return Outcome("ignored_owner", press_seen=True)
        if not layer_one:
            return Outcome("ignored_layer", press_seen=True)
        local_id = slots[press.key_index] if press.key_index < len(slots) else None
        if local_id is None:
            return Outcome("empty_slot", feedback=True, press_seen=True)
        try:
            opened = bool(self._opener(local_id))
        except Exception:
            opened = False
        if opened:
            return Outcome("opened", press_seen=True)
        return Outcome("open_failed", feedback=True, press_seen=True)
