"""Web content in device pixels: Chromium at a fractional output scale.

At scale 1.6, Google Chrome 156 on KWin 6.8.1 reports its browser UI in logical
pixels but a page's "document web" node and everything inside it in device
pixels (observed: a 1353x1223 logical panel holding a 2165x1957 document at
(104, 145) instead of (65, 91)). The worker divides such a subtree by the
output scale before translating it to the screen.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from kwin_mcp import accessibility
from kwin_mcp.accessibility import _device_pixel_scale

from ._atspi_tree import Node, install, kwin_window

if TYPE_CHECKING:
    from collections.abc import Callable

PID = 4242


def _scales(*values: float) -> Callable[[], tuple[float, ...]]:
    return lambda: values


@pytest.mark.parametrize(
    ("rect", "parent", "scales", "expected"),
    [
        pytest.param((104, 145, 2165, 1957), (65, 91, 1353, 1223), (1.6,), 1.6, id="observed"),
        pytest.param((65, 91, 1353, 1223), (65, 91, 1353, 1223), (1.6,), 1.0, id="logical"),
        pytest.param((0, 0, 2000, 1000), (0, 0, 1000, 500), (1.6,), 1.0, id="not-a-scale"),
        pytest.param((0, 0, 2000, 1000), (0, 0, 1000, 500), (1.0, 2.0), 2.0, id="second-output"),
        pytest.param((0, 0, 1600, 900), (0, 0, 1000, 500), (1.6,), 1.0, id="height-differs"),
        pytest.param((0, 0, 1600, 800), None, (1.6,), 1.0, id="no-parent"),
        pytest.param((0, 0, 1600, 800), (0, 0, 0, 0), (1.6,), 1.0, id="empty-parent"),
        pytest.param((0, 0, 1600, 800), (0, 0, 1000, 500), (), 1.0, id="kwin-unavailable"),
    ],
)
def test_device_pixel_scale(
    rect: tuple[int, int, int, int],
    parent: tuple[int, int, int, int] | None,
    scales: tuple[float, ...],
    expected: float,
) -> None:
    assert _device_pixel_scale(rect, parent, _scales(*scales)) == expected


def test_kwin_is_not_asked_when_sizes_agree() -> None:
    def fail() -> tuple[float, ...]:
        raise AssertionError("output scales must not be read")

    assert _device_pixel_scale((0, 0, 500, 400), (0, 0, 501, 400), fail) == 1.0


def _chrome() -> Node:
    """frame > panel > document web (device px) > section > button; plus a toolbar button."""
    button = Node("Send", "push button", (160, 320, 160, 64), actions=["click"])
    nested = Node("ad", "document web", (32, 48, 80, 40))
    document = Node(
        "Page",
        "document web",
        (104, 145, 2165, 1957),
        children=[Node("", "section", (104, 145, 2165, 1957), children=[button, nested])],
    )
    panel = Node("", "panel", (65, 91, 1353, 1223), children=[document])
    toolbar = Node("Reload", "push button", (20, 40, 32, 32), actions=["press"])
    frame = Node(
        "Page - Google Chrome - Work",
        "frame",
        (0, 0, 1428, 1324),
        children=[Node("", "panel", (0, 0, 1428, 1324), children=[toolbar, panel])],
    )
    return Node("Google Chrome", "application", children=[frame], pid=PID)


def _query(locator: dict[str, Any]) -> list[dict[str, Any]]:
    response = accessibility._handle_request({"op": "query", "locator": locator, "max_depth": 64})
    assert response["ok"] is True, response
    return response["result"]


@pytest.fixture
def chrome(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    window = kwin_window(
        PID, "w1", "Page - Google Chrome", [992, 0, 1408, 1304], [982, -10, 1428, 1324]
    )
    return install(monkeypatch, [_chrome()], [window], scales=(1.6,))


def _rect(element: dict[str, Any]) -> tuple[int, int, int, int]:
    assert element["mapped"] is True, element
    return element["x"], element["y"], element["width"], element["height"]


def test_web_subtree_is_divided_by_the_scale_and_translated(chrome: list[Any]) -> None:
    del chrome
    by_name = {e["name"]: e for e in _query({}) if e["name"]}

    # Buffer origin (982, -10) plus the logical window-local rectangle.
    assert _rect(by_name["Reload"]) == (1002, 30, 32, 32)
    assert _rect(by_name["Page"]) == (982 + 65, -10 + 91, 1353, 1223)
    assert _rect(by_name["Send"]) == (982 + 100, -10 + 200, 100, 40)
    # A nested document inherits the scale instead of being scaled twice.
    assert _rect(by_name["ad"]) == (982 + 20, -10 + 30, 50, 25)


def test_refs_and_strict_actions_keep_the_scale(chrome: list[Any]) -> None:
    (send,) = _query({"query": "Send"})
    response = accessibility._handle_request({"op": "resolve", "refs": [send["ref"]]})
    assert _rect(response["result"][0]["element"]) == (1082, 190, 100, 40)

    response = accessibility._handle_request(
        {"op": "action", "address": {"strict": {"query": "Send"}}, "action_name": "click"}
    )
    assert response["ok"] is True, response
    assert _rect(response["result"]["element"]) == (1082, 190, 100, 40)
    assert chrome == [("click", 0)]
