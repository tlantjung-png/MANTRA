"""Presenter loop: the single place the screen is updated.

The loop drains input events fully each iteration, lets the application
mutate state, and — only when something changed and the throttle allows —
asks the application to compose a frame and flushes it. An animation tick
is armed only while the application reports animation (spinner, drag
auto-scroll), so an idle console costs no wakeups.
"""

from __future__ import annotations

import queue
import time


class Presenter:
    def __init__(self, app, min_draw_interval: float = 0.033) -> None:
        self.app = app
        self.min_draw_interval = min_draw_interval
        self._last_draw = 0.0

    def run(self) -> None:
        try:
            self._run()
        except KeyboardInterrupt:
            # Ctrl+C reaching the loop as a signal rather than a key:
            # quit with the terminal restored, never a traceback.
            self.app.running = False

    def _run(self) -> None:
        while self.app.running:
            # Block while idle so an idle console sleeps instead of
            # busy-spinning at 100% CPU; animation keeps its short tick.
            timeout = (
                self.app.animation_interval()
                if self.app.needs_animation()
                else 0.05
            )
            try:
                event = self.app.next_event(timeout)
            except queue.Empty:
                event = None
            # Drain the input queue fully: held keys and bursts are
            # applied before any frame is composed.
            while event is not None:
                self.app.handle_event(event)
                try:
                    event = self.app.next_event(0)
                except queue.Empty:
                    event = None
            if self.app.needs_animation():
                self.app.tick_animation()
                self.app.mark_dirty()
            if self.app.apply_pending_resize():
                self._last_draw = 0.0  # resize repaints immediately
            now = time.monotonic()
            if self.app.dirty and (now - self._last_draw) >= self.min_draw_interval:
                try:
                    self.app.render_frame()
                except Exception:
                    # Deliberately broad: a frame bug must not kill the
                    # loop; force a full repaint next time since the
                    # screen may be mid-frame.
                    self.app.force_full_repaint()
                self._last_draw = now
                self.app.dirty = False
