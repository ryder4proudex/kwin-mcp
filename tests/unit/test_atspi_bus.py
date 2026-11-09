"""Broken a11y endpoints are diagnosed without starting a native worker."""

from __future__ import annotations

import shutil
import socket
import subprocess
import sys
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import Mock

import dbus.bus
import pytest

from kwin_mcp import atspi_bus, core
from kwin_mcp.atspi_bus import _probe, checked_address

if TYPE_CHECKING:
    from pathlib import Path


def test_advertised_dead_socket_refuses_request_without_spawn(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "dead-bus"
    with socket.socket(socket.AF_UNIX) as sock:
        sock.bind(str(path))
    session = Mock()
    session.get_object.return_value.GetAddress.return_value = f"unix:path={path}"
    connect = dbus.bus.BusConnection
    monkeypatch.setattr(
        dbus.bus,
        "BusConnection",
        lambda address: session if address == "session" else connect(address),
    )
    monkeypatch.setattr(atspi_bus, "checked_address", _probe)
    engine = core.AutomationEngine()
    monkeypatch.setattr(engine, "_session_env", lambda: {"DBUS_SESSION_BUS_ADDRESS": "session"})
    spawn = Mock(side_effect=AssertionError("must not spawn on an unreachable bus"))
    monkeypatch.setattr(core.process_registry, "spawn", spawn)
    for _ in range(2):
        with pytest.raises(RuntimeError, match=r"org\.a11y\.Bus\.GetAddress.*NoServer") as error:
            engine._run_atspi("query", max_nodes=10)
        assert "systemctl --user restart at-spi-dbus-bus.service" in str(error.value)
        assert "Retried once" not in str(error.value)
    spawn.assert_not_called()
    assert session.close.call_count == 2


def test_explicit_override_failure_does_not_fall_back(monkeypatch: pytest.MonkeyPatch) -> None:
    connect = Mock(side_effect=ValueError("private-value"))
    monkeypatch.setattr(dbus.bus, "BusConnection", connect)
    with pytest.raises(RuntimeError, match="Remove or correct AT_SPI_BUS_ADDRESS") as error:
        _probe({"AT_SPI_BUS_ADDRESS": "broken", "DBUS_SESSION_BUS_ADDRESS": "session"})
    connect.assert_called_once_with("broken")
    assert "private-value" not in str(error.value)


def test_reachable_bus_is_pinned_to_verified_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    connection = Mock()
    connection.get_object.return_value.GetId.return_value = "abc123"
    monkeypatch.setattr(dbus.bus, "BusConnection", Mock(return_value=connection))
    assert _probe({"AT_SPI_BUS_ADDRESS": "unix:path=/bus"}) == "unix:path=/bus,guid=abc123"
    connection.close.assert_called_once()


def test_worker_receives_verified_address(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = core.AutomationEngine()
    monkeypatch.setattr(engine, "_session_env", lambda: {"DBUS_SESSION_BUS_ADDRESS": "session"})
    monkeypatch.setattr(engine, "_a11y_bus_address", lambda env: "unix:path=/verified,guid=abc")
    proc = SimpleNamespace()
    spawn = Mock(return_value=proc)
    monkeypatch.setattr(core.process_registry, "spawn", spawn)
    assert engine._ensure_atspi_worker() is proc
    assert spawn.call_args.kwargs["env"]["AT_SPI_BUS_ADDRESS"] == "unix:path=/verified,guid=abc"
    engine._atspi_proc = None


def test_real_dbus_connection_identity() -> None:
    if not shutil.which("dbus-run-session"):
        pytest.skip("dbus-run-session is unavailable")
    result = subprocess.run(
        [
            "dbus-run-session",
            "--",
            sys.executable,
            "-c",
            "import os; from kwin_mcp.atspi_bus import checked_address; "
            "env = {'AT_SPI_BUS_ADDRESS': os.environ['DBUS_SESSION_BUS_ADDRESS']}; "
            "address = checked_address(env); "
            "assert 'guid=' in address",
        ],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr


def test_authentication_stall_is_bounded(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    path = tmp_path / "stalled-bus"
    monkeypatch.setattr(atspi_bus, "_PROBE_TIMEOUT_S", 0.3)
    with socket.socket(socket.AF_UNIX) as listener:
        listener.bind(str(path))
        listener.listen()
        with pytest.raises(RuntimeError, match=r"timed out.*authentication"):
            checked_address({"AT_SPI_BUS_ADDRESS": f"unix:path={path}"})
