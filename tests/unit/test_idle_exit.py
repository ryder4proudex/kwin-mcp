"""``--idle-exit``: the request clock, the watcher's sleep, and a real server exiting idle."""

from __future__ import annotations

import asyncio
import contextlib
import subprocess
import sys
import time
from typing import TYPE_CHECKING

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from kwin_mcp import server
from kwin_mcp.server import IdleClock, idle_exit_delay

if TYPE_CHECKING:
    from collections.abc import Iterator


class _FakeClock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def test_idle_for_counts_from_the_last_request() -> None:
    clock = _FakeClock()
    idle = IdleClock(clock)
    clock.now += 5
    assert idle.idle_for() == 5
    idle.touch()
    clock.now += 2
    assert idle.idle_for() == 2


def test_idle_for_is_none_while_a_request_runs() -> None:
    clock = _FakeClock()
    idle = IdleClock(clock)
    with idle.busy():
        clock.now += 50
        assert idle.idle_for() is None
        with idle.busy():
            clock.now += 50
        # The outer request is still in flight.
        assert idle.idle_for() is None
    # The clock restarts when the last request ends, not when it began.
    clock.now += 3
    assert idle.idle_for() == 3


def test_busy_ends_on_error() -> None:
    clock = _FakeClock()
    idle = IdleClock(clock)
    with pytest.raises(RuntimeError), idle.busy():
        raise RuntimeError
    assert idle.idle_for() == 0


def test_delay_waits_out_the_remaining_idle_time() -> None:
    clock = _FakeClock()
    idle = IdleClock(clock)
    clock.now += 10
    assert idle_exit_delay(idle, 30) == 20
    clock.now += 25
    assert idle_exit_delay(idle, 30) == 0


def test_delay_polls_while_busy_and_never_exits() -> None:
    clock = _FakeClock()
    idle = IdleClock(clock)
    with idle.busy():
        clock.now += 1000
        assert idle_exit_delay(idle, 30) == server.IDLE_BUSY_POLL_SECONDS
        assert idle_exit_delay(idle, 0.5) == 0.5


def _spawn(*argv: str) -> subprocess.Popen[bytes]:
    # stdin stays open: the client keeps the pipe but never sends a request, as a
    # keep-alive pool does with a connection nobody reuses.
    return subprocess.Popen(
        [sys.executable, "-m", "kwin_mcp", *argv],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def test_server_exits_when_idle_with_stdin_open() -> None:
    proc = _spawn("--default-live-session", "--idle-exit", "1")
    try:
        started = time.monotonic()
        code = proc.wait(timeout=30)
        elapsed = time.monotonic() - started
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    assert proc.stdin is not None
    assert proc.stderr is not None
    stderr = proc.stderr.read().decode()
    proc.stdin.close()
    assert code == 0, stderr
    assert elapsed >= 1
    assert "(--idle-exit); exiting" in stderr
    assert "exit cleanup:" in stderr


def test_server_without_idle_exit_keeps_running() -> None:
    proc = _spawn("--default-live-session")
    assert proc.stdin is not None
    try:
        with pytest.raises(subprocess.TimeoutExpired):
            proc.wait(timeout=2)
        # Closing stdin (EOF) still ends it, with status 0.
        proc.stdin.close()
        assert proc.wait(timeout=30) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


class _CountingClock(IdleClock):
    """An IdleClock that records the most requests seen in flight at once."""

    def __init__(self) -> None:
        super().__init__()
        self.entered = 0
        self.touched = 0

    def touch(self) -> None:
        self.touched += 1
        super().touch()

    @contextlib.contextmanager
    def busy(self) -> Iterator[None]:
        self.entered += 1
        with super().busy():
            yield


def test_requests_drive_the_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tool calls (even rejected ones), resource reads and tools/list count as requests."""
    counting = _CountingClock()
    monkeypatch.setattr(server, "_activity", counting)
    built = server.build_server([])

    async def scenario() -> None:
        await built.list_tools()
        with pytest.raises(ToolError):
            await built.call_tool("session_info", {"bogus": 1})
        await built.call_tool("session_info", {})
        await built.read_resource("kwin://session")

    asyncio.run(scenario())
    assert counting.touched == 1
    assert counting.entered == 3
    assert counting.idle_for() is not None
