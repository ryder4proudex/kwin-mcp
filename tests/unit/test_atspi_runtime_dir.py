"""Unit tests for the accessibility launcher's private runtime dir (issue #6).

at-spi-bus-launcher binds ``$XDG_RUNTIME_DIR/at-spi/bus$DISPLAY`` after unlinking
whatever is there. A virtual session must never run it in the shared runtime
dir, where it would replace another session's bus socket. The real launcher is
exercised by ``tests/e2e/test_atspi_runtime_isolation.py``.
"""

from __future__ import annotations

import contextlib
import os
import shlex
import signal
import stat
import subprocess
from typing import TYPE_CHECKING

import pytest

from kwin_mcp import session as session_module
from kwin_mcp.session import (
    _ATSPI_SOCKET_PATH_LIMIT,
    _ATSPI_SOCKET_SUFFIX,
    Session,
    SessionConfig,
    _make_atspi_runtime_dir,
)

if TYPE_CHECKING:
    from pathlib import Path


def test_dir_is_private_and_in_the_runtime_dir(tmp_path: Path) -> None:
    path = _make_atspi_runtime_dir(str(tmp_path))
    assert path.parent == tmp_path
    assert path.name.startswith("kwin-mcp-a11y-")
    assert stat.S_IMODE(path.stat().st_mode) == 0o700
    assert len(os.fsencode(path)) + len(_ATSPI_SOCKET_SUFFIX) < _ATSPI_SOCKET_PATH_LIMIT
    assert _make_atspi_runtime_dir(str(tmp_path)) != path


def test_long_runtime_dir_falls_back_to_the_temp_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    temp_root = tmp_path / "t"
    temp_root.mkdir()
    long_dir = tmp_path / ("r" * 90)
    long_dir.mkdir()
    monkeypatch.setattr(session_module.tempfile, "tempdir", str(temp_root))

    path = _make_atspi_runtime_dir(str(long_dir))

    assert path.parent == temp_root
    assert list(long_dir.iterdir()) == []


def test_missing_runtime_dir_falls_back_to_the_temp_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(session_module.tempfile, "tempdir", str(tmp_path))
    assert _make_atspi_runtime_dir(str(tmp_path / "missing")).parent == tmp_path


def test_no_short_dir_is_an_actionable_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    long_dir = tmp_path / ("r" * 90)
    long_dir.mkdir()
    monkeypatch.setattr(session_module.tempfile, "tempdir", str(long_dir))
    with pytest.raises(RuntimeError, match="TMPDIR or XDG_RUNTIME_DIR"):
        _make_atspi_runtime_dir(str(long_dir))
    assert list(long_dir.iterdir()) == []


def _script(runtime_dir: Path) -> str:
    session = Session()
    session._socket_name = "wayland-unit"
    session._atspi_runtime_dir = runtime_dir
    return session._build_wrapper_script(SessionConfig())


def test_launcher_is_activated_only_under_the_private_dir(tmp_path: Path) -> None:
    private = tmp_path / "kwin-mcp-a11y-x y"
    script = _script(private)

    assert f"ATSPI_RUNTIME_DIR={shlex.quote(str(private))}\n" in script
    switch = script.index('dbus-update-activation-environment XDG_RUNTIME_DIR="$ATSPI_RUNTIME_DIR"')
    first_activation = script.index("org.a11y.Bus.GetAddress 2>&1")
    restore = script.index('dbus-update-activation-environment XDG_RUNTIME_DIR="$HOST_RUNTIME_DIR"')
    portals = script.index("dbus-update-activation-environment WAYLAND_DISPLAY=")
    kwin = script.index("kwin_wayland --virtual")
    # Activation of the launcher happens between switch and restore, and every
    # other activated service (portals via KWin) after the restore.
    assert switch < first_activation < restore < portals < kwin
    # The direct start path (activation denied) runs the launcher under it too.
    assert 'XDG_RUNTIME_DIR="$ATSPI_RUNTIME_DIR" \\\n        "${ATSPI_HELPER[@]}"' in script


def test_script_requires_the_private_dir() -> None:
    session = Session()
    with pytest.raises(RuntimeError, match="accessibility runtime dir"):
        session._build_wrapper_script(SessionConfig())


