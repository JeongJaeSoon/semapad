from __future__ import annotations

import pytest

from semapad import input as input_module
from semapad.input import KeyRouter, Outcome, Press, parse


def hid(key: str = "AG00", act: object = 1) -> dict:
    return {"m": "v.oai.hid", "p": {"k": key, "act": act}}


SLOTS = ("local_a", "local_b", None, None, None, None)


def router(opened: list | None = None, ok: bool = True) -> KeyRouter:
    def opener(local_id: str) -> bool:
        if opened is not None:
            opened.append(local_id)
        return ok
    return KeyRouter(opener)


# --- async open results ------------------------------------------------------

def test_drain_open_failures_reports_then_empties():
    """The press cannot wait for LaunchServices, so a failed open surfaces here
    or nowhere -- and it must not be reported twice."""
    input_module.note_open_failure("claude://x/local_a", -10814)
    assert input_module.drain_open_failures() == [("claude://x/local_a", -10814)]
    assert input_module.drain_open_failures() == []


def test_drain_open_failures_is_bounded():
    """A wedged LaunchServices must not turn one tick into thousands of lines."""
    for index in range(40):
        input_module.note_open_failure(f"claude://x/{index}", -1)
    drained = input_module.drain_open_failures(limit=8)
    assert len(drained) == 8
    input_module.drain_open_failures(limit=100)   # leave the queue clean


# --- parse -------------------------------------------------------------------

def test_parse_non_input_messages_are_none():
    assert parse({"m": "v.oai.thstatus", "id": 3}) is None
    assert parse("nonsense") is None


@pytest.mark.parametrize("message", [
    hid(key="AG09"), hid(key="XX00"), hid(act="1"), hid(act=True),
    {"m": "v.oai.hid"}, {"m": "v.oai.hid", "p": []},
])
def test_parse_rejects_invalid_input_shapes(message):
    assert parse(message) == "invalid"


def test_parse_release_is_its_own_disposition():
    """A release must never be classified as noise: the daemon drops it
    silently so it cannot overwrite the last meaningful press in the ui."""
    assert parse(hid(act=0)) == "release"


def test_parse_valid_press():
    assert parse(hid("AG05")) == Press(key_index=5)


# --- gates -------------------------------------------------------------------

def test_owner_and_layer_gates_close_before_slots():
    r = router()
    assert r.dispatch(Press(0), 0.0, owner="codex", layer_one=True,
                      slots=SLOTS) == Outcome("ignored_owner", press_seen=True)
    assert r.dispatch(Press(0), 0.0, owner="claude", layer_one=False,
                      slots=SLOTS) == Outcome("ignored_layer", press_seen=True)


def test_empty_slot_gives_feedback():
    r = router()
    outcome = r.dispatch(Press(2), 0.0, owner="claude", layer_one=True,
                         slots=SLOTS)
    assert outcome == Outcome("empty_slot", feedback=True, press_seen=True)


# --- tap dispatch (single action: open immediately, foreground) --------------
# The vendor's background/double-tap split was dropped after acceptance
# (2026-08-10): claude:// makes Desktop raise itself, so the 350 ms tap window
# was pure lag. Distinguishing taps again is semapad#12.

def test_press_opens_immediately():
    opened: list = []
    r = router(opened)
    outcome = r.dispatch(Press(0), 10.0, owner="claude", layer_one=True,
                         slots=SLOTS)
    assert outcome == Outcome("opened", press_seen=True)
    assert opened == ["local_a"]


def test_two_presses_open_twice_without_any_window():
    opened: list = []
    r = router(opened)
    r.dispatch(Press(0), 10.0, owner="claude", layer_one=True, slots=SLOTS)
    r.dispatch(Press(1), 10.1, owner="claude", layer_one=True, slots=SLOTS)
    assert opened == ["local_a", "local_b"]


def test_open_failure_gives_feedback():
    r = router(ok=False)
    outcome = r.dispatch(Press(0), 10.0, owner="claude", layer_one=True,
                         slots=SLOTS)
    assert outcome == Outcome("open_failed", feedback=True, press_seen=True)


def test_opener_exception_is_a_failed_open():
    def boom(local_id: str) -> bool:
        raise RuntimeError("no")
    r = KeyRouter(boom)
    outcome = r.dispatch(Press(0), 10.0, owner="claude", layer_one=True,
                         slots=SLOTS)
    assert outcome == Outcome("open_failed", feedback=True, press_seen=True)


# --- open_local --------------------------------------------------------------

def test_open_local_builds_the_epitaxy_route_and_does_not_wait():
    calls: list = []

    class Child:
        def poll(self):
            return 0    # exited; the reaper may drop it

    def spawner(command, **kwargs):
        calls.append(command)
        return Child()

    lid = "local_00000000-0000-4000-8000-000000000000"
    assert input_module.open_local(lid, spawner=spawner)
    assert calls == [["/usr/bin/open", f"claude://claude.ai/epitaxy/{lid}"]]


def test_open_local_refuses_invalid_ids():
    assert input_module.open_local("not-a-local-id") is False


def test_injected_spawner_bypasses_the_ls_worker():
    """Tests and fallbacks use the Popen path; the LS worker is production-only
    (keyed on the spawner being the real subprocess.Popen)."""
    calls: list = []

    class Child:
        def poll(self):
            return 0

    def spawner(command, **kwargs):
        calls.append(command)
        return Child()

    lid = "local_00000000-0000-4000-8000-000000000000"
    assert input_module.open_local(lid, spawner=spawner)
    assert calls and input_module._ls_opener is None
