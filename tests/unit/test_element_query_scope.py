"""element_query scoped to one window, with a text cap and a JSONL output file.

A browser with dozens of windows produced a multi-megabyte tree; agents need
the tree of one window, ideally in a file they can search with grep or jq.
"""

from __future__ import annotations

import json
import os
import stat
from typing import TYPE_CHECKING, Any

import pytest

from kwin_mcp import accessibility
from kwin_mcp.models import Locator, Wait, WindowSelector

from ._atspi_tree import Node, install, kwin_window

if TYPE_CHECKING:
    from pathlib import Path

    from kwin_mcp.geometry import KWinWindow

    from ._fakes import FakeEngine

BROWSER, EDITOR = 10, 20

WINDOWS: list[KWinWindow] = [
    kwin_window(BROWSER, "w-mail", "Mail - Browser", [0, 0, 800, 600], app="browser"),
    kwin_window(BROWSER, "w-docs", "Docs - Browser", [800, 0, 640, 480], app="browser"),
    kwin_window(EDITOR, "w-edit", "notes.txt - Editor", [0, 600, 400, 300], app="editor"),
]


def _apps() -> list[Node]:
    mail = Node(
        "Mail - Browser",
        "frame",
        (0, 0, 800, 600),
        children=[Node("Inbox", "push button", (10, 10, 50, 20), actions=["press"])],
    )
    docs = Node(
        "Docs - Browser",
        "frame",
        (0, 0, 640, 480),
        children=[Node("Share", "push button", (20, 30, 60, 20), actions=["press"])],
    )
    editor = Node(
        "notes.txt - Editor",
        "frame",
        (0, 0, 400, 300),
        children=[Node("", "text", (0, 0, 400, 280), text="x" * 500)],
    )
    return [
        Node("browser", "application", children=[mail, docs], pid=BROWSER),
        Node("editor", "application", children=[editor], pid=EDITOR),
    ]


@pytest.fixture
def desktop(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, _apps(), WINDOWS)


def _query(**fields: Any) -> dict[str, Any]:
    return accessibility._handle_request({"op": "query", "locator": {}, **fields})


def test_window_scope_keeps_one_top_level_and_its_application(desktop: None) -> None:
    del desktop
    response = _query(window_ids=["w-docs"])

    assert response["ok"] is True
    names = [(e["role"], e["name"], e["window_id"]) for e in response["result"]]
    assert names == [
        ("application", "browser", None),
        ("frame", "Docs - Browser", "w-docs"),
        ("push button", "Share", "w-docs"),
    ]
    share = response["result"][-1]
    assert (share["x"], share["y"]) == (820, 30)
    # The editor application owns none of the windows and is not read at all.
    assert response["walk"] == {"visited": 3, "truncated": ""}


def test_scope_of_several_windows_and_of_none(desktop: None) -> None:
    del desktop
    both = _query(window_ids=["w-mail", "w-edit"], locator={"role": "push button"})
    assert [e["name"] for e in both["result"]] == ["Inbox"]

    gone = _query(window_ids=["w-closed"])
    assert gone == {"ok": True, "result": [], "walk": {"visited": 0, "truncated": ""}}


def test_a_window_whose_top_level_does_not_map_yields_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Two same-size, same-caption windows: the top-level cannot be told apart.
    twins = [
        kwin_window(BROWSER, "w-a", "Twin - Browser", [0, 0, 800, 600]),
        kwin_window(BROWSER, "w-b", "Twin - Browser", [900, 0, 800, 600]),
    ]
    frame = Node("Twin - Browser", "frame", (0, 0, 800, 600))
    install(monkeypatch, [Node("browser", "application", children=[frame], pid=BROWSER)], twins)

    assert _query(window_ids=["w-a"])["result"] == []
    unscoped = _query()["result"]
    assert unscoped[1]["unavailable"] == "ambiguous"


def test_scope_needs_the_kwin_window_list(desktop: None, monkeypatch: pytest.MonkeyPatch) -> None:
    del desktop
    monkeypatch.setattr(accessibility, "_kwin_windows", lambda: ([], "DBusException: no KWin"))

    response = _query(window_ids=["w-docs"])
    assert response["ok"] is False
    assert "KWin window list unavailable" in response["error"]
    wait = accessibility._handle_request(
        {"op": "wait", "locator": {}, "window_ids": ["w-docs"], "timeout_ms": 0}
    )
    assert wait["ok"] is False
    # Unscoped walks still work and report the mapping failure per element.
    assert _query()["ok"] is True


def test_max_depth_zero_with_scope_lists_only_the_owning_application(desktop: None) -> None:
    del desktop
    response = _query(window_ids=["w-edit"], max_depth=0)
    assert [e["name"] for e in response["result"]] == ["editor"]


@pytest.mark.parametrize(
    ("cap", "expected"),
    [(None, "x" * 200 + "…"), (10, "x" * 10 + "…"), (0, "x" * 500), (-1, "x" * 500)],
)
def test_text_cap(desktop: None, cap: int | None, expected: str) -> None:
    del desktop
    fields = {} if cap is None else {"text_max_chars": cap}
    response = _query(locator={"role": "text"}, **fields)
    (editor,) = response["result"]
    assert editor["text"] == expected


