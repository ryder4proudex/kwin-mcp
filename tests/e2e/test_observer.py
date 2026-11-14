"""Observer attach and kwin-mcp-viewer handoff against test-owned virtual KWin sessions.

Every session here is started by the test (``session_start``); nothing attaches
to the host desktop. Session A is the owner's session; an observer engine
attaches to it read-only. For the viewer, session C is a second virtual KWin
that displays the viewer window, and a fake owner serves the handoff protocol
by running ``human_input`` on A.
"""

from __future__ import annotations

import json
import os
import shlex
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from _suite_policy import require_or_skip
from visual_harness import requires_native_capture

from kwin_mcp.core import OBSERVER_CAPTURE_TEXT, OBSERVER_TEXT, AutomationEngine
from kwin_mcp.models import Locator, Point, RefAddr, Wait, WindowSelector
from kwin_mcp.screenshot import ScreenShot2AbsentError

if TYPE_CHECKING:
    from collections.abc import Iterator

E2E_DIR = Path(__file__).parent
GUI_PROBE = E2E_DIR / "gui_probe.py"
WIDTH, HEIGHT = 1280, 800
TIMEOUT_MS = 20000
GTK4_LIMIT = "GTK 4 unavailable: kwin-mcp-viewer needs the Gtk 4.0 typelib"
CAPTURE_LIMIT = "KWin limit: observer screen capture needs KWin 6.8 or later"


def _require_gtk4() -> None:
    probe = subprocess.run(
        [sys.executable, "-c", "import gi; gi.require_version('Gtk', '4.0')"],
        capture_output=True,
        text=True,
        check=False,
    )
    if probe.returncode != 0:
        require_or_skip("gtk4", f"{GTK4_LIMIT}: {probe.stderr.strip()[-200:]}", os.environ)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


@pytest.fixture
def owner() -> Iterator[AutomationEngine]:
    engine = AutomationEngine()
    engine.session_start(screen_width=WIDTH, screen_height=HEIGHT)
    try:
        yield engine
    finally:
        engine.session_stop()


@pytest.fixture
def probe_owner(owner: AutomationEngine) -> AutomationEngine:
    owner.app_launch(f"{shlex.quote(sys.executable)} {shlex.quote(str(GUI_PROBE))}")
    found = owner.element_query(
        locator=Locator(app="gui_probe.py"), wait=Wait(timeout_ms=TIMEOUT_MS)
    )
    assert found.wait is not None and found.wait.satisfied, found.render()[:500]
    return owner


def _observe(owner: AutomationEngine) -> AutomationEngine:
    info = owner.session_info().session
    assert info is not None
    observer = AutomationEngine()
    attached = observer.session_connect(
        dbus_address=info.dbus_address, wayland_display=info.wayland_socket, observe=True
    )
    assert attached.observer, attached.render()
    assert attached.input_backend is None
    assert attached.clipboard is False
    return observer


def test_observer_reads_but_cannot_act(probe_owner: AutomationEngine) -> None:
    observer = _observe(probe_owner)
    try:
        windows = observer.window_list().windows
        assert windows, "the observer sees the owner's windows"
        elements = observer.element_query(locator=Locator(app="gui_probe.py"))
        assert elements.elements
        refusals = [
            lambda: observer.pointer_click(Point(x=10, y=10)),
            lambda: observer.keyboard_type("x"),
            lambda: observer.keyboard_press("Return"),
            lambda: observer.clipboard_read(),
            lambda: observer.app_launch("true"),
            lambda: observer.window_focus(WindowSelector(id=windows[0].id)),
            lambda: observer.window_close(WindowSelector(id=windows[0].id)),
            lambda: observer.element_act(RefAddr(ref=elements.elements[0].ref), "click"),
        ]
        for refused in refusals:
            with pytest.raises(RuntimeError) as exc:
                refused()
            assert str(exc.value) == OBSERVER_TEXT
    finally:
        stopped = observer.session_stop()
    assert stopped.stopped
    # Detaching the observer left the owner's session and app running.
    info = probe_owner.session_info()
    assert info.session is not None
    assert [a for a in info.apps if _alive(a.pid)], info.render()
    again = probe_owner.element_query(locator=Locator(app="gui_probe.py"))
    assert again.elements
    probe_owner.pointer_move(Point(x=5, y=5))  # the owner keeps its input


def test_observer_captures_the_screen(probe_owner: AutomationEngine) -> None:
    """KWin 6.8+ must capture; older KWin must refuse with the stated requirement.

    Only the absent-ScreenShot2 error is the known limit: any other capture failure
    fails the test on every KWin version.
    """
    native = requires_native_capture()
    observer = _observe(probe_owner)
    try:
        try:
            frame = observer.screen_capture()
        except ScreenShot2AbsentError as exc:
            if native:
                raise
            assert str(exc).endswith(OBSERVER_CAPTURE_TEXT), str(exc)
            require_or_skip("observer-capture", CAPTURE_LIMIT, os.environ)
        assert frame.width > 0
        assert Path(frame.path).is_file()
    finally:
        observer.session_stop()


