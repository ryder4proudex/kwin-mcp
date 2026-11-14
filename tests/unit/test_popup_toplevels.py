"""Qt popups that the application leaves out of its children.

Qt's application node omits ``Qt::Popup`` windows (QMenu context menus, combo
box lists) from its children, although each reports the application as its
parent. The worker learns about them from ``object:state-changed:showing``
events and adds the ones still showing to their application's top-levels.
"""

from __future__ import annotations

import types
from typing import TYPE_CHECKING, Any

import pytest

from kwin_mcp import accessibility

from ._atspi_tree import Node, install, kwin_window

if TYPE_CHECKING:
    from kwin_mcp.geometry import KWinWindow

FILES = 40


def _popup_window() -> KWinWindow:
    window = kwin_window(FILES, "w-menu", "", [700, 400, 360, 200])
    window["normal"], window["popup"] = False, True
    return window


WINDOWS: list[KWinWindow] = [
    kwin_window(FILES, "w-main", "Home — Files", [100, 100, 760, 550]),
    _popup_window(),
]


def _desktop(*, popup_states: list[str] | None = None) -> tuple[Node, Node]:
    """An application with one listed frame and an unlisted context menu."""
    frame = Node("Home — Files", "frame", (0, 0, 760, 550), children=[Node("Places", "list")])
    app = Node("files", "application", children=[frame], pid=FILES)
    popup = Node(
        "Files",
        "popup menu",
        (0, 0, 360, 200),
        states=popup_states if popup_states is not None else ["enabled", "showing"],
        children=[Node("Properties", "menu item", (4, 40, 352, 29), actions=["Press"])],
    )
    popup.parent = app
    return app, popup


@pytest.fixture
def tracker(monkeypatch: pytest.MonkeyPatch) -> accessibility._PopupTracker:
    fresh = accessibility._PopupTracker()
    monkeypatch.setattr(accessibility, "_POPUPS", fresh)
    return fresh


def _showing(source: Node, detail1: int = 1) -> Any:
    return types.SimpleNamespace(source=source, detail1=detail1)


def _menu_items() -> list[dict[str, Any]]:
    response = accessibility._handle_request(
        {"op": "query", "locator": {"role": "menu item", "states": ["showing"]}}
    )
    assert response["ok"] is True
    return response["result"]


def test_unlisted_popup_is_invisible_without_its_showing_event(
    monkeypatch: pytest.MonkeyPatch, tracker: accessibility._PopupTracker
) -> None:
    del tracker
    app, _popup = _desktop()
    install(monkeypatch, [app], WINDOWS)

    assert _menu_items() == []


def test_popup_seen_showing_is_walked_and_mapped_to_its_window(
    monkeypatch: pytest.MonkeyPatch, tracker: accessibility._PopupTracker
) -> None:
    del tracker
    app, popup = _desktop()
    install(monkeypatch, [app], WINDOWS)
    accessibility._on_showing(_showing(popup))

    items = _menu_items()

    assert [(i["name"], i["window_id"], i["x"], i["y"]) for i in items] == [
        ("Properties", "w-menu", 704, 440)
    ]


def test_strict_action_reaches_a_popup_item(
    monkeypatch: pytest.MonkeyPatch, tracker: accessibility._PopupTracker
) -> None:
    del tracker
    app, popup = _desktop()
    invoked = install(monkeypatch, [app], WINDOWS)
    accessibility._on_showing(_showing(popup))

    response = accessibility._handle_request(
        {
            "op": "action",
            "address": {"strict": {"app": "files", "query": "Properties"}},
            "action_name": "Press",
        }
    )

    assert response["ok"] is True, response
    assert invoked == [("Press", 0)]


def test_hidden_popup_is_left_out_but_remembered(
    monkeypatch: pytest.MonkeyPatch, tracker: accessibility._PopupTracker
) -> None:
    app, popup = _desktop(popup_states=["enabled"])
    install(monkeypatch, [app], WINDOWS)
    accessibility._on_showing(_showing(popup))

    assert _menu_items() == []
    assert tracker._seen == [popup]


def test_defunct_popup_is_forgotten(
    monkeypatch: pytest.MonkeyPatch, tracker: accessibility._PopupTracker
) -> None:
    app, popup = _desktop(popup_states=["defunct"])
    install(monkeypatch, [app], WINDOWS)
    accessibility._on_showing(_showing(popup))

    assert _menu_items() == []
    assert tracker._seen == []


def test_hide_events_and_nested_nodes_are_ignored(tracker: accessibility._PopupTracker) -> None:
    app, popup = _desktop()
    del app
    accessibility._on_showing(_showing(popup, detail1=0))
    accessibility._on_showing(_showing(popup.children[0]))

    assert tracker._seen == []


def test_listed_popup_is_not_added_twice(
    monkeypatch: pytest.MonkeyPatch, tracker: accessibility._PopupTracker
) -> None:
    # GTK 3 lists its menus as children; the tracker must not repeat them.
    del tracker
    app, popup = _desktop()
    app.children.append(popup)
    install(monkeypatch, [app], WINDOWS)
    accessibility._on_showing(_showing(popup))

    assert len(_menu_items()) == 1


# PyGObject's GLib.idle_add override imports its asyncio glue, which warns on 3.14.
@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_popup_opened_during_a_wait_is_found(
    monkeypatch: pytest.MonkeyPatch, tracker: accessibility._PopupTracker
) -> None:
    # The showing event arrives on the main context while the worker is busy
    # answering the wait request; the wait must dispatch it between polls.
    del tracker
    app, popup = _desktop()
    install(monkeypatch, [app], WINDOWS)
    accessibility.GLib.idle_add(lambda: accessibility._on_showing(_showing(popup)) and False)

    response = accessibility._handle_request(
        {
            "op": "wait",
            "locator": {"role": "menu item", "query": "Properties"},
            "until": "present",
            "timeout_ms": 2000,
            "poll_interval_ms": 10,
        }
    )

    assert response["ok"] is True
    assert response["result"]["satisfied"] is True, response
    assert response["result"]["elapsed_ms"] < 1000
