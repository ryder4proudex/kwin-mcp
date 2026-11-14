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


def test_reachable_bus_keeps_the_advertised_address(monkeypatch: pytest.MonkeyPatch) -> None:
    connection = Mock()
    connection.get_object.return_value.GetId.return_value = "abc123"
    monkeypatch.setattr(dbus.bus, "BusConnection", Mock(return_value=connection))
    assert _probe({"AT_SPI_BUS_ADDRESS": "unix:path=/bus"}) == ("unix:path=/bus", "abc123")
    connection.close.assert_called_once()


def test_worker_receives_the_address_and_follows_the_bus_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = core.AutomationEngine()
    monkeypatch.setattr(engine, "_session_env", lambda: {"DBUS_SESSION_BUS_ADDRESS": "session"})
    identity = ["abc"]
    monkeypatch.setattr(engine, "_a11y_bus_address", lambda env: ("unix:path=/v", identity[0]))
    monkeypatch.setattr(core.process_registry, "poll", lambda proc: None)
    teardown = Mock(side_effect=lambda: setattr(engine, "_atspi_proc", None))
    monkeypatch.setattr(engine, "_teardown_atspi_worker", teardown)
    spawn = Mock(side_effect=lambda *args, **kwargs: SimpleNamespace())
    monkeypatch.setattr(core.process_registry, "spawn", spawn)

    first = engine._ensure_atspi_worker()
    assert engine._ensure_atspi_worker() is first
    identity[0] = "restarted"
    assert engine._ensure_atspi_worker() is not first

    assert [call.kwargs["env"]["AT_SPI_BUS_ADDRESS"] for call in spawn.call_args_list] == [
        "unix:path=/v",
        "unix:path=/v",
    ]
    teardown.assert_called_once()
    engine._atspi_proc = None


def test_address_without_guid_stays_connectable() -> None:
    """Regression: a GetId value written as ``guid=`` made libdbus reject the bus.

    The live desktop's dbus-broker advertises its address without a guid; the
    worker then aborted in libatspi on every call although the bus was healthy.
    """
    if not shutil.which("dbus-run-session") or not shutil.which("dbus-send"):
        pytest.skip("dbus-run-session or dbus-send is unavailable")
    script = (
        "import os, subprocess; from kwin_mcp.atspi_bus import checked_address; "
        "plain = ','.join(p for p in os.environ['DBUS_SESSION_BUS_ADDRESS'].split(',') "
        "if not p.startswith('guid=')); "
        "address, bus_id = checked_address({'AT_SPI_BUS_ADDRESS': plain}); "
        "assert address == plain, address; "
        "out = subprocess.run(['dbus-send', '--bus=' + address, '--print-reply=literal', "
        "'--dest=org.freedesktop.DBus', '/org/freedesktop/DBus', "
        "'org.freedesktop.DBus.GetId'], capture_output=True, text=True, timeout=10, "
        "check=True).stdout; "
        "assert out.split() == [bus_id], (out, bus_id)"
    )
    result = subprocess.run(
        ["dbus-run-session", "--", sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=30,
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
