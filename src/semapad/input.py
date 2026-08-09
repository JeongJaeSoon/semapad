"""KeyRouter -- physical key input to deep links, behind the owner gate (spec §7).

Tap semantics follow the vendor (spec §9): a single tap focuses the
conversation in the background (``open -g``) so a mispress never steals the
screen; a double tap raises the Desktop window. A single tap therefore
dispatches only after the double-tap window has passed without a second press.
"""
from __future__ import annotations

import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from semapad import deeplink

KEY_NAMES = {f"AG{i:02d}": i for i in range(6)}
DOUBLE_TAP_SECONDS = 0.35
_OPEN_TIMEOUT_SECONDS = 5.0

#: Terminal dispositions surfaced as ``last_input_result`` (spec §5.2 D).
#: ``pending`` is the one non-terminal outcome: a single tap waiting out the
#: double-tap window.
RESULTS = frozenset({
    "opened_background", "opened_foreground", "open_failed", "empty_slot",
    "ignored_owner", "ignored_layer", "ignored_input", "pending",
})


def open_local(local_id: str, *, foreground: bool,
               runner: Callable[..., object] = subprocess.run) -> bool:
    """Open a conversation by its own local_id -- dead conversations included."""
    try:
        url = deeplink.url_for(local_id)
    except ValueError:
        return False
    command = ["/usr/bin/open"] + ([] if foreground else ["-g"]) + [url]
    try:
        completed = runner(command, check=False, timeout=_OPEN_TIMEOUT_SECONDS)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return getattr(completed, "returncode", None) == 0


@dataclass(frozen=True)
class Press:
    """One validated A-key press. Anything else never reaches dispatch."""

    key_index: int


@dataclass(frozen=True)
class Outcome:
    result: str
    feedback: bool = False       # brief fault flash on the border (P6)
    press_seen: bool = False     # the pad self-paints on any press (#60)


@dataclass(frozen=True)
class _Pending:
    key_index: int
    local_id: str
    due: float


def parse(message: object) -> Press | None | str:
    """Return a Press, ``None`` for non-input messages, or ``"invalid"``.

    ``"invalid"`` means the message claimed to be HID input but did not carry a
    valid A-key press (unknown key, release-only, malformed params).
    """
    if not isinstance(message, dict) or message.get("m") != "v.oai.hid":
        return None
    params = message.get("p")
    key = params.get("k") if isinstance(params, dict) else None
    action = params.get("act") if isinstance(params, dict) else None
    index = KEY_NAMES.get(key) if type(key) is str else None
    if index is None or action != 1 or isinstance(action, bool):
        return "invalid"
    return Press(key_index=index)


class KeyRouter:
    """Dispatch validated presses through the owner and layer gates."""

    def __init__(self,
                 opener: Callable[..., bool] | None = None,
                 *, double_tap_seconds: float = DOUBLE_TAP_SECONDS) -> None:
        #: opener(local_id, foreground=...) -> bool; injectable for tests.
        self._opener = opener if opener is not None else open_local
        self._window = double_tap_seconds
        self._pending: _Pending | None = None

    def dispatch(self, press: Press, now: float, *, owner: str,
                 layer_one: bool, slots: Sequence[str | None]) -> Outcome:
        """Route one validated press.

        Every outcome here has ``press_seen`` -- the pad lights the whole
        A-zone by itself while a key is down, so any physical press must dirty
        the keys regardless of what the gate decided (#60).
        """
        if owner != "claude":
            self._pending = None
            return Outcome("ignored_owner", press_seen=True)
        if not layer_one:
            self._pending = None
            return Outcome("ignored_layer", press_seen=True)
        local_id = slots[press.key_index] if press.key_index < len(slots) else None
        if local_id is None:
            self._pending = None
            return Outcome("empty_slot", feedback=True, press_seen=True)

        pending = self._pending
        if pending is not None and pending.key_index == press.key_index \
                and now < pending.due:
            self._pending = None
            outcome = self._open(pending.local_id, foreground=True)
            return Outcome(outcome.result, outcome.feedback, press_seen=True)
        # A press on a different key abandons the old pending tap: opening a
        # conversation the user has already moved past would be noise.
        self._pending = _Pending(press.key_index, local_id, now + self._window)
        return Outcome("pending", press_seen=True)

    def flush(self, now: float) -> Outcome | None:
        """Dispatch an expired pending single tap as a background focus."""
        pending = self._pending
        if pending is None or now < pending.due:
            return None
        self._pending = None
        return self._open(pending.local_id, foreground=False)

    def _open(self, local_id: str, *, foreground: bool) -> Outcome:
        try:
            opened = bool(self._opener(local_id, foreground=foreground))
        except Exception:
            opened = False
        if opened:
            result = "opened_foreground" if foreground else "opened_background"
            return Outcome(result)
        return Outcome("open_failed", feedback=True)
