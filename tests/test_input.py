from __future__ import annotations

import pytest

from semapad import input as input_module
from semapad.input import KeyRouter, Outcome, Press, parse


def hid(key: str = "AG00", act: object = 1) -> dict:
    return {"m": "v.oai.hid", "p": {"k": key, "act": act}}


SLOTS = ("local_a", "local_b", None, None, None, None)


def router(opened: list | None = None, ok: bool = True) -> KeyRouter:
    def opener(local_id: str, *, foreground: bool) -> bool:
        if opened is not None:
            opened.append((local_id, foreground))
        return ok
    return KeyRouter(opener, double_tap_seconds=0.35)


# --- parse -------------------------------------------------------------------

def test_parse_non_input_messages_are_none():
    assert parse({"m": "v.oai.thstatus", "id": 3}) is None
    assert parse("nonsense") is None


@pytest.mark.parametrize("message", [
    hid(key="AG09"), hid(key="XX00"), hid(act=0), hid(act="1"), hid(act=True),
    {"m": "v.oai.hid"}, {"m": "v.oai.hid", "p": []},
])
def test_parse_rejects_invalid_input_shapes(message):
    assert parse(message) == "invalid"


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


# --- tap semantics (spec §9: vendor style) ----------------------------------

def test_single_tap_opens_in_background_after_window():
    opened: list = []
    r = router(opened)
    outcome = r.dispatch(Press(0), 10.0, owner="claude", layer_one=True,
                         slots=SLOTS)
    assert outcome.result == "pending" and outcome.press_seen
    assert r.flush(10.2) is None                     # window still open
    flushed = r.flush(10.4)
    assert flushed == Outcome("opened_background")
    assert opened == [("local_a", False)]


def test_double_tap_opens_foreground_immediately():
    opened: list = []
    r = router(opened)
    r.dispatch(Press(0), 10.0, owner="claude", layer_one=True, slots=SLOTS)
    outcome = r.dispatch(Press(0), 10.2, owner="claude", layer_one=True,
                         slots=SLOTS)
    assert outcome.result == "opened_foreground" and outcome.press_seen
    assert opened == [("local_a", True)]
    assert r.flush(11.0) is None                     # nothing left pending


def test_press_on_another_key_replaces_the_pending_tap():
    opened: list = []
    r = router(opened)
    r.dispatch(Press(0), 10.0, owner="claude", layer_one=True, slots=SLOTS)
    outcome = r.dispatch(Press(1), 10.1, owner="claude", layer_one=True,
                         slots=SLOTS)
    assert outcome.result == "pending"
    assert r.flush(10.5) == Outcome("opened_background")
    assert opened == [("local_b", False)]            # only the newer key


def test_open_failure_gives_feedback():
    r = router(ok=False)
    r.dispatch(Press(0), 10.0, owner="claude", layer_one=True, slots=SLOTS)
    assert r.flush(10.4) == Outcome("open_failed", feedback=True)


def test_opener_exception_is_a_failed_open():
    def boom(local_id: str, *, foreground: bool) -> bool:
        raise RuntimeError("no")
    r = KeyRouter(boom)
    r.dispatch(Press(0), 10.0, owner="claude", layer_one=True, slots=SLOTS)
    assert r.flush(10.4) == Outcome("open_failed", feedback=True)


# --- open_local --------------------------------------------------------------

def test_open_local_builds_the_epitaxy_route():
    calls: list = []

    class Done:
        returncode = 0

    def runner(command, **kwargs):
        calls.append(command)
        return Done()

    lid = "local_00000000-0000-4000-8000-000000000000"
    assert input_module.open_local(lid, foreground=False, runner=runner)
    assert calls == [["/usr/bin/open", "-g",
                      f"claude://claude.ai/epitaxy/{lid}"]]
    assert input_module.open_local(lid, foreground=True, runner=runner)
    assert calls[1][1] != "-g"


def test_open_local_refuses_invalid_ids():
    assert input_module.open_local("not-a-local-id", foreground=True) is False
