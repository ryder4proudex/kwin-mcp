"""Counts in result text agree with their noun: "1 element", "2 elements"."""

from __future__ import annotations

from typing import Any

from kwin_mcp.models import AppLogResult, ElementList, Locator, UIElement, text_echo


def _element(ref: str) -> UIElement:
    worker: dict[str, Any] = {
        "ref": ref,
        "role": "push button",
        "name": "OK",
        "description": "",
        "states": [],
        "depth": 2,
        "app": "app",
        "window_id": "w1",
        "children_count": 0,
        "mapped": True,
        "x": 1,
        "y": 1,
        "width": 10,
        "height": 10,
        "actions": [],
        "text": "",
        "value": None,
        "value_max": None,
    }
    return UIElement.from_worker(worker)


def test_one_element_is_singular() -> None:
    found = ElementList(locator=Locator(query="OK"), elements=[_element("e1.1")])
    assert found.render().startswith("Found 1 element matching query='OK':")
    tree = ElementList(elements=[_element("e1.1")])
    assert tree.render().startswith("# Accessibility Tree (1 element)")
    written = ElementList(elements=[], output_file="/tmp/x.jsonl", output_count=1, output_bytes=9)
    assert written.render() == "Wrote 1 element as JSONL to /tmp/x.jsonl (9 bytes)."


def test_several_elements_stay_plural() -> None:
    found = ElementList(locator=Locator(query="OK"), elements=[_element("e1.1"), _element("e1.2")])
    assert found.render().startswith("Found 2 elements matching")


def test_walk_and_log_counts() -> None:
    walk = ElementList(elements=[_element("e1.1")], visited=1, truncated="time")
    assert "after 1 node;" in walk.render()
    log = AppLogResult(pid=7, log_path="/l", lines=["x"], truncated=True)
    assert log.render().startswith("App log of PID 7: /l (last 1 line)")


def test_long_echo_counts_characters() -> None:
    assert text_echo("x" * 1000) == "<1000 characters>"
