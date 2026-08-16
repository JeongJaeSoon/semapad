from __future__ import annotations

from semapad import view
from semapad.model import PALETTE, AgentState
from semapad.sources.conversations import Conversation
from semapad.sources.hooks import SessionRecord


def conv(lid: str, cli: str | None = None, activity: float = 100.0,
         pinned: bool = False, title: str = "t",
         focused: float = 0.0) -> Conversation:
    return Conversation(local_id=f"local_{lid}", cli_session_id=cli, title=title,
                        cwd="", created_at=1.0, last_activity_at=activity,
                        last_focused_at=focused, pinned=pinned)


def rec(cli: str, state: AgentState, updated_at: float = 100.0) -> SessionRecord:
    return SessionRecord(session_id=cli, cwd="", state=state, rev=1,
                         updated_at=updated_at)


# --- display_state -----------------------------------------------------------

def test_no_process_is_idle_white():
    state, reason = view.display_state(live=False, record=rec("c", AgentState.ERROR),
                                       last_activity_at=50.0, last_focused_at=60.0,
                                       now=100.0, working_max_seconds=900)
    assert state is AgentState.IDLE
    assert reason == "no_process"


def test_live_without_hook_is_idle():
    state, reason = view.display_state(live=True, record=None,
                                       last_activity_at=50.0, last_focused_at=60.0,
                                       now=100.0, working_max_seconds=900)
    assert state is AgentState.IDLE
    assert reason == "no_hook"


def test_working_past_max_demotes_to_idle():
    state, reason = view.display_state(
        live=True, record=rec("c", AgentState.WORKING, updated_at=100.0),
        last_activity_at=50.0, last_focused_at=60.0,
        now=1001.0, working_max_seconds=900)
    assert state is AgentState.IDLE
    assert reason == "working_timeout"


def test_live_hook_state_passes_through():
    state, reason = view.display_state(
        live=True, record=rec("c", AgentState.WAITING),
        last_activity_at=50.0, last_focused_at=60.0, now=100.0,
        working_max_seconds=900)
    assert state is AgentState.WAITING
    assert reason == "state"


def test_unread_without_hooks_is_done_green():
    state, reason = view.display_state(live=False, record=None,
                                       last_activity_at=70.0,
                                       last_focused_at=60.0, now=100.0,
                                       working_max_seconds=900)
    assert state is AgentState.DONE
    assert reason == "unread"


def test_unread_needs_a_recorded_focus():
    # Desktop never recorded a focus: do not guess unread (spec principle 5)
    state, reason = view.display_state(live=False, record=None,
                                       last_activity_at=70.0,
                                       last_focused_at=0.0, now=100.0,
                                       working_max_seconds=900)
    assert state is AgentState.IDLE
    assert reason == "no_process"


def test_hook_state_wins_over_unread():
    state, reason = view.display_state(
        live=True, record=rec("c", AgentState.WORKING),
        last_activity_at=70.0, last_focused_at=60.0, now=100.0,
        working_max_seconds=900)
    assert state is AgentState.WORKING
    assert reason == "state"


def test_live_process_without_hook_still_shows_unread():
    # hooks are optional: a live but hook-less session with unseen activity
    state, reason = view.display_state(live=True, record=None,
                                       last_activity_at=70.0,
                                       last_focused_at=60.0, now=100.0,
                                       working_max_seconds=900)
    assert state is AgentState.DONE
    assert reason == "unread"


# --- busy floor (#17: running background task, no hook signal) ---------------

def test_busy_lifts_done_to_working():
    # The reproduced case: turn ended (Stop -> done) but a background task
    # keeps running -- Desktop says "1 running task", the key must be blue.
    state, reason = view.display_state(
        live=True, record=rec("c", AgentState.DONE),
        last_activity_at=50.0, last_focused_at=60.0, now=100.0,
        working_max_seconds=900, busy=True)
    assert state is AgentState.WORKING
    assert reason == "bg_task"


def test_busy_lifts_hookless_idle_to_working():
    # The originally reported case: resume left the record at idle while a
    # background task was running.
    state, reason = view.display_state(
        live=True, record=None, last_activity_at=50.0, last_focused_at=60.0,
        now=100.0, working_max_seconds=900, busy=True)
    assert state is AgentState.WORKING
    assert reason == "bg_task"


def test_busy_survives_working_timeout():
    # Ageing guards against a lost Stop; a held-open task file is positive
    # evidence of work, so it re-floors the state.
    state, reason = view.display_state(
        live=True, record=rec("c", AgentState.WORKING, updated_at=100.0),
        last_activity_at=50.0, last_focused_at=60.0,
        now=1001.0, working_max_seconds=900, busy=True)
    assert state is AgentState.WORKING
    assert reason == "bg_task"