def _serve(fake_engine: FakeEngine) -> None:
    fake_engine.kwin.respond("windows", {"ok": True, "result": list(WINDOWS)})
    for op in ("query", "wait"):
        fake_engine.atspi.respond(
            op, lambda arguments, op=op: accessibility._handle_request({"op": op, **arguments})
        )


def test_engine_resolves_the_selector_to_window_ids(desktop: None, fake_engine: FakeEngine) -> None:
    del desktop
    _serve(fake_engine)

    result = fake_engine.engine.element_query(window=WindowSelector(caption="docs"))
    assert fake_engine.atspi.calls[-1].kwargs["window_ids"] == ["w-docs"]
    assert result.window_ids == ["w-docs"]
    assert [e.name for e in result.elements] == ["browser", "Docs - Browser", "Share"]
    assert result.render().startswith("# Accessibility Tree in window w-docs (3 elements)")

    browser = fake_engine.engine.element_query(
        locator=Locator(role="push button"), window=WindowSelector(app_id="browser")
    )
    assert browser.window_ids == ["w-mail", "w-docs"]
    assert browser.render().startswith("Found 2 elements matching")
    assert "in window w-mail, w-docs:" in browser.render()

    unscoped = fake_engine.engine.element_query()
    assert fake_engine.atspi.calls[-1].kwargs["window_ids"] is None
    assert unscoped.window_ids is None


def test_engine_scope_errors_and_empty_result(desktop: None, fake_engine: FakeEngine) -> None:
    del desktop
    _serve(fake_engine)

    with pytest.raises(ValueError, match="No window matches"):
        fake_engine.engine.element_query(window=WindowSelector(caption="nowhere"))
    with pytest.raises(ValueError, match="needs at least one field"):
        fake_engine.engine.element_query(window=WindowSelector())

    empty = fake_engine.engine.element_query(
        locator=Locator(query="Inbox"), window=WindowSelector(id="w-docs")
    )
    assert empty.elements == []
    assert empty.render().startswith("No elements found matching")
    assert "cannot be scoped" in empty.render()

    waited = fake_engine.engine.element_query(
        locator=Locator(query="Share"),
        window=WindowSelector(id="w-docs"),
        wait=Wait(timeout_ms=0),
    )
    assert waited.wait is not None
    assert waited.wait.satisfied is True
    assert waited.window_ids == ["w-docs"]


def test_output_file_receives_jsonl(desktop: None, fake_engine: FakeEngine, tmp_path: Path) -> None:
    del desktop
    _serve(fake_engine)
    target = tmp_path / "docs.jsonl"
    target.write_text("old content that must disappear\n" * 10)
    target.chmod(0o644)

    result = fake_engine.engine.element_query(
        window=WindowSelector(id="w-docs"), output_file=str(target)
    )

    lines = target.read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["name"] for line in lines] == ["browser", "Docs - Browser", "Share"]
    assert json.loads(lines[-1])["ref"].startswith("e1.")
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert result.elements == []
    assert (result.output_file, result.output_count) == (str(target), 3)
    assert result.output_bytes == target.stat().st_size
    assert result.render() == (
        f"Wrote 3 elements in window w-docs as JSONL to {target} ({result.output_bytes} bytes)."
    )


def test_new_output_file_is_private(desktop: None, fake_engine: FakeEngine, tmp_path: Path) -> None:
    del desktop
    _serve(fake_engine)
    target = tmp_path / "all.jsonl"
    previous = os.umask(0)
    try:
        result = fake_engine.engine.element_query(
            locator=Locator(role="push button"), output_file=str(target)
        )
    finally:
        os.umask(previous)
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert result.output_count == 2
    assert "Wrote 2 elements matching" in result.render()


@pytest.mark.parametrize(
    ("make", "message"),
    [
        (lambda d: "relative.jsonl", "absolute path ending in .jsonl"),
        (lambda d: str(d / "tree.json"), "absolute path ending in .jsonl"),
        (lambda d: str(d / "missing" / "tree.jsonl"), "directory does not exist"),
        (lambda d: _symlink(d), "not a regular file"),
        (lambda d: _directory(d), "not a regular file"),
    ],
)
def test_output_file_refusals(
    fake_engine: FakeEngine, tmp_path: Path, make: Any, message: str
) -> None:
    path = make(tmp_path)
    with pytest.raises(ValueError, match=message):
        fake_engine.engine.element_query(output_file=path)
    # Refused before any walk runs.
    assert fake_engine.atspi.calls == []


def _symlink(directory: Path) -> str:
    victim = directory / "victim.txt"
    victim.write_text("keep")
    link = directory / "link.jsonl"
    link.symlink_to(victim)
    return str(link)


def _directory(directory: Path) -> str:
    target = directory / "dir.jsonl"
    target.mkdir()
    return str(target)
