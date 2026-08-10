"""Join + selection + colour, the single source of truth (spec §5.1).

Pure: no files, no hardware, no clock reads. The web UI (Phase 1-2) and the
surfaces/compositor (Phase 3) both consume this module's output; neither may
recompute colour on its own (#57 lesson, spec principle 9).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping

from semapad import deeplink
from semapad.model import AgentState
from semapad.sources.conversations import Conversation
from semapad.sources.hooks import SessionRecord

KEY_COUNT = 6
SNAPSHOT_SCHEMA = 1

#: Selection tiers within equal pinned/live rank (spec §5). DONE and IDLE are
#: deliberately bottom: neither demands the human's attention.
_SELECT_PRIORITY = {
    AgentState.WAITING: 3,
    AgentState.ERROR: 2,
    AgentState.WORKING: 1,
}

#: Border-worthy states: a bumped conversation in one of these raises alert.
_NOTABLE = (AgentState.WAITING, AgentState.ERROR)


@dataclass(frozen=True)
class SlotView:
    key: int
    local_id: str | None
    title: str
    state: str | None          # display state value; None for an empty key
    reason: str                # empty | no_process | no_hook | working_timeout | state
    color: int | None          # what the daemon will actually light; None = off
    process_alive: bool
    deeplink_url: str | None


@dataclass(frozen=True)
class ConversationRow:
    local_id: str
    title: str
    key: int | None            # which key it is on, None if bumped
    state: str
    reason: str
    process_alive: bool
    last_activity_at: float
    pinned: bool
    deeplink_url: str | None


@dataclass(frozen=True)
class View:
    slots: tuple[SlotView, ...]
    conversations: tuple[ConversationRow, ...]
    alert: str                 # alert | normal
    palette: dict[str, int]
    diagnostics: tuple[str, ...]


def display_state(*, live: bool, record: SessionRecord | None,
                  last_activity_at: float, last_focused_at: float, now: float,
                  working_max_seconds: float) -> tuple[AgentState, str]:
    """The colour-deciding state of one conversation, with its reason.

    Hooks are an optional enrichment layer (spec §4 principle 4): with a live
    process and a hook record, the hook wins; a missing Stop event must not
    leave a key blue forever, so WORKING ages into idle -- a recolour, not a
    turn-off. Without hook state, the unread approximation (spec §5,
    ``lastActivityAt > lastFocusedAt``) shows unseen activity as DONE green,
    the vendor's unread meaning. A conversation Desktop has never recorded a
    focus for stays idle rather than guessed unread (spec principle 5).
    """
    if live and record is not None:
        if (record.state is AgentState.WORKING
                and max(0.0, now - record.updated_at) >= working_max_seconds):
            return AgentState.IDLE, "working_timeout"
        return record.state, "state"
    if 0.0 < last_focused_at < last_activity_at:
        return AgentState.DONE, "unread"
    if not live:
        return AgentState.IDLE, "no_process"
    return AgentState.IDLE, "no_hook"


def assign(prev: tuple[str | None, ...] | None, selected: list[str],
           existing: set[str],
           activity: Mapping[str, float]) -> tuple[str | None, ...]:
    """Place selected conversations on the six keys (spec §5, §11.8-9).

    Startup (``prev is None``): one-time sort by activity, newest first.
    After that keys move only on lifecycle events: an archived conversation's
    departure pulls later keys forward (no holes), a newly selected
    conversation takes the frontmost empty key, and when the keys are full it
    replaces -- in place -- the occupant it bumped out of selection. Activity
    never reorders keys.
    """
    if prev is None:
        ordered = sorted(selected, key=lambda i: -activity.get(i, 0.0))
        chosen = ordered[:KEY_COUNT]
        return tuple(chosen + [None] * (KEY_COUNT - len(chosen)))

    # Lifecycle compaction: drop conversations that left the list entirely.
    kept = [lid for lid in prev if lid is not None and lid in existing]
    slots: list[str | None] = kept + [None] * (KEY_COUNT - len(kept))

    selected_set = set(selected)
    unplaced = [lid for lid in selected if lid not in slots]
    unplaced.sort(key=lambda i: -activity.get(i, 0.0))
    for lid in unplaced:
        if None in slots:
            slots[slots.index(None)] = lid
            continue
        for index, occupant in enumerate(slots):
            if occupant not in selected_set:
                slots[index] = lid
                break
    return tuple(slots)


class DepartureDebouncer:
    """Hold a keyed conversation through one missed scan (spec §5).

    A Desktop mapping-file rewrite can race our poll: the file reads as
    invalid for one scan, the conversation vanishes, compaction moves every
    later key, and the reappearing conversation lands on a different key --
    a sticky-contract violation observed on real hardware (2026-08-10).
    Departure therefore requires two consecutive misses; provable archival
    still departs, one poll later. Only conversations holding a key are held.
    """

    #: consecutive misses before a keyed conversation really departs
    MAX_MISSES = 2

    def __init__(self) -> None:
        self._cache: dict[str, Conversation] = {}
        self._misses: dict[str, int] = {}

    def apply(self, conversations: tuple[Conversation, ...],
              *, prev_slots: tuple[str | None, ...] | None,
              ) -> tuple[Conversation, ...]:
        present = {c.local_id for c in conversations}
        out = list(conversations)
        for lid in (prev_slots or ()):
            if lid is None or lid in present:
                continue
            streak = self._misses.get(lid, 0) + 1
            if streak < self.MAX_MISSES and lid in self._cache:
                self._misses[lid] = streak
                out.append(self._cache[lid])
            else:
                self._misses.pop(lid, None)
                self._cache.pop(lid, None)
        keyed = set(prev_slots or ()) | present
        for c in conversations:
            if c.local_id in keyed:
                self._cache[c.local_id] = c
            self._misses.pop(c.local_id, None)
        # drop cache entries for conversations that no longer hold a key
        for lid in list(self._cache):
            if lid not in (prev_slots or ()) and lid not in present:
                self._cache.pop(lid, None)
                self._misses.pop(lid, None)
        return tuple(out)


def _url(local_id: str) -> str | None:
    try:
        return deeplink.url_for(local_id)
    except ValueError:
        return None


def build(*, conversations: Iterable[Conversation], live_cli_ids: set[str],
          records: Iterable[SessionRecord],
          prev_slots: tuple[str | None, ...] | None,
          colors: Mapping[AgentState, int], working_max_seconds: float,
          now: float, diagnostics: tuple[str, ...] = ()) -> View:
    convs = sorted(conversations, key=lambda c: -c.last_activity_at)
    by_cli = {r.session_id: r for r in records}

    computed: dict[str, tuple[Conversation, AgentState, str, bool]] = {}
    for c in convs:
        live = c.cli_session_id in live_cli_ids if c.cli_session_id else False
        record = by_cli.get(c.cli_session_id) if c.cli_session_id else None
        state, reason = display_state(live=live, record=record,
                                      last_activity_at=c.last_activity_at,
                                      last_focused_at=c.last_focused_at,
                                      now=now,
                                      working_max_seconds=working_max_seconds)
        computed[c.local_id] = (c, state, reason, live)

    ranked = sorted(
        computed.values(),
        key=lambda item: (item[0].pinned, item[3],
                          _SELECT_PRIORITY.get(item[1], 0),
                          item[0].last_activity_at),
        reverse=True,
    )
    selected = [item[0].local_id for item in ranked[:KEY_COUNT]]
    slots = assign(prev_slots, selected,
                   set(computed), {c.local_id: c.last_activity_at for c in convs})

    slot_views = []
    for key, lid in enumerate(slots):
        if lid is None:
            slot_views.append(SlotView(key=key, local_id=None, title="",
                                       state=None, reason="empty", color=None,
                                       process_alive=False, deeplink_url=None))
            continue
        c, state, reason, live = computed[lid]
        slot_views.append(SlotView(
            key=key, local_id=lid, title=c.title, state=state.value,
            reason=reason, color=colors[state], process_alive=live,
            deeplink_url=_url(lid)))

    key_of = {lid: key for key, lid in enumerate(slots) if lid}
    rows = tuple(ConversationRow(
        local_id=c.local_id, title=c.title, key=key_of.get(c.local_id),
        state=state.value, reason=reason, process_alive=live,
        last_activity_at=c.last_activity_at, pinned=c.pinned,
        deeplink_url=_url(c.local_id))
        for c, state, reason, live in (computed[c.local_id] for c in convs))

    alert = "alert" if any(
        row.key is None and row.state in tuple(s.value for s in _NOTABLE)
        for row in rows) else "normal"

    return View(slots=tuple(slot_views), conversations=rows, alert=alert,
                palette={state.value: colors[state] for state in AgentState},
                diagnostics=tuple(diagnostics))


def snapshot(view: View, *, config_fingerprint: str,
             generated_at: float) -> dict:
    """The finished-value snapshot every consumer draws from (spec §5.1)."""
    return {
        "schema": SNAPSHOT_SCHEMA,
        "generated_at": generated_at,
        "config_fingerprint": config_fingerprint,
        "alert": view.alert,
        "palette": view.palette,
        "diagnostics": list(view.diagnostics),
        "slots": [{
            "key": s.key, "local_id": s.local_id, "title": s.title,
            "state": s.state, "reason": s.reason, "color": s.color,
            "process_alive": s.process_alive, "deeplink_url": s.deeplink_url,
        } for s in view.slots],
        "conversations": [{
            "local_id": c.local_id, "title": c.title, "key": c.key,
            "state": c.state, "reason": c.reason,
            "process_alive": c.process_alive,
            "last_activity_at": c.last_activity_at, "pinned": c.pinned,
            "deeplink_url": c.deeplink_url,
        } for c in view.conversations],
    }
