"""Application children that alias a node inside another top-level (issue #10).

Qt lists the offscreen QQuickWindow of a QQuickWidget as a child of its
application, but that window's parent is a node inside the main window, which
also exposes the same QML content (KDE System Settings, Info Center). Walks and
strict searches must see that content once, under the window that shows it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from kwin_mcp import accessibility

from ._atspi_tree import Node, install, kwin_window

if TYPE_CHECKING:
    from kwin_mcp.geometry import KWinWindow

SETTINGS = 30

WINDOWS: list[KWinWindow] = [
    kwin_window(SETTINGS, "w-settings", "Keyboard — System Settings", [100, 50, 930, 650]),
]


def _apps(*, broken_parent: bool = False, unlisted: bool = False) -> list[Node]:
    """systemsettings: frame > layered pane > host > quick root > Keyboard entry.

    The quick root is also listed as a second child of the application, the way
    Qt lists the QQuickWidget's offscreen window; its parent stays ``host``.
    """
    entry = Node("Keyboard", "list item", (10, 200, 250, 30), actions=["Press"])
    quick_root = Node("", "filler", (0, 0, 271, 650), children=[entry])
    host = Node("", "filler", (0, 0, 271, 650), children=[quick_root])
    frame = Node(
        "Keyboard — System Settings",
        "frame",
        (0, 0, 930, 650),
        children=[Node("", "layered pane", (0, 0, 930, 650), children=[host])],
    )
    app = Node("systemsettings", "application", children=[quick_root, frame], pid=SETTINGS)
    # Node() made the application the parent of every listed child; Qt reports
    # the alias's real parent.
    quick_root.parent = None if broken_parent else host
    if unlisted:
        # The chain points through the frame, but no node there enumerates the
        # child: it is reachable only from the application, so it is no alias.
        host.children = []
    return [app]


@pytest.fixture
def desktop(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    return install(monkeypatch, _apps(), WINDOWS)


def test_walk_lists_quick_content_once_and_mapped(desktop: list[Any]) -> None:
    del desktop
    response = accessibility._handle_request(
        {"op": "query", "locator": {"role": "list item", "query": "Keyboard"}}
    )

    assert response["ok"] is True
    found = response["result"]
    assert len(found) == 1
    assert found[0]["window_id"] == "w-settings"
    assert found[0]["mapped"] is True
    assert (found[0]["x"], found[0]["y"]) == (110, 250)


def test_strict_action_is_not_ambiguous(desktop: list[Any]) -> None:
    response = accessibility._handle_request(
        {
            "op": "action",
            "address": {"strict": {"app": "systemsettings", "query": "Keyboard"}},
            "action_name": "Press",
        }
    )

    assert response["ok"] is True, response
    assert desktop == [("Press", 0)]


def test_child_with_unreadable_parent_chain_is_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    # Without proof that the content is reachable elsewhere, the child stays.
    install(monkeypatch, _apps(broken_parent=True), WINDOWS)
    response = accessibility._handle_request(
        {"op": "query", "locator": {"role": "list item", "query": "Keyboard"}}
    )

    assert response["ok"] is True
    assert len(response["result"]) == 2


def test_child_its_parent_does_not_enumerate_is_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    # A parent chain alone proves nothing: every link must list the node below it.
    invoked = install(monkeypatch, _apps(unlisted=True), WINDOWS)
    response = accessibility._handle_request(
        {"op": "query", "locator": {"role": "list item", "query": "Keyboard"}}
    )

    assert response["ok"] is True
    assert len(response["result"]) == 1
    strict = accessibility._handle_request(
        {
            "op": "action",
            "address": {"strict": {"app": "systemsettings", "query": "Keyboard"}},
            "action_name": "Press",
        }
    )
    assert strict["ok"] is True, strict
    assert invoked == [("Press", 0)]