class FakeOwner:
    """The owner's side of the handoff protocol, running human_input on session A."""

    def __init__(self, path: str, engine: AutomationEngine) -> None:
        self.engine = engine
        self.holder: str | None = None
        self.requests: list[dict[str, Any]] = []
        self.lock = threading.Lock()
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(path)
        os.chmod(path, 0o600)
        self.sock.listen(8)
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            with conn:
                buf = b""
                while not buf.endswith(b"\n"):
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    buf += chunk
                req = json.loads(buf)
                with self.lock:
                    self.requests.append(req)
                    reply = self._handle(req)
                conn.sendall(json.dumps(reply).encode() + b"\n")

    def _handle(self, req: dict[str, Any]) -> dict[str, Any]:
        op = req.get("op")
        if op == "takeover":
            self.holder = req["holder"]
            return {"ok": True}
        if op == "release":
            self.holder = None
            return {"ok": True, "released": True}
        if op in ("heartbeat", "human_input") and req.get("holder") != self.holder:
            return {"ok": False, "code": "not_holder", "error": "no lease"}
        if op == "human_input":
            tool = req["tool"]
            args = req["args"]
            if tool == "pointer_click":
                t = args["target"]
                self.engine.pointer_click(
                    Point(x=t["x"], y=t["y"]), button=args.get("button", "left")
                )
            elif tool == "keyboard_type":
                self.engine.keyboard_type(args["text"], method=args.get("method", "keys"))
            elif tool == "keyboard_press":
                self.engine.keyboard_press(args["keys"])
            return {"ok": True}
        return {"ok": True}

    def ops(self) -> list[str]:
        with self.lock:
            return [str(r["op"]) for r in self.requests]

    def inputs(self) -> list[dict[str, Any]]:
        with self.lock:
            return [r for r in self.requests if r["op"] == "human_input"]

    def close(self) -> None:
        self.sock.close()


def _wait_until(cond: Any, timeout: float = 20.0) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return
        time.sleep(0.2)
    raise AssertionError("condition not met in time")


def _button(display: AutomationEngine, name: str) -> Point:
    found = display.element_query(
        locator=Locator(query=name, role="button"),
        wait=Wait(timeout_ms=TIMEOUT_MS),
    )
    assert found.elements, found.render()[:800]
    rect = found.elements[0].rect
    assert rect is not None
    return Point(x=rect.x + rect.width // 2, y=rect.y + rect.height // 2)


def test_viewer_watches_and_hands_off(probe_owner: AutomationEngine, tmp_path: Path) -> None:
    _require_gtk4()
    info = probe_owner.session_info().session
    assert info is not None
    state = tmp_path / "owner.json"
    state.write_text(
        json.dumps({"dbus_address": info.dbus_address, "wayland_socket": info.wayland_socket})
    )
    control = str(tmp_path / "owner.sock")
    fake = FakeOwner(control, probe_owner)
    display = AutomationEngine()
    display.session_start(screen_width=WIDTH, screen_height=HEIGHT)
    try:
        command = " ".join(
            shlex.quote(a)
            for a in [
                sys.executable,
                "-c",
                "from kwin_mcp.viewer import cli; cli()",
                "--session-file",
                str(state),
                "--control-socket",
                control,
                "--interval",
                "0.5",
            ]
        )
        display.app_launch(command)
        take = _button(display, "Take over")
        # Watching only: a click on the picture sends nothing.
        display.pointer_click(Point(x=WIDTH // 2, y=HEIGHT // 2))
        time.sleep(1.0)
        assert fake.ops() == []
        display.pointer_click(take)
        _wait_until(lambda: fake.holder is not None)
        give_back = _button(display, "Give back control")
        # Click the centre of the picture: the owner receives screen coordinates on A.
        windows = display.window_list(WindowSelector(caption="kwin-mcp viewer")).windows
        assert len(windows) == 1, windows
        frame = windows[0].client
        display.pointer_click(
            Point(x=frame.x + frame.width // 2, y=frame.y + frame.height * 3 // 5)
        )
        _wait_until(lambda: any(r["tool"] == "pointer_click" for r in fake.inputs()))
        click = next(r for r in fake.inputs() if r["tool"] == "pointer_click")
        x, y = click["args"]["target"]["x"], click["args"]["target"]["y"]
        assert 0 <= x < WIDTH and 0 <= y < HEIGHT, click
        assert abs(x - WIDTH // 2) < WIDTH // 8, click
        display.keyboard_type("a")
        _wait_until(lambda: any(r["tool"] == "keyboard_type" for r in fake.inputs()))
        typed = next(r for r in fake.inputs() if r["tool"] == "keyboard_type")
        assert typed["args"] == {"text": "a", "method": "keys"}
        display.pointer_click(give_back)
        _wait_until(lambda: fake.holder is None)
        assert "release" in fake.ops()
        # Closing the viewer detaches its observer; the owner's session keeps running.
        viewer_windows = display.window_list(WindowSelector(caption="kwin-mcp viewer")).windows
        assert viewer_windows
        display.window_close(WindowSelector(id=viewer_windows[0].id))
        _wait_until(
            lambda: not display.window_list(WindowSelector(caption="kwin-mcp viewer")).windows
        )
        assert probe_owner.session_info().session is not None
        assert probe_owner.element_query(locator=Locator(app="gui_probe.py")).elements
    finally:
        display.session_stop()
        fake.close()