_RESTORE = 'if ! dbus-update-activation-environment XDG_RUNTIME_DIR="$HOST_RUNTIME_DIR"'


def _run_switch_and_restore(
    tmp_path: Path, *, host_runtime_dir: str | None, fail_on: str = ""
) -> subprocess.CompletedProcess[str]:
    """Run the wrapper's switch and restore steps with a stub activation updater.

    The accessibility steps between them need a live bus and are covered by the
    E2E test; here only the fail-closed branches around them are exercised.
    """
    private = tmp_path / "kwin-mcp-a11y-unit"
    script = _script(private)
    switch = script[script.index("ATSPI_RUNTIME_DIR=") : script.index("ATSPI_UP=1\n")]
    restore_at = script.index(_RESTORE)
    restore = script[restore_at : script.index("\nfi\n", restore_at) + len("\nfi\n")]
    trap = script[script.index("cleanup() {") : script.index("trap cleanup EXIT TERM INT HUP\n")]
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls.log"
    stub = bin_dir / "dbus-update-activation-environment"
    stub.write_text(
        '#!/bin/sh\necho "$*" >> "$STUB_LOG"\n'
        'case "$*" in *"$STUB_FAIL_ON"*) [ -n "$STUB_FAIL_ON" ] && exit 1 ;; esac\nexit 0\n'
    )
    stub.chmod(0o755)
    env = {"PATH": f"{bin_dir}:{os.environ['PATH']}", "STUB_LOG": str(log)}
    env["STUB_FAIL_ON"] = fail_on
    if host_runtime_dir is not None:
        env["XDG_RUNTIME_DIR"] = host_runtime_dir
    # A long-lived background child stands in for directly started AT-SPI
    # helpers: an early exit must not wait for it.
    helper = tmp_path / "helper.pid"
    body = (
        f"{trap}trap cleanup EXIT\n"
        f"sleep 30 </dev/null >/dev/null 2>&1 &\necho $! > {shlex.quote(str(helper))}\n"
        f"{switch}echo SWITCHED\n{restore}echo RESTORED\n"
    )
    try:
        return subprocess.run(
            ["bash", "-c", body], env=env, capture_output=True, text=True, check=False, timeout=5
        )
    finally:
        with contextlib.suppress(OSError, ValueError):
            os.kill(int(helper.read_text()), signal.SIGKILL)


def test_switch_and_restore_point_activation_back_at_the_host_dir(tmp_path: Path) -> None:
    result = _run_switch_and_restore(tmp_path, host_runtime_dir="/run/host-rt")
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["SWITCHED", "RESTORED"]
    calls = (tmp_path / "calls.log").read_text().splitlines()
    assert calls == [
        f"XDG_RUNTIME_DIR={tmp_path / 'kwin-mcp-a11y-unit'}",
        "XDG_RUNTIME_DIR=/run/host-rt",
    ]


def test_failed_switch_refuses_to_activate_the_launcher(tmp_path: Path) -> None:
    result = _run_switch_and_restore(
        tmp_path, host_runtime_dir="/run/host-rt", fail_on="kwin-mcp-a11y-unit"
    )
    assert result.returncode == 1
    assert result.stdout.split() == ["FAILED"]
    assert "refusing to activate it in the shared one" in result.stderr


def test_failed_restore_stops_the_session(tmp_path: Path) -> None:
    result = _run_switch_and_restore(tmp_path, host_runtime_dir="/run/host-rt", fail_on="host-rt")
    assert result.returncode == 1
    assert result.stdout.split() == ["SWITCHED", "FAILED"]
    assert "could not restore XDG_RUNTIME_DIR" in result.stderr


@pytest.mark.parametrize("host_runtime_dir", [None, ""])
def test_unset_host_runtime_dir_stops_before_the_switch(
    tmp_path: Path, host_runtime_dir: str | None
) -> None:
    result = _run_switch_and_restore(tmp_path, host_runtime_dir=host_runtime_dir)
    assert result.returncode == 1
    assert result.stdout.split() == ["FAILED"]
    assert "XDG_RUNTIME_DIR is not set" in result.stderr
    assert not (tmp_path / "calls.log").exists()
