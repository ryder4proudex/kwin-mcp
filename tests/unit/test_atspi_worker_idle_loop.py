"""The AT-SPI worker keeps its GLib main context running while it waits for requests.

libatspi reads the a11y bus only when the default GLib main context is dispatched.
A worker that blocked on stdin between requests left AT-SPI event signals unread
until dbus-broker's per-UID quota ran out and the broker disconnected every client
of that UID. These tests run the real ``--serve`` loop in a subprocess; the
requests used here never touch D-Bus.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
import time
import types
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pytest

# A GLib timeout stands in for bus traffic: it fires only if the worker
# dispatches the default main context while it idles between requests.
_WORKER = textwrap.dedent(
    """
    import sys
    from gi.repository import GLib
    from kwin_mcp import accessibility

    def tick():
        sys.stderr.write("dispatched-while-idle\\n")
        sys.stderr.flush()
        return False

    GLib.timeout_add(50, tick)
    accessibility._serve(1)
    """
)


def _start() -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [sys.executable, "-c", _WORKER],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def test_worker_dispatches_main_context_while_idle() -> None:
    proc = _start()
    assert proc.stdin is not None
    assert proc.stdout is not None
    assert proc.stderr is not None
    time.sleep(0.5)
    proc.stdin.write(b'{"op": "no-such-op"}\n')
    proc.stdin.flush()
    reply = json.loads(proc.stdout.readline())
    proc.stdin.close()
    assert proc.wait(timeout=10) == 0
    assert reply == {"ok": False, "error": "Unknown operation: no-such-op"}
    assert "dispatched-while-idle" in proc.stderr.read().decode()


def test_worker_answers_split_and_batched_lines_then_exits_on_eof() -> None:
    proc = _start()
    stdout, _ = proc.communicate(
        b'{"op": "a"}\n{"op": "b"}\n{"op": "c"}',  # no trailing newline on the last one
        timeout=10,
    )
    assert proc.returncode == 0
    replies = [json.loads(line) for line in stdout.splitlines()]
    assert [r["error"] for r in replies] == [
        "Unknown operation: a",
        "Unknown operation: b",
        "Unknown operation: c",
    ]


def test_worker_reports_a_malformed_request_and_keeps_serving() -> None:
    proc = _start()
    stdout, _ = proc.communicate(b'not json\n{"op": "x"}\n', timeout=10)
    assert proc.returncode == 0
    first, second = (json.loads(line) for line in stdout.splitlines())
    assert first["worker_error"].startswith("JSONDecodeError")
    assert second == {"ok": False, "error": "Unknown operation: x"}


def test_worker_start_prints_no_glib_deprecation_warning() -> None:
    """The stdin watch uses GLibUnix where it exists, not the deprecated GLib name.

    The real worker runs as ``__main__``, where Python shows deprecation warnings
    by default; ``-W default`` shows them for this ``-c`` stand-in as well.
    """
    proc = subprocess.Popen(
        [sys.executable, "-W", "default", "-c", _WORKER],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    _, stderr = proc.communicate(b'{"op": "x"}\n', timeout=10)
    assert proc.returncode == 0
    assert b"unix_fd_add_full" not in stderr, stderr.decode()


def test_stdin_watch_falls_back_to_glib_without_glibunix(monkeypatch: pytest.MonkeyPatch) -> None:
    """GLib before 2.80 has no GLibUnix typelib: the old GLib name is used there."""
    from kwin_mcp import accessibility

    def no_glibunix(namespace: str, version: str) -> None:
        msg = f"Namespace {namespace} not available"
        raise ValueError(msg)

    calls: list[tuple[object, ...]] = []
    fake_glib = types.SimpleNamespace(
        PRIORITY_DEFAULT=accessibility.GLib.PRIORITY_DEFAULT,
        unix_fd_add_full=lambda *args: calls.append(args),
    )
    monkeypatch.setattr(accessibility.gi, "require_version", no_glibunix)
    monkeypatch.setattr(accessibility, "GLib", fake_glib)
    callback = lambda _fd, _condition: True  # noqa: E731
    condition = object()  # passed through unchanged
    accessibility._unix_fd_add(0, condition, callback)
    assert calls == [(fake_glib.PRIORITY_DEFAULT, 0, condition, callback)]