def test_busy_never_hides_waiting_or_error():
    for blocked in (AgentState.WAITING, AgentState.ERROR):
        state, reason = view.display_state(
            live=True, record=rec("c", blocked),
            last_activity_at=50.0, last_focused_at=60.0, now=100.0,
            working_max_seconds=900, busy=True)
        assert state is blocked
        assert reason == "state"


def test_busy_without_live_process_stays_idle():
    # An orphaned shell can outlive its session; a dead session is not working.
    state, reason = view.display_state(
        live=False, record=None, last_activity_at=50.0, last_focused_at=60.0,
        now=100.0, working_max_seconds=900, busy=True)
    assert state is AgentState.IDLE
    assert reason == "no_process"


def test_build_threads_busy_ids_to_display_state():
    built = view.build(
        conversations=[conv("a", cli="cli-a", focused=60.0)],
        live_cli_ids={"cli-a"},
        records=[rec("cli-a", AgentState.DONE)],
        prev_slots=None, colors=PALETTE, working_max_seconds=900, now=100.0,
        busy_cli_ids=frozenset({"cli-a"}))
    slot = built.slots[0]
    assert slot.state == "working"
    assert slot.reason == "bg_task"
    assert slot.color == PALETTE[AgentState.WORKING]


# --- assign ------------------------------------------------------------------

def test_startup_orders_by_activity_desc():
    slots = view.assign(None, ["a", "b", "c"], {"a", "b", "c"},
                        {"a": 1.0, "b": 3.0, "c": 2.0})
    assert slots == ("b", "c", "a", None, None, None)


def test_activity_change_does_not_reorder():
    prev = ("a", "b", None, None, None, None)
    slots = view.assign(prev, ["a", "b"], {"a", "b"}, {"a": 1.0, "b": 999.0})
    assert slots == prev


def test_archive_compacts_holes():
    prev = ("a", "b", "c", None, None, None)
    slots = view.assign(prev, ["a", "c"], {"a", "c"}, {"a": 1.0, "c": 2.0})
    assert slots == ("a", "c", None, None, None, None)


def test_new_conversation_takes_first_empty_key():
    prev = ("a", None, "b", None, None, None)
    slots = view.assign(prev, ["a", "b", "n"], {"a", "b", "n"},
                        {"a": 1.0, "b": 2.0, "n": 3.0})
    # compaction closes the hole first (lifecycle rule), then n appends
    assert slots == ("a", "b", "n", None, None, None)


def test_bumped_conversation_is_replaced_in_place():
    prev = ("a", "b", "c", "d", "e", "f")
    selected = ["a", "b", "n", "d", "e", "f"]        # c lost selection to n
    existing = {"a", "b", "c", "d", "e", "f", "n"}
    slots = view.assign(prev, selected, existing,
                        {i: 1.0 for i in existing})
    assert slots == ("a", "b", "n", "d", "e", "f")


def test_archive_plus_new_conversation_same_tick():
    prev = ("a", "b", "c", None, None, None)
    # b archived (gone from existing), n new
    slots = view.assign(prev, ["a", "c", "n"], {"a", "c", "n"},
                        {"a": 1.0, "c": 2.0, "n": 3.0})
    assert slots == ("a", "c", "n", None, None, None)


# --- selection ---------------------------------------------------------------

def _build(convs, procs_alive: set[str], records, prev=None, now=1000.0,
           colors=None):
    return view.build(
        conversations=convs,
        live_cli_ids=procs_alive,
        records=records,
        prev_slots=prev,
        colors=colors or dict(PALETTE),
        working_max_seconds=900,
        now=now,
    )


def test_selection_prefers_pinned_live_and_state():
    convs = [
        conv("p", cli="cp", activity=1.0, pinned=True),
        conv("w", cli="cw", activity=2.0),   # live waiting
        conv("e", cli="ce", activity=3.0),   # live error
        conv("k", cli="ck", activity=4.0),   # live working
        conv("i", cli="ci", activity=5.0),   # live idle
        conv("d", activity=6.0),             # dead, recent
        conv("x", activity=0.5),             # dead, old -> bumped
    ]
    records = [rec("cw", AgentState.WAITING), rec("ce", AgentState.ERROR),
               rec("ck", AgentState.WORKING)]
    v = _build(convs, {"cp", "cw", "ce", "ck", "ci"}, records)
    on_keys = {s.local_id for s in v.slots if s.local_id}
    assert on_keys == {"local_p", "local_w", "local_e", "local_k",
                       "local_i", "local_d"}
    bumped = [c for c in v.conversations if c.key is None]
    assert [c.local_id for c in bumped] == ["local_x"]


