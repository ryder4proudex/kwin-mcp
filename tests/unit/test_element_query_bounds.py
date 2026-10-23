"""element_query walks are bounded by node count and time, not by depth.

Web apps in Chrome put their controls around depth 20 (a Lark document's
buttons were at depth 20-26), below the former fixed depth limit of 15.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from kwin_mcp import accessibility
from kwin_mcp.accessibility import _WalkLimits
from kwin_mcp.models import DEFAULT_MAX_NODES, ElementList, Locator, Wait

from ._atspi_tree import Node, install, kwin_window

if TYPE_CHECKING:
    from ._fakes import FakeEngine

PID = 77


def _chain(depth: int) -> Node:
    """An application whose frame holds a chain of panels ending in a button."""
    node = Node("Deep", "push button", (10, 10, 20, 20), actions=["press"])
    for _ in range(depth - 2):
        node = Node("", "panel", (0, 0, 400, 300), children=[node])
    frame = Node("Deep app", "frame", (0, 0, 400, 300), children=[node])
    return Node("deepapp", "application", children=[frame], pid=PID)


@pytest.fixture
def deep(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, [_chain(30)], [kwin_window(PID, "w1", "Deep app", [100, 50, 400, 300])])


def _request(op: str, **fields: Any) -> dict[str, Any]:
    response = accessibility._handle_request({"op": op, **fields})
    assert response["ok"] is True, response
    return response


def test_default_walk_reaches_any_depth(deep: None) -> None:
    del deep
    response = _request("query", locator={"query": "Deep", "role": "push button"})

    (button,) = response["result"]
    assert button["depth"] == 30
    assert (button["x"], button["y"], button["mapped"]) == (110, 60, True)
    assert response["walk"] == {"visited": 31, "truncated": ""}


def test_explicit_max_depth_still_cuts_the_walk(deep: None) -> None:
    del deep
    response = _request("query", locator={"role": "push button"}, max_depth=15)

    assert response["result"] == []
    # A depth limit the caller chose is not a truncation.
    assert response["walk"] == {"visited": 16, "truncated": ""}


def test_max_nodes_truncates_and_says_so(deep: None) -> None:
    del deep
    response = _request("query", locator={}, max_nodes=10)

    assert len(response["result"]) == 10
    assert response["walk"] == {"visited": 10, "truncated": "max_nodes"}
    # 0 lifts the cap.
    assert _request("query", locator={}, max_nodes=0)["walk"]["truncated"] == ""


def test_time_budget_truncates() -> None:
    limits = _WalkLimits(time_budget_s=0.0)
    limits.start()

    assert limits.admit() is False
    assert limits.truncated == "time"


def test_a_tree_that_loops_back_ends_at_the_node_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    loop = Node("loop", "panel", (0, 0, 10, 10))
    loop.children = [loop]
    app = Node("loopy", "application", children=[loop], pid=PID)
    install(monkeypatch, [app], [])

    response = _request("query", locator={}, max_nodes=2000)

    assert response["walk"] == {"visited": 2000, "truncated": "max_nodes"}


def test_a_truncated_walk_cannot_prove_absence(deep: None) -> None:
    del deep
    common = {"until": "absent", "timeout_ms": 0, "poll_interval_ms": 0}
    truncated = _request("wait", locator={"query": "Deep"}, max_nodes=5, **common)["result"]
    assert truncated["satisfied"] is False
    assert truncated["walk"] == {"visited": 5, "truncated": "max_nodes"}

    complete = _request("wait", locator={"query": "Nowhere"}, **common)["result"]
    assert complete["satisfied"] is True
    assert complete["walk"]["truncated"] == ""


def _serve(fake_engine: FakeEngine) -> None:
    for op in ("query", "wait"):
        fake_engine.atspi.respond(
            op, lambda arguments, op=op: accessibility._handle_request({"op": op, **arguments})
        )


def test_engine_defaults_and_truncation_text(deep: None, fake_engine: FakeEngine) -> None:
    del deep
    _serve(fake_engine)

    found = fake_engine.engine.element_query(locator=Locator(query="Deep", role="push button"))
    assert fake_engine.atspi.calls[-1].kwargs["max_depth"] is None
    assert fake_engine.atspi.calls[-1].kwargs["max_nodes"] == DEFAULT_MAX_NODES
    assert [e.depth for e in found.elements] == [30]
    assert (found.visited, found.truncated) == (31, None)
    assert "TRUNCATED" not in found.render()

    cut = fake_engine.engine.element_query(max_nodes=7)
    assert (cut.visited, cut.truncated, len(cut.elements)) == (7, "max_nodes", 7)
    assert cut.render().splitlines()[-1] == (
        "TRUNCATED: the walk stopped at max_nodes=7 after 7 nodes; "
        "narrow locator.app or max_depth, or raise max_nodes."
    )
    assert ElementList.model_validate(cut.model_dump()).render() == cut.render()

    waited = fake_engine.engine.element_query(
        locator=Locator(query="Deep"), wait=Wait(until="absent", timeout_ms=0), max_nodes=3
    )
    assert waited.wait is not None
    assert waited.wait.satisfied is False
    assert waited.truncated == "max_nodes"
