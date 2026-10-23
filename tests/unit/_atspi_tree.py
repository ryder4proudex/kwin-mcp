"""An in-process AT-SPI tree with extents, process ids and actions, for worker tests.

``install()`` points the worker module at a fake desktop of ``Node`` objects and
a fixed KWin window list, so ``accessibility._handle_request`` can run whole
walks, ref resolutions and strict searches without a session.
"""

from __future__ import annotations

import types
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from kwin_mcp import accessibility
from kwin_mcp.accessibility import _RefTable

if TYPE_CHECKING:
    import pytest

    from kwin_mcp.geometry import KWinWindow


@dataclass(frozen=True)
class _State:
    value_nick: str


class _StateSet:
    def __init__(self, nicks: list[str]) -> None:
        self._nicks = nicks

    def get_states(self) -> list[_State]:
        return [_State(nick) for nick in self._nicks]


@dataclass
class _Extents:
    x: int
    y: int
    width: int
    height: int


@dataclass
class _Component:
    rect: tuple[int, int, int, int]

    def get_extents(self, _coord_type: object) -> _Extents:
        return _Extents(*self.rect)


class _Actions:
    def __init__(self, names: list[str]) -> None:
        self.names = names
        self.invoked: list[int] = []

    def get_n_actions(self) -> int:
        return len(self.names)

    def get_action_name(self, index: int) -> str:
        return self.names[index]


@dataclass
class _Text:
    text: str


@dataclass(eq=False)
class Node:
    """Minimal ``Atspi.Accessible`` double with window-local extents."""

    name: str
    role: str = "panel"
    rect: tuple[int, int, int, int] | None = None
    children: list[Node] = field(default_factory=list)
    states: list[str] = field(default_factory=lambda: ["enabled", "showing"])
    actions: list[str] = field(default_factory=list)
    text: str = ""
    pid: int = 0
    parent: Node | None = None

    def __post_init__(self) -> None:
        for child in self.children:
            child.parent = self

    def get_name(self) -> str:
        return self.name

    def get_role_name(self) -> str:
        return self.role

    def get_description(self) -> str:
        return ""

    def get_state_set(self) -> _StateSet:
        return _StateSet(self.states)

    def get_component_iface(self) -> _Component | None:
        return None if self.rect is None else _Component(self.rect)

    def get_action_iface(self) -> _Actions | None:
        return _Actions(self.actions) if self.actions else None

    def get_text_iface(self) -> _Text | None:
        return _Text(self.text) if self.text else None

    def get_value_iface(self) -> None:
        return None

    def get_child_count(self) -> int:
        return len(self.children)

    def get_child_at_index(self, index: int) -> Node:
        return self.children[index]

    def get_parent(self) -> Node | None:
        return self.parent

    def get_process_id(self) -> int:
        return self.pid


def _text_count(text: _Text) -> int:
    return len(text.text)


def _text_slice(text: _Text, start: int, end: int) -> str:
    return text.text[start:end]


def install(
    monkeypatch: pytest.MonkeyPatch,
    apps: list[Node],
    windows: list[KWinWindow],
    scales: tuple[float, ...] = (1.0,),
) -> list[Any]:
    """Serve ``apps`` as the AT-SPI desktop and ``windows`` as KWin's list.

    Returns the list that records every ``Atspi.Action.do_action`` call.
    """
    invoked: list[Any] = []

    def do_action(iface: _Actions, index: int) -> bool:
        invoked.append((iface.names[index], index))
        return True

    monkeypatch.setattr(
        accessibility,
        "Atspi",
        types.SimpleNamespace(
            get_desktop=lambda _index: Node("desktop", "desktop frame", children=apps),
            CoordType=types.SimpleNamespace(WINDOW=1),
            Text=types.SimpleNamespace(get_character_count=_text_count, get_text=_text_slice),
            Action=types.SimpleNamespace(do_action=do_action),
        ),
    )
    monkeypatch.setattr(accessibility, "_kwin_windows", lambda: (list(windows), ""))
    monkeypatch.setattr(accessibility, "_read_output_scales", lambda: scales)
    monkeypatch.setattr(accessibility, "_REFS", _RefTable(generation=1))
    return invoked


def kwin_window(
    pid: int,
    window_id: str,
    caption: str,
    client: list[int],
    buffer: list[int] | None = None,
    *,
    app: str = "app",
    active: bool = False,
) -> KWinWindow:
    """One managed, normal KWin window record as the geometry script reports it."""
    return {
        "pid": pid,
        "id": window_id,
        "app": app,
        "caption": caption,
        "normal": True,
        "dialog": False,
        "utility": False,
        "popup": False,
        "managed": True,
        "deleted": False,
        "desktop": False,
        "dock": False,
        "notification": False,
        "active": active,
        "closeable": True,
        "moveable": True,
        "resizeable": True,
        "desktops": ["d1"],
        "onAllDesktops": False,
        "transient": False,
        "transientFor": None,
        "modal": False,
        "frame": client,
        "client": client,
        "buffer": buffer if buffer is not None else client,
    }
