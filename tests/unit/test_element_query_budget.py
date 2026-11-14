"""element_query caps the elements it returns inline (issue #8).

A broad query on a large web page returned megabytes inline. The inline reply
keeps the first matching elements whose JSON fits a byte budget and says how
many it left out; ``output_file`` still receives every element.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from kwin_mcp import accessibility
from kwin_mcp.models import DEFAULT_ELEMENT_QUERY_MAX_BYTES, Locator, Wait
from kwin_mcp.server import ServerFlags, _parse_argv, build_server

from ._atspi_tree import Node, install, kwin_window

if TYPE_CHECKING:
    from pathlib import Path

    from kwin_mcp.geometry import KWinWindow
    from kwin_mcp.models import UIElement

    from ._fakes import FakeEngine

PAGE = 50
LINKS = 120

WINDOWS: list[KWinWindow] = [
    kwin_window(PAGE, "w-page", "Long page - Browser", [0, 0, 1000, 700], app="browser"),
]


def _apps() -> list[Node]:
    links = [
        Node(f"Link number {i} with a fairly long accessible name", "link", (10, 20 * i, 300, 18))
        for i in range(LINKS)
    ]
    frame = Node("Long page - Browser", "frame", (0, 0, 1000, 700), children=links)
    return [Node("browser", "application", children=[frame], pid=PAGE)]


@pytest.fixture
def page(monkeypatch: pytest.MonkeyPatch, fake_engine: FakeEngine) -> FakeEngine:
    install(monkeypatch, _apps(), WINDOWS)
    fake_engine.kwin.respond("windows", {"ok": True, "result": list(WINDOWS)})
    for op in ("query", "wait"):
        fake_engine.atspi.respond(
            op, lambda arguments, op=op: accessibility._handle_request({"op": op, **arguments})
        )
    return fake_engine


def _size(elements: list[UIElement]) -> int:
    return sum(len(e.model_dump_json().encode()) for e in elements)


def test_default_budget_is_64_kib() -> None:
    assert DEFAULT_ELEMENT_QUERY_MAX_BYTES == 65536
    assert ServerFlags().element_query_max_bytes == 65536


def test_inline_reply_keeps_the_first_elements_that_fit(page: FakeEngine) -> None:
    page.engine._element_query_max_bytes = 4000

    result = page.engine.element_query(locator=Locator(role="link"))

    kept = len(result.elements)
    assert 0 < kept < LINKS
    assert result.omitted == LINKS - kept
    assert result.inline_max_bytes == 4000
    assert _size(result.elements) <= 4000
    assert result.elements[0].name.startswith("Link number 0 ")
    text = result.render()
    assert text.startswith(f"Found {kept} elements matching")
    assert (
        f"OUTPUT CAPPED: returned the first {kept} of {LINKS} matching elements "
        "(4000-byte inline budget)."
    ) in text
    assert "output_file" in text.splitlines()[-1]


def test_output_file_is_not_capped(page: FakeEngine, tmp_path: Path) -> None:
    page.engine._element_query_max_bytes = 4000
    target = tmp_path / "links.jsonl"

    result = page.engine.element_query(locator=Locator(role="link"), output_file=str(target))

    assert result.output_count == LINKS
    assert result.omitted == 0
    assert len(target.read_text(encoding="utf-8").splitlines()) == LINKS
    assert "CAPPED" not in result.render()


def test_reply_within_budget_and_zero_budget_are_whole(page: FakeEngine) -> None:
    small = page.engine.element_query(locator=Locator(query="Link number 7 "))
    assert len(small.elements) == 1
    assert small.omitted == 0
    assert "CAPPED" not in small.render()

    page.engine._element_query_max_bytes = 0
    whole = page.engine.element_query(locator=Locator(role="link"))
    assert len(whole.elements) == LINKS
    assert whole.omitted == 0


def test_element_larger_than_the_budget_is_left_out(page: FakeEngine) -> None:
    page.engine._element_query_max_bytes = 50

    result = page.engine.element_query(locator=Locator(role="link"))

    assert result.elements == []
    assert result.omitted == LINKS
    assert "returned the first 0 of 120 matching elements" in result.render()


def test_wait_result_is_capped_too(page: FakeEngine) -> None:
    page.engine._element_query_max_bytes = 4000

    result = page.engine.element_query(locator=Locator(role="link"), wait=Wait(timeout_ms=0))

    assert result.wait is not None
    assert result.wait.satisfied is True
    assert 0 < len(result.elements) < LINKS
    assert result.omitted == LINKS - len(result.elements)


def test_structured_output_carries_the_cap(page: FakeEngine) -> None:
    page.engine._element_query_max_bytes = 4000
    dumped = json.loads(page.engine.element_query(locator=Locator(role="link")).model_dump_json())
    assert dumped["omitted"] > 0
    assert dumped["inline_max_bytes"] == 4000


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["--element-query-max-bytes", "262144"], 262144),
        (["--element-query-max-bytes=0"], 0),
    ],
)
def test_flag_sets_the_budget(argv: list[str], expected: int) -> None:
    assert _parse_argv(argv).element_query_max_bytes == expected
    assert build_server(argv).engine._element_query_max_bytes == expected


@pytest.mark.parametrize(
    "argv",
    [
        ["--element-query-max-bytes"],
        ["--element-query-max-bytes", "-1"],
        ["--element-query-max-bytes", "64k"],
        ["--element-query-max-bytes=1", "--element-query-max-bytes=2"],
    ],
)
def test_flag_usage_errors(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        _parse_argv(argv)
    assert exc_info.value.code == 2
