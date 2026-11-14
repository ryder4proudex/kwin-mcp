"""element_query says when no element in scope has the requested role.

``role`` is an exact AT-SPI role name, and toolkits report e.g. "push button"
where an agent may guess "button". When nothing matches and no element in the
walk's scope has that role at all, the result names the closest roles seen.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from kwin_mcp import accessibility
from kwin_mcp.models import ElementList, Locator

from ._atspi_tree import Node, install, kwin_window

if TYPE_CHECKING:
    from kwin_mcp.geometry import KWinWindow

APP = 70
WINDOWS: list[KWinWindow] = [kwin_window(APP, "w-calc", "Calculator", [0, 0, 400, 500])]


@pytest.fixture(autouse=True)
def desktop(monkeypatch: pytest.MonkeyPatch) -> None:
    frame = Node(
        "Calculator",
        "frame",
        (0, 0, 400, 500),
        children=[
            Node("7", "push button", (10, 10, 40, 40), actions=["Press"]),
            Node("Mode", "toggle button", (60, 10, 40, 40)),
            Node("Display", "text", (10, 60, 380, 40)),
        ],
    )
    install(monkeypatch, [Node("calc", "application", children=[frame], pid=APP)], WINDOWS)


def _walk(locator: dict[str, Any]) -> dict[str, Any]:
    response = accessibility._handle_request({"op": "query", "locator": locator})
    assert response["ok"] is True
    return response


def test_unknown_role_lists_close_roles_seen() -> None:
    response = _walk({"role": "button"})
    assert response["result"] == []
    assert response["walk"]["role_suggestions"] == ["push button", "toggle button"]


def test_role_without_any_close_match_gets_an_empty_list() -> None:
    assert _walk({"role": "spreadsheet"})["walk"]["role_suggestions"] == []


def test_known_role_or_a_match_gets_no_suggestions() -> None:
    assert "role_suggestions" not in _walk({"role": "push button", "query": "nothing"})["walk"]
    assert "role_suggestions" not in _walk({"role": "Push Button"})["walk"]
    assert "role_suggestions" not in _walk({"query": "nothing"})["walk"]


def test_incomplete_walk_proves_nothing() -> None:
    response = accessibility._handle_request(
        {"op": "query", "locator": {"role": "button"}, "max_depth": 1}
    )
    assert "role_suggestions" not in response["walk"]
    response = accessibility._handle_request(
        {"op": "query", "locator": {"role": "button"}, "max_nodes": 2}
    )
    assert response["walk"]["truncated"] == "max_nodes"
    assert "role_suggestions" not in response["walk"]


class _Unreadable(Node):
    def get_role_name(self) -> str:
        raise RuntimeError("org.freedesktop.DBus.Error.NoReply")


class _NoChildren(Node):
    def get_child_at_index(self, index: int) -> Node:
        raise RuntimeError("org.freedesktop.DBus.Error.NoReply")


@pytest.mark.parametrize("kind", ["node", "child"])
def test_walk_that_skipped_unreadable_nodes_proves_nothing(
    monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    # The unreadable node may be the very element with the role.
    if kind == "node":
        frame = Node("Calculator", "frame", (0, 0, 400, 500), children=[_Unreadable("7")])
    else:
        frame = _NoChildren("Calculator", "frame", (0, 0, 400, 500), children=[Node("7")])
    install(monkeypatch, [Node("calc", "application", children=[frame], pid=APP)], WINDOWS)

    response = _walk({"role": "button"})

    assert response["result"] == []
    assert "role_suggestions" not in response["walk"]


def test_wait_reports_suggestions_too() -> None:
    response = accessibility._handle_request(
        {"op": "wait", "locator": {"role": "button"}, "timeout_ms": 0}
    )
    assert response["result"]["walk"]["role_suggestions"] == ["push button", "toggle button"]


def test_render_names_the_close_roles() -> None:
    found = ElementList(
        locator=Locator(role="button"), elements=[], role_suggestions=["push button"]
    )
    assert found.render() == (
        "No elements found matching role='button'\n"
        "No element in scope has role 'button'; roles seen that are close: 'push button'."
    )
    nothing = ElementList(locator=Locator(role="spreadsheet"), elements=[], role_suggestions=[])
    assert nothing.render().splitlines()[1] == (
        "No element in scope has role 'spreadsheet'; list the roles with an element_query "
        "without role."
    )