def test_view_slot_colors_come_from_palette():
    convs = [conv("w", cli="cw", activity=2.0)]
    v = _build(convs, {"cw"}, [rec("cw", AgentState.WAITING)])
    slot = v.slots[0]
    assert slot.state == "waiting"
    assert slot.color == PALETTE[AgentState.WAITING]
    assert v.slots[1].local_id is None
    assert v.slots[1].color is None
    assert v.slots[1].reason == "empty"


def test_custom_colors_flow_into_slots():
    colors = dict(PALETTE) | {AgentState.IDLE: 0x123456}
    v = _build([conv("a", activity=1.0)], set(), [], colors=colors)
    assert v.slots[0].color == 0x123456
    assert v.palette["idle"] == 0x123456


def test_alert_only_for_bumped_notable_states():
    convs = [conv(str(i), cli=f"c{i}", activity=float(i)) for i in range(7)]
    live = {f"c{i}" for i in range(7)}
    # seven waiting conversations: the oldest is bumped off the keys -> alert
    records = [rec(f"c{i}", AgentState.WAITING) for i in range(7)]
    v = _build(convs, live, records)
    assert v.alert == "alert"
    # a single waiting conversation wins a key by selection -> no alert
    records = [rec("c6", AgentState.WAITING)]
    v = _build(convs, live, records)
    assert v.alert == "normal"


def test_conversations_ordered_by_activity_desc():
    v = _build([conv("a", activity=1.0), conv("b", activity=9.0)], set(), [])
    assert [c.local_id for c in v.conversations] == ["local_b", "local_a"]


def test_deeplink_url_for_canonical_ids_only():
    v = _build([conv("00000000-0000-4000-8000-000000000000", activity=2.0),
                conv("not-a-uuid", activity=1.0)], set(), [])
    assert v.slots[0].deeplink_url == \
        "claude://claude.ai/epitaxy/local_00000000-0000-4000-8000-000000000000"
    # a non-canonical id cannot be routed; the dashboard shows the failure
    assert v.slots[1].deeplink_url is None


def test_snapshot_is_json_ready_and_versioned():
    v = _build([conv("a", cli="ca", activity=1.0)], {"ca"},
               [rec("ca", AgentState.DONE)])
    snap = view.snapshot(v, config_fingerprint="abc", generated_at=5.0)
    assert snap["schema"] == view.SNAPSHOT_SCHEMA
    assert snap["config_fingerprint"] == "abc"
    assert snap["slots"][0]["color"] == PALETTE[AgentState.DONE]
    import json
    json.dumps(snap)


def test_debouncer_holds_a_keyed_conversation_through_one_missed_scan():
    """A transient unreadable mapping file must not move keys (spec §5: keys
    move on lifecycle events only). One missed scan is indistinguishable from
    a Desktop rewrite race, so departure needs two consecutive misses."""
    a, b = conv("a" * 36), conv("b" * 36)
    deb = view.DepartureDebouncer()

    both = deb.apply((a, b), prev_slots=(a.local_id, b.local_id, None, None, None, None))
    assert {c.local_id for c in both} == {a.local_id, b.local_id}

    # scan race: `a` vanishes for one poll -- held with last-known data
    held = deb.apply((b,), prev_slots=(a.local_id, b.local_id, None, None, None, None))
    assert {c.local_id for c in held} == {a.local_id, b.local_id}

    # second consecutive miss: now it really departs
    gone = deb.apply((b,), prev_slots=(a.local_id, b.local_id, None, None, None, None))
    assert {c.local_id for c in gone} == {b.local_id}


def test_debouncer_reappearance_clears_the_miss_streak():
    a, b = conv("a" * 36), conv("b" * 36)
    deb = view.DepartureDebouncer()
    slots = (a.local_id, b.local_id, None, None, None, None)
    deb.apply((a, b), prev_slots=slots)
    deb.apply((b,), prev_slots=slots)          # miss 1
    deb.apply((a, b), prev_slots=slots)        # back -- streak resets
    held = deb.apply((b,), prev_slots=slots)   # miss 1 again, still held
    assert {c.local_id for c in held} == {a.local_id, b.local_id}


def test_debouncer_ignores_conversations_that_hold_no_key():
    a, b = conv("a" * 36), conv("b" * 36)
    deb = view.DepartureDebouncer()
    deb.apply((a, b), prev_slots=(a.local_id, None, None, None, None, None))
    # `b` never had a key: its disappearance is not held
    out = deb.apply((a,), prev_slots=(a.local_id, None, None, None, None, None))
    assert {c.local_id for c in out} == {a.local_id}
