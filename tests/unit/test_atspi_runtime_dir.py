"""Unit tests for the accessibility launcher's private runtime dir (issue #6).

at-spi-bus-launcher binds ``$XDG_RUNTIME_DIR/at-spi/bus$DISPLAY`` after unlinking
whatever is there. A virtual session must never run it in the shared runtime
dir, where it would replace another session's bus socket. The real launcher is
exercised by ``tests/e2e/test_atspi_runtime_isolation.py``.
"""

from __future__ import annotations

import os
import shlex
import stat
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
