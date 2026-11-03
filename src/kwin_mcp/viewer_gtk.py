"""GTK 4 window of kwin-mcp-viewer: the observed screen, a status line and the handoff button.

Kept apart from ``viewer`` so the protocol, mapping and key logic import without GTK.
Input reaches the observed session only through ``Handoff.send`` (the owner runs it)
and only while the human holds the lease; otherwise clicks and keys are ignored.
"""

from __future__ import annotations

import contextlib
import os
import threading
from typing import TYPE_CHECKING, Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, Gio, GLib, Gtk  # noqa: E402

from kwin_mcp.viewer import (  # noqa: E402
    Handoff,
    InputQueue,
    ObserverWorker,
    ViewerError,
    click_to_input,
    image_to_screen,
    key_to_input,
    widget_to_image,
)

if TYPE_CHECKING:
    from kwin_mcp.models import Frame

APP_ID = "io.github.kwin_mcp.Viewer"
WATCH_ONLY = "Watching (read-only). Start the viewer with --control-socket to take over."
OBSERVING = "Watching (read-only). The AI keeps control until you take over."
HOLDING = "You have control: clicks, scrolls and keys go to the session. The AI is paused."


class ViewerWindow:
    """One viewer window; all GTK calls run on the main loop."""

    def __init__(
        self, app: Gtk.Application, worker: ObserverWorker, handoff: Handoff | None, interval: float
    ) -> None:
        self.worker = worker
        self.handoff = handoff
        self.interval_ms = max(int(interval * 1000), 200)
        self.frame: Frame | None = None
        self._capturing = False
        self._pointer: tuple[float, float] | None = None
        # One FIFO sender: input keeps its order and never outlives the takeover it was made in.
        self._inputs = InputQueue(handoff, self._input_failed) if handoff is not None else None

        self.window = Gtk.ApplicationWindow(application=app, title="kwin-mcp viewer")
        self.window.set_default_size(1280, 780)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        for side in ("start", "end", "top", "bottom"):
            getattr(bar, f"set_margin_{side}")(6)
        self.status = Gtk.Label(label=WATCH_ONLY if handoff is None else OBSERVING, xalign=0)
        self.status.set_hexpand(True)
        self.status.set_wrap(True)
        bar.append(self.status)
        self.button = Gtk.Button(label="Take over")
        self.button.connect("clicked", self._on_button)
        self.button.set_visible(handoff is not None)
        bar.append(self.button)
        box.append(bar)

        self.picture = Gtk.Picture()
        self.picture.set_content_fit(Gtk.ContentFit.CONTAIN)
        self.picture.set_can_shrink(True)
        self.picture.set_vexpand(True)
        self.picture.set_hexpand(True)
        self.picture.set_focusable(True)
        self.picture.set_alternative_text("Observed screen")
        box.append(self.picture)
        self.window.set_child(box)

        click = Gtk.GestureClick(button=0)
        click.connect("pressed", self._on_click)
        self.picture.add_controller(click)
        motion = Gtk.EventControllerMotion()
        motion.connect("motion", self._on_motion)
        self.picture.add_controller(motion)
        scroll = Gtk.EventControllerScroll(flags=Gtk.EventControllerScrollFlags.BOTH_AXES)
        scroll.connect("scroll", self._on_scroll)
        self.picture.add_controller(scroll)
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_key)
        self.window.add_controller(keys)
        self.window.connect("close-request", self._on_close)

        GLib.timeout_add(self.interval_ms, self._tick)
        if handoff is not None:
            GLib.timeout_add_seconds(max(int(handoff.lease_s / 3), 3), self._beat)
        self._tick()
        self.window.present()

    # -- screen ------------------------------------------------------------
    def _tick(self) -> bool:
        if not self._capturing:
            self._capturing = True
            self.worker.submit(self.worker.capture, self._captured)
        return True

    def _captured(self, frame: Frame | None, error: BaseException | None) -> None:
        texture = None
        if frame is not None:
            try:
                texture = Gdk.Texture.new_from_filename(frame.path)
            except GLib.Error as exc:
                error = exc
            finally:
                # Frames are this observer's own artifacts; keep none on disk.
                with contextlib.suppress(OSError):
                    os.unlink(frame.path)
        GLib.idle_add(self._show, frame, texture, error)

    def _show(self, frame: Frame | None, texture: Any, error: BaseException | None) -> bool:
        self._capturing = False
        if texture is not None:
            self.frame = frame
            self.picture.set_paintable(texture)
            if self.status.get_label().startswith("Not connected"):
                self._set_status()
        elif error is not None:
            self.status.set_label(f"Not connected: {error}. Retrying.")
        return False

    # -- handoff -----------------------------------------------------------
    def _set_status(self, extra: str = "") -> None:
        if self.handoff is None:
            text = WATCH_ONLY
        else:
            text = HOLDING if self.handoff.holding else OBSERVING
            self.button.set_label("Give back control" if self.handoff.holding else "Take over")
        self.status.set_label(f"{text} {extra}".strip())

    def _in_background(self, fn: Any, after: Any = None) -> None:
        """Run a blocking handoff request off the main loop, then update the status."""

        def work() -> None:
            note = ""
            try:
                fn()
            except ViewerError as exc:
                note = f"({exc})"
            GLib.idle_add(lambda: (self._set_status(note), after and after(), False)[-1])

        threading.Thread(target=work, daemon=True).start()

    def _on_button(self, _button: Gtk.Button) -> None:
        if self.handoff is None:
            return
        if self.handoff.holding:
            self._in_background(self.handoff.give_back)
        else:
            self._in_background(self.handoff.take, self.picture.grab_focus)

    def _beat(self) -> bool:
        if self.handoff is not None and self.handoff.holding:
            self._in_background(self.handoff.heartbeat)
        return True

    def _send(self, tool: str, args: dict[str, Any]) -> None:
        if self._inputs is not None:
            self._inputs.submit(tool, args)

    def _input_failed(self, note: str) -> None:
        GLib.idle_add(lambda: (self._set_status(f"({note})"), False)[-1])

    def _screen_point(self, wx: float, wy: float) -> tuple[int, int] | None:
        frame = self.frame
        if frame is None or frame.mapping is None:
            return None
        image = widget_to_image(
            wx, wy, self.picture.get_width(), self.picture.get_height(), frame.width, frame.height
        )
        if image is None:
            return None
        return image_to_screen(image[0], image[1], frame.mapping)

    def _on_click(self, gesture: Gtk.GestureClick, _n_press: int, x: float, y: float) -> None:
        self.picture.grab_focus()
        # Every press is one click, n_press included: a double click is two presses.
        point = self._screen_point(x, y)
        if point is None:
            return
        self._send(*click_to_input(point, gesture.get_current_button()))

    def _on_motion(self, _ctl: Gtk.EventControllerMotion, x: float, y: float) -> None:
        self._pointer = (x, y)

    def _on_scroll(self, _ctl: Gtk.EventControllerScroll, dx: float, dy: float) -> bool:
        if self.handoff is None or not self.handoff.holding:
            return False
        if self._pointer is None:
            return False
        point = self._screen_point(*self._pointer)
        if point is None:
            return False
        axis, delta = ("vertical", dy) if abs(dy) >= abs(dx) else ("horizontal", dx)
        steps = round(delta) or (1 if delta > 0 else -1)
        self._send(
            "pointer_scroll",
            {
                "target": {"x": point[0], "y": point[1]},
                "delta": steps,
                "axis": axis,
                "unit": "detent",
            },
        )
        return True

    def _on_key(self, _ctl: Gtk.EventControllerKey, keyval: int, _code: int, state: Any) -> bool:
        if self.handoff is None or not self.handoff.holding:
            return False
        mods = Gdk.ModifierType
        cp = Gdk.keyval_to_unicode(keyval)
        call = key_to_input(
            Gdk.keyval_name(keyval),
            chr(cp) if cp else "",
            ctrl=bool(state & mods.CONTROL_MASK),
            alt=bool(state & mods.ALT_MASK),
            super_=bool(state & mods.SUPER_MASK),
            shift=bool(state & mods.SHIFT_MASK),
        )
        if call is None:
            return False
        self._send(*call)
        return True

    def _on_close(self, _window: Gtk.Window) -> bool:
        # Never block the main loop on the owner: hand control back and stop the input
        # thread from a non-daemon thread, which the interpreter waits for before exiting.
        handoff, inputs = self.handoff, self._inputs

        def finish() -> None:
            if handoff is not None:
                with contextlib.suppress(ViewerError):
                    handoff.abandon()
            if inputs is not None:
                inputs.close()

        threading.Thread(target=finish, name="kwin-mcp-viewer-close").start()
        return False


def run(worker: ObserverWorker, handoff: Handoff | None, interval: float) -> int:
    """Run the GTK application until the window closes."""
    app = Gtk.Application(application_id=APP_ID, flags=Gio.ApplicationFlags.NON_UNIQUE)
    holder: dict[str, ViewerWindow] = {}

    def activate(a: Gtk.Application) -> None:
        holder["window"] = ViewerWindow(a, worker, handoff, interval)

    app.connect("activate", activate)
    return app.run([])
