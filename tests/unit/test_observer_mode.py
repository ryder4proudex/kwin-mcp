"""session_connect(observe=True): a read-only observer of a session another process owns."""

from __future__ import annotations

import asyncio
import socket
import sys
import types
from typing import TYPE_CHECKING, cast

import pytest
from mcp.server.mcpserver.exceptions import ToolError

import kwin_mcp.core as core_module
from kwin_mcp import server as server_module
from kwin_mcp.core import OBSERVER_TEXT, AutomationEngine
from kwin_mcp.models import WindowSelector
from kwin_mcp.server import build_server

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from mcp.server.mcpserver import Context

    from tests.unit.conftest import FakeEngine


@pytest.fixture
def observer(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[AutomationEngine]:
    """An engine attached with observe=True to a fake KWin (fake bus, real socket file)."""
    fake_dbus = types.ModuleType("dbus")
    setattr(fake_dbus, "DBusException", RuntimeError)  # noqa: B010
    fake_bus = types.ModuleType("dbus.bus")

    class _Bus:
        def __init__(self, address: str) -> None:
            self.address = address

        def get_object(self, name: str, path: str) -> object:
            return object()

    setattr(fake_bus, "BusConnection", _Bus)  # noqa: B010
    setattr(fake_dbus, "bus", fake_bus)  # noqa: B010
    monkeypatch.setitem(sys.modules, "dbus", fake_dbus)
    monkeypatch.setitem(sys.modules, "dbus.bus", fake_bus)

    def _no_input(*_args: object, **_kwargs: object) -> None:
        msg = "an observer must never open an input backend"
        raise AssertionError(msg)

    monkeypatch.setattr(core_module, "InputBackend", _no_input)
    monkeypatch.setattr(core_module, "accessibility_enabled", lambda _addr: True)
    sock_path = tmp_path / "wayland-owner"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(sock_path))
    listener.listen(1)
    engine = AutomationEngine()
    monkeypatch.setattr(
        engine,
        "_run_kwin_query",
        lambda _req: {"ok": True, "result": {"virtual": [0, 0, 1600, 900]}},
    )
    try:
        engine.session_connect(
            dbus_address="unix:path=/fake", wayland_display=str(sock_path), observe=True
        )
        yield engine
    finally:
        engine._session = None
        listener.close()


def test_observer_has_no_input_or_clipboard(observer: AutomationEngine) -> None:
    info = observer._session_info_model()
    assert info.observer is True
    assert info.input_backend is None
    assert info.clipboard is False
    assert "Observer: read-only" in info.render()


@pytest.mark.parametrize(
    ("method", "args"),
    [
        ("app_launch", {"command": "true"}),
        ("element_act", {"element": {"ref": "e1.1"}, "action": "Press"}),
        ("window_focus", {"window": WindowSelector(active=True)}),
        ("window_place", {"window": WindowSelector(active=True), "desktop": "1"}),
        ("window_close", {"window": WindowSelector(active=True)}),
        ("clipboard_read", {}),
        ("clipboard_write", {"text": "x"}),
        (
            "dbus_call",
            {
                "service": "org.kde.KWin",
                "path": "/KWin",
                "interface": "org.kde.KWin",
                "method": "x",
            },
        ),
        ("keyboard_press", {"keys": "Return"}),
        ("keyboard_type", {"text": "x"}),
        ("pointer_click", {"target": {"x": 1, "y": 1}}),
    ],
)
def test_observer_refuses_every_control_tool(
    observer: AutomationEngine, method: str, args: dict[str, object]
) -> None:
    fn = getattr(observer, method)
    with pytest.raises((RuntimeError, ValueError, TypeError)) as exc:
        fn(**args)
    assert isinstance(exc.value, RuntimeError), exc.value
    assert str(exc.value) == OBSERVER_TEXT


def test_observer_cannot_switch_accessibility() -> None:
    engine = AutomationEngine()
    with pytest.raises(ValueError, match="observe=true"):
        engine.session_connect(
            dbus_address="unix:path=/x",
            wayland_display="w",
            enable_accessibility=True,
            observe=True,
        )
    assert engine._session is None


def test_owner_session_unaffected_by_flag(fake_engine: FakeEngine) -> None:
    """A normal (owning) connection keeps input: the observer gate is opt-in."""
    assert fake_engine.engine._observe is False
    fake_engine.engine._require_control()
    fake_engine.engine._require_input()


def test_server_gates_refuse_observer(observer: AutomationEngine) -> None:
    server = build_server(engine=observer)
    ctx = cast("Context", types.SimpleNamespace(mcp_server=server))
    for gate in (
        server_module.require_control,
        server_module.require_input,
        server_module.require_clipboard,
    ):
        with pytest.raises(ToolError, match="Observer session"):
            asyncio.run(gate(ctx))
    asyncio.run(server_module.require_session(ctx))
