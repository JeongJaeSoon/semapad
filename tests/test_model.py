import pytest
from semapad.model import AgentState, highest


def test_priority_order_waiting_beats_error():
    assert highest([AgentState.ERROR, AgentState.WAITING]) is AgentState.WAITING


def test_priority_order_full_chain():
    every = [AgentState.IDLE, AgentState.WORKING, AgentState.DONE,
             AgentState.ERROR, AgentState.WAITING]
    assert highest(every) is AgentState.WAITING
    assert highest([AgentState.IDLE, AgentState.WORKING]) is AgentState.WORKING
    assert highest([AgentState.IDLE, AgentState.DONE]) is AgentState.DONE


def test_empty_is_none():
    assert highest([]) is None


def test_single():
    assert highest([AgentState.IDLE]) is AgentState.IDLE
