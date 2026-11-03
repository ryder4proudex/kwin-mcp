"""kwin-mcp-viewer logic without GTK: session file, handoff protocol, coordinates, keys, worker."""

from __future__ import annotations

import json
import os
import socket
import threading
import time
from typing import TYPE_CHECKING, Any, cast

import pytest

from kwin_mcp.models import ImageMapping, Point
from kwin_mcp.viewer import (
    ControlClient,
    Handoff,
    InputQueue,
    ObserverWorker,
    SessionTarget,
    ViewerError,
    _parse_args,
    image_to_screen,
    key_to_input,
    read_session_file,
    widget_to_image,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from kwin_mcp.core import AutomationEngine


# -- session file -------------------------------------------------------------
def test_session_file_reads_owner_state(tmp_path: Path) -> None:
    path = tmp_path / "s.json"
    path.write_text(json.dumps({"dbus_address": "unix:path=/b", "wayland_socket": "/w", "pid": 1}))
    assert read_session_file(path) == SessionTarget("unix:path=/b", "/w")
    path.write_text(json.dumps({"dbus_address": "unix:path=/b", "wayland_display": "wayland-9"}))
    assert read_session_file(path).wayland_display == "wayland-9"


@pytest.mark.parametrize("content", ["{", "[]", '{"dbus_address": "x"}', '{"wayland_socket": "w"}'])
def test_session_file_refuses_bad_content(tmp_path: Path, content: str) -> None:
    path = tmp_path / "s.json"
    path.write_text(content)
    with pytest.raises(ViewerError):
        read_session_file(path)


def test_missing_session_file_means_not_running(tmp_path: Path) -> None:
    with pytest.raises(ViewerError, match="not running") as exc:
        read_session_file(tmp_path / "none.json")
    assert exc.value.code == "not_running"


# -- handoff protocol -----------------------------------------------------------
class FakeOwner:
    """A session owner on a Unix socket: one lease holder, records every request."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.requests: list[dict[str, Any]] = []
        self.holder: str | None = None
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(path)
        self.sock.listen(8)
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            with conn:
                buf = b""
                while not buf.endswith(b"\n"):
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    buf += chunk
                if not buf:  # a client that refused us hung up without a request
                    continue
                req = json.loads(buf)
                self.requests.append(req)
                conn.sendall(json.dumps(self._reply(req)).encode() + b"\n")

    def _reply(self, req: dict[str, Any]) -> dict[str, Any]:
        op = req["op"]
        if op == "takeover":
            self.holder = req["holder"]
            return {"ok": True, "lease": {"holder": self.holder}}
        if op in ("heartbeat", "human_input") and req.get("holder") != self.holder:
            return {"ok": False, "code": "not_holder", "error": "you do not hold the input lease"}
        if op == "release":
            released = req["holder"] == self.holder
            self.holder = None if released else self.holder
            return {"ok": True, "released": released}
        return {"ok": True, "op": op}

    def close(self) -> None:
        self.sock.close()


@pytest.fixture
def owner(tmp_path: Path) -> Iterator[FakeOwner]:
    fake = FakeOwner(str(tmp_path / "control.sock"))
    yield fake
    fake.close()


def test_handoff_take_input_heartbeat_release(owner: FakeOwner) -> None:
    handoff = Handoff(ControlClient(owner.path), "human:test", lease_s=30)
    with pytest.raises(ViewerError, match="take control first"):
        handoff.send("pointer_click", {"target": {"x": 1, "y": 1}})
    assert owner.requests == []  # nothing is sent before a takeover
    handoff.take()
    assert handoff.holding
    handoff.heartbeat()
    handoff.send("keyboard_type", {"text": "a", "method": "keys"})
    handoff.give_back()
    handoff.give_back()  # idempotent: nothing more is sent
    assert not handoff.holding
    assert [r["op"] for r in owner.requests] == ["takeover", "heartbeat", "human_input", "release"]
    assert owner.requests[0] == {"op": "takeover", "holder": "human:test", "ttl_s": 30}
    assert owner.requests[2]["tool"] == "keyboard_type"


def test_lost_lease_stops_input(owner: FakeOwner) -> None:
    handoff = Handoff(ControlClient(owner.path), "human:test")
    handoff.take()
    owner.holder = "human:someone-else"  # e.g. the lease expired and another human took over
    with pytest.raises(ViewerError) as exc:
        handoff.send("keyboard_press", {"keys": "Return"})
    assert exc.value.code == "not_holder"
    assert not handoff.holding


def test_owner_not_listening(tmp_path: Path) -> None:
    client = ControlClient(str(tmp_path / "absent.sock"))
    with pytest.raises(ViewerError) as exc:
        client.request("status")
    assert exc.value.code == "not_running"


def test_control_socket_of_another_user_is_refused(owner: FakeOwner) -> None:
    impostor = ControlClient(owner.path, peer_uid_of=lambda _sock: os.getuid() + 1)
    with pytest.raises(ViewerError) as exc:
        impostor.request("takeover", holder="human:test", ttl_s=30)
    assert exc.value.code == "forbidden"
    wrong_owner = ControlClient(owner.path, expected_uid=os.getuid() + 1)
    with pytest.raises(ViewerError) as exc:
        wrong_owner.request("status")
    assert exc.value.code == "forbidden"
    assert owner.requests == []  # nothing, typed text least of all, reached the server
    handoff = Handoff(impostor, "human:test")
    with pytest.raises(ViewerError):
        handoff.take()
    assert not handoff.holding


def _resetting_owner(path: str) -> socket.socket:
    """A server that closes each connection with the request unread: the client sees a reset."""
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(path)
    srv.listen(8)

    def serve() -> None:
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            time.sleep(0.05)  # the request has arrived; closing with it unread resets the peer
            conn.close()

    threading.Thread(target=serve, daemon=True).start()
    return srv


def test_connection_loss_drops_control(tmp_path: Path) -> None:
    path = str(tmp_path / "reset.sock")
    srv = _resetting_owner(path)
    try:
        handoff = Handoff(ControlClient(path), "human:test")
        handoff.holding = True
        with pytest.raises(ViewerError) as exc:
            handoff.send("keyboard_type", {"text": "a"})
        assert exc.value.code == "disconnected"
        assert not handoff.holding  # the UI no longer claims the AI is paused
        handoff.holding = True
        with pytest.raises(ViewerError):
            handoff.heartbeat()
        assert not handoff.holding
    finally:
        srv.close()


class GatedClient:
    """In-memory owner: records requests; human_input waits for `gate` when it is set."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, dict[str, Any]]] = []
        self.gate: threading.Event | None = None
        self.entered = threading.Event()

    def request(self, op: str, **fields: Any) -> dict[str, Any]:
        self.requests.append((op, fields))
        if op == "human_input" and self.gate is not None:
            self.entered.set()
            self.gate.wait(5)
        return {"ok": True}


def _wait(cond: Any) -> None:
    for _ in range(500):
        if cond():
            return
        time.sleep(0.01)
    raise AssertionError("condition not reached")


def test_input_queue_keeps_order() -> None:
    client = GatedClient()
    handoff = Handoff(cast("ControlClient", client), "human:test")
    handoff.take()
    errors: list[str] = []
    inputs = InputQueue(handoff, errors.append)
    try:
        for i in range(50):
            assert inputs.submit("keyboard_type", {"text": str(i)})
        _wait(lambda: len(client.requests) == 51)
    finally:
        inputs.close()
    sent = [f["args"]["text"] for op, f in client.requests if op == "human_input"]
    assert sent == [str(i) for i in range(50)] and errors == []


def test_queued_input_never_crosses_into_a_new_takeover() -> None:
    client = GatedClient()
    handoff = Handoff(cast("ControlClient", client), "human:test")
    handoff.take()
    inputs = InputQueue(handoff, lambda _note: None)
    try:
        client.gate = threading.Event()
        inputs.submit("keyboard_type", {"text": "a"})
        client.entered.wait(5)  # "a" is in flight
        inputs.submit("keyboard_press", {"keys": "Return"})  # queued behind it
        handoff.give_back()
        handoff.take()  # a quick new takeover
        client.gate.set()
        _wait(lambda: inputs._q.empty())
        time.sleep(0.05)
    finally:
        inputs.close()
    ops = [(op, f.get("tool")) for op, f in client.requests]
    assert ops == [
        ("takeover", None),
        ("human_input", "keyboard_type"),
        ("release", None),
        ("takeover", None),
    ]  # the stale Return was dropped
    assert not inputs.submit("x", {}) or handoff.holding


# -- coordinates ----------------------------------------------------------------
def test_widget_to_image_letterbox() -> None:
    # 1600x900 image in a 800x800 widget: scale 0.5, 175 px bars top and bottom.
    assert widget_to_image(400, 400, 800, 800, 1600, 900) == (800, 900 / 2)
    assert widget_to_image(400, 100, 800, 800, 1600, 900) is None
    assert widget_to_image(0, 175, 800, 800, 1600, 900) == (0, 0)
    assert widget_to_image(1, 1, 0, 0, 1600, 900) is None


def test_image_to_screen_uses_mapping() -> None:
    mapping = ImageMapping(scale=0.5, origin=Point(x=10, y=20))
    assert image_to_screen(100, 50, mapping) == (210, 120)


# -- keys -----------------------------------------------------------------------
def _key(name: str, char: str = "", **mods: bool) -> tuple[str, dict[str, Any]] | None:
    flags = {"ctrl": False, "alt": False, "super_": False, "shift": False, **mods}
    return key_to_input(name, char, **flags)


def test_keys_map_to_kwin_tools() -> None:
    assert _key("a", "a") == ("keyboard_type", {"text": "a", "method": "keys"})
    assert _key("A", "A", shift=True) == ("keyboard_type", {"text": "A", "method": "keys"})
    assert _key("eacute", chr(0xE9)) == ("keyboard_type", {"text": chr(0xE9), "method": "unicode"})
    assert _key("space", " ") == ("keyboard_type", {"text": " ", "method": "keys"})
    assert _key("Return") == ("keyboard_press", {"keys": "return"})
    assert _key("KP_Enter") == ("keyboard_press", {"keys": "return"})
    assert _key("ISO_Left_Tab", shift=True) == ("keyboard_press", {"keys": "shift+tab"})
    assert _key("c", "c", ctrl=True) == ("keyboard_press", {"keys": "ctrl+c"})
    assert _key("T", "T", ctrl=True, shift=True) == ("keyboard_press", {"keys": "ctrl+shift+t"})
    assert _key("F5") == ("keyboard_press", {"keys": "f5"})
    assert _key("space", " ", ctrl=True) == ("keyboard_press", {"keys": "ctrl+space"})


def test_modifier_and_unknown_keys_are_ignored() -> None:
    assert _key("Shift_L") is None
    assert _key("Control_R", ctrl=True) is None
    assert _key("XF86AudioMute") is None


# -- observer worker ------------------------------------------------------------
class FakeEngine:
    def __init__(self, log: list[str], fail_capture: bool = False) -> None:
        self.log = log
        self.fail_capture = fail_capture

    def session_connect(self, **kwargs: Any) -> None:
        self.log.append(f"connect observe={kwargs.get('observe')} {kwargs.get('wayland_display')}")

    def screen_capture(self, max_edge: int | None = None) -> str:
        self.log.append(f"capture {max_edge}")
        if self.fail_capture:
            msg = "bus gone"
            raise RuntimeError(msg)
        return "frame"

    def session_stop(self) -> None:
        self.log.append("stop")


def _run(worker: ObserverWorker) -> tuple[Any, BaseException | None]:
    done = threading.Event()
    out: list[Any] = []

    def finish(result: Any, error: BaseException | None) -> None:
        out.extend([result, error])
        done.set()

    worker.submit(worker.capture, finish)
    assert done.wait(5)
    return out[0], out[1]


def test_worker_attaches_as_observer_and_detaches() -> None:
    log: list[str] = []
    engines = [FakeEngine(log, fail_capture=True), FakeEngine(log)]
    worker = ObserverWorker(
        lambda: SessionTarget("unix:path=/b", "/w"),
        engine_factory=lambda: cast("AutomationEngine", engines.pop(0)),
        max_edge=800,
    )
    result, error = _run(worker)
    assert result is None
    assert isinstance(error, RuntimeError)
    result, error = _run(worker)  # re-attaches after a failed capture (session restarted)
    assert (result, error) == ("frame", None)
    worker.close()
    assert log == [
        "connect observe=True /w",
        "capture 800",
        "stop",
        "connect observe=True /w",
        "capture 800",
        "stop",
    ]


def test_cli_arguments() -> None:
    ns = _parse_args(["--session-file", "/s.json", "--control-socket", "/c.sock"])
    assert ns.session_file == "/s.json"
    assert ns.control_socket == "/c.sock"
    with pytest.raises(SystemExit):
        _parse_args(["--dbus-address", "unix:path=/b"])
    with pytest.raises(SystemExit):
        _parse_args(["--session-file", "/s.json", "--lease", "1"])
