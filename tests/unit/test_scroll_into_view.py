"""element_act(action="scroll_into_view") through AT-SPI Component.scroll_to (issue #9).

Pointer input cannot reach an element below the visible part of a scrollable
view, and the off-screen refusal says so. ``scroll_into_view`` asks the toolkit
to scroll the element into view, then reports its new position and whether
its centre is on a screen, so the agent can click it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from kwin_mcp import accessibility
from kwin_mcp.models import ElementPoint, Locator, RefAddr, StrictAddr

from ._atspi_tree import Node, install, kwin_window

if TYPE_CHECKING:
    from kwin_mcp.geometry import KWinWindow

    from ._fakes import FakeEngine

PAGE = 60
WINDOWS: list[KWinWindow] = [
    kwin_window(PAGE, "w-page", "Long page - Browser", [0, 0, 1000, 700], app="browser"),
]
REGIONS = [(0, 0, 1920, 1080)]


@pytest.fixture(autouse=True)
def _no_settle_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(accessibility, "_SCROLL_POLL_S", 0.0)


def _page(far: Node | None = None, *extra: Node) -> list[Node]:
    far = far or Node(
        "Far button",
        "push button",
        (20, 2900, 120, 30),
        actions=["press"],
        scrolled_rect=(20, 300, 120, 30),
    )
    frame = Node("Long page - Browser", "frame", (0, 0, 1000, 700), children=[far, *extra])
    return [Node("browser", "application", children=[frame], pid=PAGE)]


def _scroll(address: dict[str, Any]) -> dict[str, Any]:
    return accessibility._handle_request(
        {"op": "action", "address": address, "action_name": "scroll_into_view"}
    )


def _ref_of(name: str) -> str:
    response = accessibility._handle_request({"op": "query", "locator": {"query": name}})
    (element,) = response["result"]
    return element["ref"]


def test_ref_is_scrolled_and_its_new_position_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    invoked = install(monkeypatch, _page(), WINDOWS)
    ref = _ref_of("Far button")

    response = _scroll({"ref": ref})

    assert response["ok"] is True, response
    result = response["result"]
    assert invoked == [("scroll_to", "Far button")]
    assert result["action"] == "scroll_into_view"
    assert result["invoked"] is True
    assert (result["element"]["x"], result["element"]["y"]) == (20, 300)
    assert result["element"]["ref"] == ref


def test_strict_locator_needs_one_element_with_a_position(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A label without a Component interface is no candidate; the button is.
    label = Node("Far button help", "label")
    invoked = install(monkeypatch, _page(None, label), WINDOWS)

    response = _scroll({"strict": {"query": "Far button"}})

    assert response["ok"] is True, response
    assert invoked == [("scroll_to", "Far button")]


def test_strict_locator_refuses_ambiguity_and_no_match(monkeypatch: pytest.MonkeyPatch) -> None:
    twin = Node("Far button", "push button", (20, 3000, 120, 30), scrolled_rect=(0, 0, 1, 1))
    invoked = install(monkeypatch, _page(None, twin), WINDOWS)

    ambiguous = _scroll({"strict": {"query": "Far button"}})
    assert ambiguous["ok"] is False
    assert "2 elements matching" in ambiguous["error"]
    assert ambiguous["error"].endswith("Narrow the locator (role, states, app) or act on one ref.")

    missing = _scroll({"strict": {"query": "Nowhere"}})
    assert missing["ok"] is False
    assert missing["error"] == (
        "No element matching query='Nowhere' has a position to scroll into view.\n"
        "Check the locator with element_query."
    )
    assert invoked == []


@pytest.mark.parametrize(
    ("node", "reason"),
    [
        (
            Node("Far button", "push button", (20, 2900, 120, 30)),
            "Component.scroll_to returned false",
        ),
        (
            Node("Far button", "push button", (20, 2900, 120, 30), scroll_error="No such method"),
            "Component.scroll_to failed: No such method",
        ),
    ],
)
def test_toolkit_without_scroll_to_gets_a_clear_error(
    monkeypatch: pytest.MonkeyPatch, node: Node, reason: str
) -> None:
    install(monkeypatch, _page(node), WINDOWS)
    ref = _ref_of("Far button")

    response = _scroll({"ref": ref})

    assert response["ok"] is False
    assert response["error"] == (
        f'browser could not scroll {ref} [push button] "Far button" into view ({reason}).\n'
        "Scroll its container with pointer_scroll or keyboard_press (e.g. Page_Down), "
        "then re-run element_query."
    )


def test_element_without_component_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, _page(None, Node("Caption", "label")), WINDOWS)
    ref = _ref_of("Caption")

    response = _scroll({"ref": ref})

    assert response["ok"] is False
    assert response["error"] == (
        f'Element {ref} [label] "Caption" has no AT-SPI Component interface, so it has no '
        "position to scroll into view.\n"
        "Scroll its container with pointer_scroll or keyboard_press (e.g. Page_Down), "
        "then re-run element_query."
    )


# ── engine ──────────────────────────────────────────────────────────────


@pytest.fixture
def engine(monkeypatch: pytest.MonkeyPatch, fake_engine: FakeEngine) -> FakeEngine:
    fake_engine.kwin.respond("windows", {"ok": True, "result": list(WINDOWS)})
    for op in ("query", "action", "resolve"):
        fake_engine.atspi.respond(
            op, lambda arguments, op=op: accessibility._handle_request({"op": op, **arguments})
        )
    fake_engine.eis.regions = list(REGIONS)
    fake_engine.engine._atspi_generation = 1
    return fake_engine


def test_engine_reports_the_element_on_screen(
    monkeypatch: pytest.MonkeyPatch, engine: FakeEngine
) -> None:
    install(monkeypatch, _page(), WINDOWS)
    (far,) = engine.engine.element_query(locator=Locator(query="Far button")).elements

    result = engine.engine.element_act(RefAddr(ref=far.ref), "scroll_into_view")

    assert result.action == "scroll_into_view"
    assert result.on_screen is True
    assert result.element is not None
    assert result.element.rect is not None
    assert (result.element.rect.x, result.element.rect.y) == (20, 300)
    assert result.render().splitlines()[0] == (
        'Scrolled [push button] "Far button" in browser into view: its centre (80, 315) '
        "is on screen."
    )
    # The pointer can reach it now.
    engine.engine.pointer_click(target=ElementPoint(ref=far.ref))


def test_engine_says_when_the_element_is_still_off_screen(
    monkeypatch: pytest.MonkeyPatch, engine: FakeEngine
) -> None:
    stuck = Node(
        "Far button", "push button", (20, 2900, 120, 30), scrolled_rect=(20, 2000, 120, 30)
    )
    install(monkeypatch, _page(stuck), WINDOWS)

    result = engine.engine.element_act(
        StrictAddr(strict=Locator(query="Far button")), "scroll_into_view"
    )

    assert result.on_screen is False
    assert result.render().splitlines()[:2] == [
        'Scrolled [push button] "Far button" in browser, but its centre (80, 2015) is still '
        "outside every screen.",
        "Scroll its container with pointer_scroll or keyboard_press, then re-run element_query.",
    ]


@pytest.mark.parametrize("action", ["scroll_into_view|Press", "Press|Scroll_Into_View"])
def test_scroll_into_view_cannot_be_a_fallback(engine: FakeEngine, action: str) -> None:
    with pytest.raises(ValueError, match="scroll_into_view cannot be combined"):
        engine.engine.element_act(RefAddr(ref="e1.1"), action)
    assert engine.atspi.calls == []


def test_off_screen_refusal_points_at_scroll_into_view(
    monkeypatch: pytest.MonkeyPatch, engine: FakeEngine
) -> None:
    install(monkeypatch, _page(), WINDOWS)
    (far,) = engine.engine.element_query(locator=Locator(query="Far button")).elements

    with pytest.raises(RuntimeError) as excinfo:
        engine.engine.pointer_click(target=ElementPoint(ref=far.ref))

    assert str(excinfo.value).splitlines()[1] == (
        'Run element_act(element={ref}, action="scroll_into_view") and retry, '
        "or invoke its action with element_act."
    )
