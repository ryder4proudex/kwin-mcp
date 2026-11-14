"""Virtual sessions never touch another session's accessibility bus socket (issue #6).

at-spi-bus-launcher binds ``$XDG_RUNTIME_DIR/at-spi/bus$DISPLAY`` after unlinking
whatever is there. When virtual sessions shared the host runtime dir, a second
session replaced the first one's socket (and the host desktop's ``bus_0`` under
``DISPLAY=:0``); clients of the first then reached the wrong bus and, once the
second exited, a dead path on which libatspi aborts the process.

The test points ``XDG_RUNTIME_DIR`` at its own directory, so even a regression
cannot reach the host's sockets. Oracles: a foreign listening socket at the
shared path (its inode and liveness), ``org.a11y.Bus.GetAddress`` on each
session bus, and ``org.freedesktop.DBus.GetId`` through each advertised address
(whose ``guid`` libdbus verifies against the server it reaches).
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from kwin_mcp.session import Session, SessionConfig

if TYPE_CHECKING:
    from collections.abc import Iterator

CALL_TIMEOUT_S = 15


def _dbus_send(*args: str, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(
        ["dbus-send", "--print-reply=literal", "--reply-timeout=10000", *args],
        capture_output=True,
        text=True,
        timeout=CALL_TIMEOUT_S,
        env=env,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return " ".join(result.stdout.split())


def _a11y_address(dbus_address: str) -> str:
    env = {**os.environ, "DBUS_SESSION_BUS_ADDRESS": dbus_address}
    return _dbus_send(
        "--session", "--dest=org.a11y.Bus", "/org/a11y/bus", "org.a11y.Bus.GetAddress", env=env
    )


def _bus_id(address: str) -> str:
    return _dbus_send(
        f"--bus={address}",
        "--dest=org.freedesktop.DBus",
        "/org/freedesktop/DBus",
        "org.freedesktop.DBus.GetId",
    )


def _socket_path(address: str) -> Path:
    entries = dict(part.split("=", 1) for part in address.split(","))
    return Path(entries["unix:path"])


def _accepts(path: Path) -> bool:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        try:
            client.connect(str(path))
        except OSError:
            return False
    return True


@pytest.fixture
def runtime_dir(monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    # Short, so the launcher's private dirs land under it, not in the temp dir.
    path = Path(tempfile.mkdtemp(prefix="kwin-mcp-rt-"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(path))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def test_sessions_keep_their_own_accessibility_bus_sockets(runtime_dir: Path) -> None:
    # Another session's bus without DISPLAY, at the path a shared launcher binds.
    shared = runtime_dir / "at-spi" / "bus"
    shared.parent.mkdir(mode=0o700)
    foreign = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    foreign.bind(str(shared))
    foreign.listen()
    foreign_inode = shared.stat().st_ino
    first, second = Session(), Session()
    try:
        first_info = first.start(SessionConfig(socket_name="wayland-a11y-iso-1"))
        second_info = second.start(SessionConfig(socket_name="wayland-a11y-iso-2"))
        assert not first_info.startup_warnings
        assert not second_info.startup_warnings
        addresses = [
            _a11y_address(first_info.dbus_address),
            _a11y_address(second_info.dbus_address),
        ]
        paths = [_socket_path(address) for address in addresses]

        assert paths[0] != paths[1]
        for path in paths:
            assert path.parent.name == "at-spi"
            assert path.parent.parent.name.startswith("kwin-mcp-a11y-")
            assert path.parent.parent.parent == runtime_dir
        # Each advertised address (its guid checked by libdbus) reaches a distinct bus.
        bus_ids = [_bus_id(address) for address in addresses]
        assert bus_ids[0] != bus_ids[1]
        # The foreign socket was neither replaced nor disturbed.
        assert shared.stat().st_ino == foreign_inode
        assert _accepts(shared)

        second.stop()
        assert not paths[1].parent.parent.exists()
        # The first session's bus survives the second session's exit.
        assert _bus_id(addresses[0]) == bus_ids[0]
    finally:
        second.stop()
        first.stop()
        foreign.close()
    assert not paths[0].parent.parent.exists()
    assert list(runtime_dir.glob("kwin-mcp-a11y-*")) == []
    assert [p.name for p in shared.parent.iterdir()] == ["bus"]
