"""kwin-mcp-viewer: watch a KWin session as a read-only observer, with optional handoff controls.

The viewer attaches with ``session_connect(observe=True)``: it opens no input
backend and no clipboard, so watching can never inject input or kill the
session. It shows the screen by polling ``screen_capture``.

Taking control is the session owner's decision. With ``--control-socket`` the
viewer speaks the handoff protocol to the owner (one JSON line per request on
a local Unix socket, one JSON line back):

- ``{"op": "takeover", "holder": H, "ttl_s": T}``: the owner pauses AI input,
  releases keys and buttons the AI holds, and grants H a lease of T seconds;
- ``{"op": "heartbeat", "holder": H, "ttl_s": T}``: renew the lease;
- ``{"op": "release", "holder": H}``: give control back at once;
- ``{"op": "human_input", "holder": H, "tool": NAME, "args": {...}}``: one
  pointer or keyboard tool call, run by the owner only while H holds the lease;
- ``{"op": "status"}``: optional, shown in the status line.

Replies are ``{"ok": true, ...}`` or ``{"ok": false, "code": C, "error": E}``.
The viewer talks only to a control socket served by its own user (``SO_PEERCRED``),
so a socket planted by someone else never receives what the human types.
The viewer never sends input itself. If it dies, it stops sending heartbeats,
the lease expires and the owner resumes. Nothing listens on the network.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import queue
import socket
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kwin_mcp.input import _EVDEV_KEY_MAP

if TYPE_CHECKING:
    from collections.abc import Callable

    from kwin_mcp.core import AutomationEngine
    from kwin_mcp.models import Frame, ImageMapping

MAX_REPLY = 4 * 1024 * 1024
CONNECT_TIMEOUT_S = 5.0
REQUEST_TIMEOUT_S = 100.0
DEFAULT_LEASE_S = 60.0
DEFAULT_INTERVAL_S = 1.0
DEFAULT_MAX_EDGE = 1600

# GDK key names (lower-cased) that differ from kwin-mcp's key names.
_KEY_ALIASES = {
    "kp_enter": "return",
    "iso_left_tab": "tab",
    "prior": "page_up",
    "next": "page_down",
    "kp_up": "up",
    "kp_down": "down",
    "kp_left": "left",
    "kp_right": "right",
    "kp_home": "home",
    "kp_end": "end",
    "kp_delete": "delete",
}
_MODIFIER_NAMES = frozenset(
    {
        "shift_l",
        "shift_r",
        "control_l",
        "control_r",
        "alt_l",
        "alt_r",
        "super_l",
        "super_r",
        "meta_l",
        "meta_r",
        "iso_level3_shift",
    }
)


class ViewerError(Exception):
    """A session file, control socket or handoff error with a message for the human."""

    def __init__(self, message: str, code: str = "error") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class SessionTarget:
    """Where the observed session lives."""

    dbus_address: str
    wayland_display: str


def read_session_file(path: str | os.PathLike[str]) -> SessionTarget:
    """Read the owner's session file: JSON with ``dbus_address`` and ``wayland_socket``.

    ``wayland_display`` is accepted as a synonym of ``wayland_socket``.
    """
    try:
        data = json.loads(Path(path).read_text())
    except FileNotFoundError:
        msg = f"session file {path} does not exist: the session is not running"
        raise ViewerError(msg, "not_running") from None
    except (OSError, ValueError) as exc:
        msg = f"cannot read session file {path}: {exc}"
        raise ViewerError(msg) from None
    if not isinstance(data, dict):
        msg = f"session file {path} is not a JSON object"
        raise ViewerError(msg)
    dbus = data.get("dbus_address")
    wayland = data.get("wayland_socket") or data.get("wayland_display")
    if not isinstance(dbus, str) or not dbus or not isinstance(wayland, str) or not wayland:
        msg = f"session file {path} has no dbus_address and wayland_socket"
        raise ViewerError(msg, "not_running")
    return SessionTarget(dbus, wayland)


# Codes after which the viewer no longer holds control: it must take over again.
LOST_CONTROL_CODES = frozenset(
    {"not_holder", "not_running", "crashed", "disconnected", "forbidden", "protocol"}
)


def peer_uid(sock: socket.socket) -> int:
    """UID of the process serving the other end of a connected Unix socket."""
    creds = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
    return int.from_bytes(creds[4:8], sys.byteorder)


class ControlClient:
    """The viewer side of the handoff protocol (see the module docstring)."""

    def __init__(
        self,
        path: str,
        timeout: float = REQUEST_TIMEOUT_S,
        expected_uid: int | None = None,
        peer_uid_of: Callable[[socket.socket], int] = peer_uid,
    ) -> None:
        self.path = path
        self.timeout = timeout
        self.expected_uid = os.getuid() if expected_uid is None else expected_uid
        self._peer_uid_of = peer_uid_of

    def _check_owner(self, sock: socket.socket) -> None:
        """Refuse a socket file or a server that does not belong to the expected user."""
        try:
            file_uid = os.stat(self.path).st_uid
        except OSError:
            file_uid = None
        server_uid = self._peer_uid_of(sock)
        if file_uid != self.expected_uid or server_uid != self.expected_uid:
            msg = (
                f"refused: {self.path} is served by uid {server_uid} (file owner {file_uid}), "
                f"not by uid {self.expected_uid}; nothing was sent"
            )
            raise ViewerError(msg, "forbidden")

    def request(self, op: str, **fields: Any) -> dict[str, Any]:
        """Send one request and return the reply; raise ViewerError on ok=false."""
        payload = {"op": op, **{k: v for k, v in fields.items() if v is not None}}
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(CONNECT_TIMEOUT_S)
        buf = bytearray()
        try:
            try:
                sock.connect(self.path)
            except (FileNotFoundError, ConnectionRefusedError):
                msg = f"the session owner is not listening on {self.path}"
                raise ViewerError(msg, "not_running") from None
            self._check_owner(sock)
            sock.settimeout(self.timeout)
            sock.sendall(json.dumps(payload, ensure_ascii=False).encode() + b"\n")
            while not buf.endswith(b"\n"):
                chunk = sock.recv(65536)
                if not chunk:
                    break
                buf += chunk
                if len(buf) > MAX_REPLY:
                    msg = "reply too large"
                    raise ViewerError(msg, "protocol")
        except TimeoutError:
            msg = f"the session owner did not answer {op} in {self.timeout:.0f} s"
            raise ViewerError(msg, "timeout") from None
        except OSError as exc:  # reset, broken pipe, ...: the owner went away mid-request
            msg = f"lost the connection to the session owner during {op}: {exc}"
            raise ViewerError(msg, "disconnected") from None
        finally:
            sock.close()
        if not buf.endswith(b"\n"):
            msg = f"the session owner closed the connection without answering {op}"
            raise ViewerError(msg, "disconnected")
        try:
            reply = json.loads(buf)
        except ValueError:
            msg = f"the session owner sent no valid reply to {op}"
            raise ViewerError(msg, "protocol") from None
        if not isinstance(reply, dict):
            msg = f"the session owner sent no valid reply to {op}"
            raise ViewerError(msg, "protocol")
        if not reply.get("ok"):
            raise ViewerError(
                str(reply.get("error") or f"{op} refused"), str(reply.get("code") or "error")
            )
        return reply


class Handoff:
    """One human's lease on the owner's input: take, renew, use and give back.

    ``generation`` changes whenever control is taken, given back or lost, so input
    queued under one takeover is never sent under a later one. Every request runs
    under one lock together with the state check before it and the state change
    after it: a release or a new takeover waits for an input call in flight, and a
    late failure of an old request can never clear a newer takeover.
    """

    def __init__(
        self, client: ControlClient, holder: str, lease_s: float = DEFAULT_LEASE_S
    ) -> None:
        self.client = client
        self.holder = holder
        self.lease_s = lease_s
        self.holding = False
        self.generation = 0
        # _io orders take, give_back and send: a release or a new takeover waits for input
        # already in flight. _state guards holding/generation and is never held across I/O, so
        # heartbeats (and closing) never wait behind a slow input call.
        self._io = threading.RLock()
        self._state = threading.Lock()

    def _lose(self, generation: int | None = None) -> None:
        """Drop control (only if still in ``generation``, when given)."""
        with self._state:
            if self.holding and (generation is None or generation == self.generation):
                self.holding = False
                self.generation += 1

    def _current(self) -> int | None:
        with self._state:
            return self.generation if self.holding else None

    def take(self) -> dict[str, Any]:
        """Take control: the owner pauses the AI and releases its held input."""
        with self._io:
            reply = self.client.request("takeover", holder=self.holder, ttl_s=self.lease_s)
            with self._state:
                self.holding = True
                self.generation += 1
            return reply

    def heartbeat(self) -> None:
        """Renew the lease; any failure means control is lost (take over again).

        Runs beside input in flight; a failure of a heartbeat sent before a give-back and
        a new takeover does not drop the new takeover.
        """
        generation = self._current()
        if generation is None:
            return
        try:
            self.client.request("heartbeat", holder=self.holder, ttl_s=self.lease_s)
        except ViewerError:
            self._lose(generation)
            raise

    def give_back(self) -> None:
        """Return control at once (idempotent), after input already in flight."""
        with self._io:
            if self._current() is None:
                return
            self._lose()
            self.client.request("release", holder=self.holder)

    def abandon(self) -> None:
        """Return control without waiting for input in flight (the viewer is closing).

        Queued input is dropped at once; a call already in flight is refused by the owner
        once the lease is released (or freed with it), and no new takeover follows.
        """
        if self._current() is None:
            return
        self._lose()
        self.client.request("release", holder=self.holder)

    def send(
        self, tool: str, args: dict[str, Any], generation: int | None = None
    ) -> dict[str, Any]:
        """Ask the owner to run one input tool for this holder (in ``generation``, if given)."""
        with self._io:
            current = self._current()
            if current is None or (generation is not None and generation != current):
                msg = "take control first: input goes to the session only while you hold the lease"
                raise ViewerError(msg, "not_holder")
            try:
                return self.client.request("human_input", holder=self.holder, tool=tool, args=args)
            except ViewerError as exc:
                if exc.code in LOST_CONTROL_CODES:
                    self._lose(current)
                raise


class InputQueue:
    """Sends the human's input to the owner one call at a time, in the order it happened.

    Each event is stamped with the takeover generation it was made in; events left
    over from an earlier takeover (given back, lost, or taken again) are dropped.
    """

    def __init__(self, handoff: Handoff, on_error: Callable[[str], None]) -> None:
        self.handoff = handoff
        self._on_error = on_error
        self._q: queue.Queue[tuple[int, str, dict[str, Any]] | None] = queue.Queue()
        self._thread = threading.Thread(target=self._run, name="kwin-mcp-viewer-input", daemon=True)
        self._thread.start()

    def submit(self, tool: str, args: dict[str, Any]) -> bool:
        """Queue one input call; False when control is not held (nothing queued)."""
        if not self.handoff.holding:
            return False
        self._q.put((self.handoff.generation, tool, args))
        return True

    def _run(self) -> None:
        while True:
            item = self._q.get()
            if item is None:
                return
            generation, tool, args = item
            if not self.handoff.holding or generation != self.handoff.generation:
                continue
            try:
                self.handoff.send(tool, args, generation)
            except ViewerError as exc:
                self._on_error(f"{tool}: {exc}")

    def close(self, timeout: float = 5.0) -> None:
        self._q.put(None)
        self._thread.join(timeout)


def click_to_input(point: tuple[int, int], gdk_button: int) -> tuple[str, dict[str, Any]]:
    """One press in the viewer becomes exactly one click in the session.

    A double click arrives as two presses (n_press 1, then 2) and is forwarded as two
    single clicks, which the session's toolkit counts as a double click again.
    """
    button = {1: "left", 2: "middle", 3: "right"}.get(gdk_button, "left")
    return "pointer_click", {"target": {"x": point[0], "y": point[1]}, "button": button}


def widget_to_image(
    wx: float, wy: float, widget_w: float, widget_h: float, image_w: int, image_h: int
) -> tuple[float, float] | None:
    """Map a point on a contain-fitted picture to image pixels (None on the letterbox)."""
    if widget_w <= 0 or widget_h <= 0 or image_w <= 0 or image_h <= 0:
        return None
    scale = min(widget_w / image_w, widget_h / image_h)
    ox = (widget_w - image_w * scale) / 2
    oy = (widget_h - image_h * scale) / 2
    ix = (wx - ox) / scale
    iy = (wy - oy) / scale
    if not (0 <= ix < image_w and 0 <= iy < image_h):
        return None
    return ix, iy


def image_to_screen(ix: float, iy: float, mapping: ImageMapping) -> tuple[int, int]:
    """Image pixels to logical screen coordinates (logical = origin + px / scale)."""
    return (
        round(mapping.origin.x + ix / mapping.scale),
        round(mapping.origin.y + iy / mapping.scale),
    )


def key_to_input(
    keyval_name: str | None, char: str, *, ctrl: bool, alt: bool, super_: bool, shift: bool
) -> tuple[str, dict[str, Any]] | None:
    """Translate one key press in the viewer into a kwin-mcp tool call (None: ignore).

    Named keys and chords with ctrl/alt/super become ``keyboard_press``; plain
    printable characters become ``keyboard_type`` (``unicode`` for non-ASCII).
    """
    name = (keyval_name or "").lower()
    if name in _MODIFIER_NAMES:
        return None
    name = _KEY_ALIASES.get(name, name)
    mods = [m for m, on in (("ctrl", ctrl), ("alt", alt), ("super", super_)) if on]
    named = len(name) > 1 and name in _EVDEV_KEY_MAP and name != "space"
    if named or (name == "space" and mods):
        chord = [*mods, *(["shift"] if shift else []), name]
        return "keyboard_press", {"keys": "+".join(chord)}
    if mods:
        base = char.lower() if char else (name if len(name) == 1 else "")
        if len(base) == 1 and base.isascii() and base.isprintable():
            chord = [*mods, *(["shift"] if shift else []), base]
            return "keyboard_press", {"keys": "+".join(chord)}
        return None
    if name == "space":
        char = " "
    if len(char) == 1 and char.isprintable():
        method = "keys" if char.isascii() else "unicode"
        return "keyboard_type", {"text": char, "method": method}
    return None


class ObserverWorker:
    """Owns the observer engine on one thread: every engine call runs there, in order."""

    def __init__(
        self,
        target: Callable[[], SessionTarget],
        engine_factory: Callable[[], AutomationEngine] | None = None,
        max_edge: int = DEFAULT_MAX_EDGE,
    ) -> None:
        self._target = target
        self._factory = engine_factory
        self.max_edge = max_edge
        self._engine: AutomationEngine | None = None
        self._jobs: queue.Queue[
            tuple[Callable[[], Any], Callable[[Any, BaseException | None], None]] | None
        ] = queue.Queue()
        self._thread = threading.Thread(
            target=self._run, name="kwin-mcp-viewer-observer", daemon=True
        )
        self._thread.start()

    def _run(self) -> None:
        while True:
            job = self._jobs.get()
            if job is None:
                self._detach()
                return
            fn, done = job
            try:
                result = fn()
            except Exception as exc:
                done(None, exc)
            else:
                done(result, None)

    def submit(
        self, fn: Callable[[], Any], done: Callable[[Any, BaseException | None], None]
    ) -> None:
        """Queue ``fn`` for the observer thread; ``done(result, error)`` runs there too."""
        self._jobs.put((fn, done))

    def _attach(self) -> AutomationEngine:
        if self._engine is not None:
            return self._engine
        if self._factory is not None:
            engine = self._factory()
        else:
            from kwin_mcp.core import AutomationEngine

            engine = AutomationEngine()
        target = self._target()
        engine.session_connect(
            dbus_address=target.dbus_address, wayland_display=target.wayland_display, observe=True
        )
        self._engine = engine
        return engine

    def _detach(self) -> None:
        engine, self._engine = self._engine, None
        if engine is not None:
            with contextlib.suppress(Exception):
                engine.session_stop()

    def capture(self) -> Frame:
        """One frame of the observed screen (runs on the observer thread)."""
        engine = self._attach()
        try:
            return engine.screen_capture(max_edge=self.max_edge)
        except Exception:
            # The session may have restarted (new bus and socket): attach again next time.
            self._detach()
            raise

    def close(self, timeout: float = 5.0) -> None:
        """Detach the observer (never stops the observed session) and end the thread."""
        self._jobs.put(None)
        self._thread.join(timeout)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        prog="kwin-mcp-viewer",
        description=(
            "Watch a KWin session read-only (session_connect observe=true). With --control-socket, "
            "take over input through the session owner and give it back."
        ),
    )
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument(
        "--session-file", help="owner's JSON file with dbus_address and wayland_socket"
    )
    src.add_argument("--dbus-address", help="D-Bus address of the observed session")
    ap.add_argument(
        "--wayland-display", help="Wayland socket of the observed session (with --dbus-address)"
    )
    ap.add_argument(
        "--control-socket", help="owner's Unix socket for takeover/release (omit: watch only)"
    )
    ap.add_argument(
        "--interval", type=float, default=DEFAULT_INTERVAL_S, help="seconds between frames"
    )
    ap.add_argument(
        "--max-edge", type=int, default=DEFAULT_MAX_EDGE, help="longest frame edge in pixels"
    )
    ap.add_argument("--lease", type=float, default=DEFAULT_LEASE_S, help="takeover lease seconds")
    ns = ap.parse_args(argv)
    if ns.dbus_address and not ns.wayland_display:
        ap.error("--dbus-address needs --wayland-display")
    if ns.interval < 0.2 or ns.lease < 10:
        ap.error("--interval must be >= 0.2 and --lease >= 10")
    return ns


def main(argv: list[str] | None = None) -> int:
    """Entry point of ``kwin-mcp-viewer``."""
    ns = _parse_args(argv)
    if ns.session_file:
        path = ns.session_file

        def target() -> SessionTarget:
            return read_session_file(path)

        target()  # fail early with a clear message
    else:
        fixed = SessionTarget(ns.dbus_address, ns.wayland_display)

        def target() -> SessionTarget:
            return fixed

    from kwin_mcp.viewer_gtk import run

    handoff = None
    if ns.control_socket:
        holder = f"human:viewer:{socket.gethostname()}:{os.getpid()}"
        handoff = Handoff(ControlClient(ns.control_socket), holder, ns.lease)
    worker = ObserverWorker(target, max_edge=ns.max_edge)
    try:
        return run(worker, handoff, ns.interval)
    finally:
        if handoff is not None:
            with contextlib.suppress(ViewerError):
                handoff.give_back()
        worker.close()


def cli() -> None:
    """Console script wrapper with readable errors."""
    try:
        sys.exit(main())
    except ViewerError as exc:
        print(f"kwin-mcp-viewer: {exc}", file=sys.stderr)
        sys.exit(1)
