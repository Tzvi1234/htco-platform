"""Compatibility smoke test: the pinned LangGraph runs on our Python (assumption A-10).

If this fails on Python 3.14, the fallback is Python 3.13 by ADR before any agent code exists
(Phase 1 risks).
"""

import sys
from typing import TypedDict

import pytest
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph


class _SmokeState(TypedDict):
    steps: list[str]


def _first(state: _SmokeState) -> _SmokeState:
    return {"steps": [*state["steps"], "first"]}


def _second(state: _SmokeState) -> _SmokeState:
    return {"steps": [*state["steps"], "second"]}


@pytest.mark.ac("AC-1.1.3")
def test_runtime__python_version__is_3_14() -> None:
    assert sys.version_info[:2] == (3, 14)


@pytest.mark.ac("AC-1.1.3")
def test_langgraph__two_node_graph_with_memory_checkpointer__runs_and_checkpoints() -> None:
    builder = StateGraph(_SmokeState)
    builder.add_node("first", _first)
    builder.add_node("second", _second)
    builder.add_edge(START, "first")
    builder.add_edge("first", "second")
    builder.add_edge("second", END)
    graph = builder.compile(checkpointer=InMemorySaver())
    config: RunnableConfig = {"configurable": {"thread_id": "smoke-1"}}

    result = graph.invoke({"steps": []}, config)

    assert result == {"steps": ["first", "second"]}
    snapshot = graph.get_state(config)
    assert snapshot.values == {"steps": ["first", "second"]}
    assert snapshot.next == ()
