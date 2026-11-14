"""Pointer and touch targets outside every screen are refused before any input.

libei silently drops absolute motion and touch points outside the EIS device's
regions, so a click aimed off-screen would land wherever the pointer already
was. The engine checks every point a gesture will visit against the device's
regions and refuses the call, sending nothing, when one lies outside them. A
device that reports no regions is not checked.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import pytest

from kwin_mcp.models import ElementPoint, Point, Waypoint

if TYPE_CHECKING:
    from ._fakes import FakeEngine

GENERATION = 3
# Two side-by-side screens: 1920x1080 at the origin and 1280x1024 to its right.
REGIONS = [(0, 0, 1920, 1080), (1920, 0, 1280, 1024)]
SCREENS = "1920x1080+0+0, 1280x1024+1920+0"


def _element(ref: str, *, x: int, y: int) -> dict[str, object]:
    """One element as the AT-SPI worker reports it, 40x20 at (x, y)."""
    return {
        "ref": ref,
        "role": "push button",
        "name": "Far button",
        "description": "",
        "states": ["enabled", "showing", "visible"],
        "depth": 3,
        "app": "chrome",
        "window_id": "{w1}",
        "children_count": 0,
        "mapped": True,
        "x": x,
        "y": y,
        "width": 40,
        "height": 20,
        "actions": ["press"],
        "text": "",
        "value": None,
        "value_max": None,
    }


@pytest.fixture
def engine(fake_engine: FakeEngine) -> FakeEngine:
    fake_engine.engine._atspi_generation = GENERATION
    fake_engine.eis.regions = list(REGIONS)
    return fake_engine


def _point_error(x: int, y: int) -> str:
    return (
        f"Point ({x}, {y}) is outside every screen ({SCREENS}); input there would be "
        "dropped.\nPass a point inside a screen."
    )


@pytest.mark.qa("QA-INP-073")
@pytest.mark.parametrize(
    "point",
    [Point(x=561, y=2949), Point(x=-1, y=10), Point(x=1920, y=1050), Point(x=3200, y=10)],
)
def test_pointer_click_outside_every_screen_is_refused_without_input(
    engine: FakeEngine, point: Point
) -> None:
    with pytest.raises(ValueError) as excinfo:
        engine.engine.pointer_click(target=point)

    assert str(excinfo.value) == _point_error(point.x, point.y)
    assert engine.eis.events == []


@pytest.mark.qa("QA-INP-073")
def test_points_on_any_screen_are_accepted(engine: FakeEngine) -> None:
    for x, y in [(0, 0), (3199, 1023), (1919, 1079)]:
        engine.engine.pointer_move(target=Point(x=x, y=y))

        # The move may nudge by a pixel first; it ends on the target.
        assert engine.eis.of_kind("motion")[-1].args == (x, y)


@pytest.mark.qa("QA-INP-073")
def test_element_scrolled_off_screen_is_refused_naming_the_element(engine: FakeEngine) -> None:
    ref = f"e{GENERATION}.45"
    engine.atspi.respond(
        "resolve",
        {"ok": True, "result": [{"ref": ref, "ok": True, "element": _element(ref, x=541, y=2939)}]},
    )

    with pytest.raises(RuntimeError) as excinfo:
        engine.engine.pointer_click(target=ElementPoint(ref=ref))

    assert str(excinfo.value) == (
        f"Element {ref} (push button 'Far button') is at (561, 2949), outside every screen "
        f"({SCREENS}); input there would be dropped.\nScroll it into view and re-run "
        "element_query, or use element_act(element={ref}, action)."
    )
    assert engine.eis.events == []


@pytest.mark.qa("QA-INP-073")
@pytest.mark.parametrize(
    ("tool", "kwargs", "bad"),
    [
        ("pointer_scroll", {"target": Point(x=10, y=5000), "delta": 1}, (10, 5000)),
        ("pointer_hold", {"phase": "press", "target": Point(x=10, y=5000)}, (10, 5000)),
        ("touch_tap", {"target": Point(x=10, y=5000)}, (10, 5000)),
        (
            "pointer_drag",
            {"from_": Point(x=10, y=10), "to": Point(x=10, y=5000)},
            (10, 5000),
        ),
        (
            "pointer_drag",
            {
                "from_": Point(x=10, y=10),
                "to": Point(x=20, y=20),
                "waypoints": [Waypoint(x=15, y=15), Waypoint(x=15, y=-3)],
            },
            (15, -3),
        ),
        (
            "touch_swipe",
            {"from_": Point(x=10, y=5000), "to": Point(x=10, y=10)},
            (10, 5000),
        ),
        # Three fingers spread 20 px apart vertically around each end point.
        (
            "touch_swipe",
            {"from_": Point(x=100, y=500), "to": Point(x=100, y=1065), "fingers": 3},
            (100, 1085),
        ),
        # The contacts sit start_distance / 2 and end_distance / 2 left and right of the centre.
        (
            "touch_pinch",
            {"center": Point(x=100, y=500), "start_distance": 100, "end_distance": 300},
            (-50, 500),
        ),
    ],
)
def test_every_point_a_gesture_visits_is_checked(
    engine: FakeEngine, tool: str, kwargs: dict[str, object], bad: tuple[int, int]
) -> None:
    with pytest.raises(ValueError, match=re.escape(f"Point ({bad[0]}, {bad[1]}) is outside")):
        getattr(engine.engine, tool)(**kwargs)

    assert engine.eis.events == []


@pytest.mark.qa("QA-INP-073")
def test_device_without_regions_is_not_checked(fake_engine: FakeEngine) -> None:
    fake_engine.eis.regions = None

    fake_engine.engine.pointer_move(target=Point(x=561, y=2949))

    assert fake_engine.eis.of_kind("motion")[-1].args == (561, 2949)
