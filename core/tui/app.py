"""The terminal application: state, input routing, frame composition.

TuiApp owns the screen. The agent session never writes to the terminal —
it reports through the layout bridge (the same duck-typed interface the
session has always used), and the presenter loop composes frames from
state. Blocking interactive prompts (menus, key entry, approvals) run on
the turn worker thread and are answered through queues, so the UI stays
live the whole time.
"""

from __future__ import annotations

import os
import queue
import threading
import time
from typing import Any

from core import theme
from core.term import visible_len
from core.term import ansi_strip as strip_ansi

from core.tui.backend import Backend, Key, Mouse, Paste, Resize
from core.tui.buffer import Buffer, Renderer
from core.tui.clipboard import copy_text
from core.tui.composer import Composer
from core.tui.loop import Presenter
from core.tui.overlays import (
    LinePrompt,
    MenuOverlay,
    QuestionCard,
    render_completion,
)
from core.tui.selection import Selection, col_to_offset
from core.tui.transcript import Transcript

SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
ANIMATION_INTERVAL = 0.08
DRAW_INTERVAL = 0.033
RESIZE_DEBOUNCE = 0.016

_WHEEL_UP = 64
_WHEEL_DOWN = 65

STYLE_HAIR = ("38;5;238",)
STYLE_ACCENT = (theme.BLOOD,)
STYLE_SELECT = ("7",)
STYLE_WARN = (theme.WARN,)


class LayoutBridge:
    """The duck-typed layout the session talks to.

    Every method translates a legacy session call into application state
    and marks the frame dirty. It never draws.
    """

    def __init__(self, app: "TuiApp") -> None:
        self.app = app
        self.prompt_sync = None

    @property
    def active(self) -> bool:
        return self.app.running

    @property
    def app_scrolls(self) -> bool:
        return True

    @property
    def following(self) -> bool:
        """True while the viewport is at the tail (not scrolled up)."""
        with self.app.lock:
            return self.app.transcript.follow

    @property
    def lines(self) -> list[str]:
        with self.app.lock:
            return list(self.app.transcript.display)

    @property
    def raw(self) -> list[str]:
        with self.app.lock:
            return list(self.app.transcript.raw)

    @property
    def _cols(self) -> int:
        return self.app.cols

    @property
    def prompt_row(self) -> int:
        return self.app.rows

    @property
    def content_top(self) -> int:
        return 3

    @property
    def content_bottom(self) -> int:
        return max(3, self.app.rows - 3)

    @property
    def _splash_visible(self) -> bool:
        return False

    def enter(self) -> None:
        pass

    def setup(self, splash_rows: int, session: Any = None, style: Any = None) -> None:
        pass

    def cleanup(self) -> None:
        pass

    def write(self, text: str) -> None:
        self.app.feed_output(text)

    def flush(self) -> None:
        self.app.transcript.flush_partial()
        self.app.mark_dirty()

    def clear_content(self) -> None:
        with self.app.lock:
            self.app.transcript.clear()
        self.app.mark_dirty()

    def draw_chrome(self) -> None:
        self.app.mark_dirty()

    def draw_border_status(self, text: str = "") -> None:
        self.app.set_status(text)

    def draw_prompt(self, body: str = "") -> None:
        # The composer owns the prompt row; only the live token counter
        # riding in the body carries information the border can show.
        plain = strip_ansi(body)
        if "tok" in plain:
            # The body is "│ <label> <counter>" - keep only the counter
            # part so the prompt label never leaks into the border chips.
            if ">" in plain:
                plain = plain.split(">", 1)[1].strip()
            else:
                plain = plain.strip()
            self.app.set_counter(plain)

    def prompt_text(self, body: str = "") -> str:
        return ""

    def check_resize(self) -> bool:
        return False

    def scroll_up(self, amount: int = 3) -> None:
        with self.app.lock:
            self.app.transcript.scroll_up(amount)
        self.app.mark_dirty()

    def scroll_down(self, amount: int = 3) -> None:
        with self.app.lock:
            self.app.transcript.scroll_down(amount)
        self.app.mark_dirty()

    def scroll_to_bottom(self) -> None:
        with self.app.lock:
            self.app.transcript.jump_bottom()
        self.app.mark_dirty()

    def show_splash(self) -> int:
        return 0

    def hide_splash(self) -> None:
        pass

    def render_content(self) -> None:
        self.app.mark_dirty()

    def redraw_content_and_chrome(self) -> None:
        self.app.mark_dirty()

    def restore_popup_rows(self, count: int) -> None:
        self.app.mark_dirty()

    def move_to_content(self) -> None:
        pass

    def move_to_prompt(self) -> None:
        pass

    def get_line_at_row(self, screen_row: int) -> str | None:
        row = self.app.row_at(screen_row)
        return row[1] if row else None

    def wall_glyph(self) -> str:
        return "│"

    def box_edge_row(self, text: str = "") -> str:
        return "╭─ " + text

    def start_prompt_pulse(self) -> None:
        pass

    def stop_prompt_pulse(self) -> None:
        pass


class TuiApp:
    def __init__(self, session, backend: Backend | None = None) -> None:
        self.session = session
        session.ui = self
        session.layout = LayoutBridge(self)
        try:
            session.approvals._ask = self.ask_approval
        except Exception:
            pass

        self.backend = backend if backend is not None else Backend()
        self.renderer: Renderer | None = None
        self.transcript = Transcript()
        self.composer = Composer()
        self.selection = Selection()
        self.lock = threading.RLock()

        self.cols = 0
        self.rows = 0
        self.running = True
        self._stopped = False
        self.dirty = True
        self._history: list[str] = []      # submitted prompts, newest last
        self._history_idx = 0              # len() = the fresh (empty) slot

        self.busy = False
        self.busy_label = "Channeling"
        self._spinner_i = 0
        self.status_text = ""
        self.counter_text = ""
        self.toast = ""
        self._toast_until = 0.0

        self._pending_resize: tuple[int, int, float] | None = None
        self._content_top = 3
        self._content_height = 10
        self._visible_rows: list[tuple[int, str]] = []
        self._welcome_card = False
        self._welcome_lines: list[str] = []
        self._popup_rect: tuple[int, int, int, int] | None = None
        self._popup_hits: dict[int, int] = {}
        self._popup_more_row: int | None = None
        self._popup_less_row: int | None = None

        self.overlay: Any = None          # menu / question / line prompt
        self._overlay_reply: queue.Queue | None = None
        self.queued = ""                  # prompt submitted mid-turn
        self._last_ctrl_c = 0.0
        self._drag_autoscroll = 0
        self._dirty = True
        self._turn_started = time.monotonic()

    # ── lifecycle ─────────────────────────────────────────────

    def start(self) -> None:
        import signal

        self._stopped = False
        self.backend.start()
        self._init_surface()
        self._show_welcome()
        # Deliver Ctrl+C through the event queue like any other key, so
        # its meaning (abort a turn; press twice to quit) stays in one
        # place instead of crashing the loop with a signal exception.
        def _on_sigint(signum, frame):
            self.backend.events.put(Key("ctrl+c"))

        previous = None
        try:
            previous = signal.getsignal(signal.SIGINT)
            signal.signal(signal.SIGINT, _on_sigint)
        except (OSError, ValueError):
            previous = None
        try:
            Presenter(self).run()
        except KeyboardInterrupt:
            self.running = False
        finally:
            # Whatever happens inside the loop, the terminal must be
            # restored to the shell's screen and modes.
            self.stop()
            if previous is not None:
                try:
                    signal.signal(signal.SIGINT, previous)
                except (OSError, ValueError):
                    pass

    def _init_surface(self) -> None:
        cols, rows = self.backend.size
        self.cols, self.rows = cols, rows
        self.renderer = Renderer(self.backend, cols, rows)
        self.transcript.set_width(cols)

    def stop(self) -> None:
        # A dedicated flag, not `running`: the KeyboardInterrupt handlers
        # clear `running` before `stop()` runs, so gating on it would skip
        # backend.stop() and leave the terminal in raw mode forever.
        if self._stopped:
            return
        self._stopped = True
        self.running = False
        # A worker blocked in ask_line/ask_approval/choose/confirm must not
        # hang forever once the app stops: wake it with a None sentinel
        # (the helpers already treat None as cancelled/empty).
        reply = self._overlay_reply
        if reply is not None:
            reply.put(None)
            self._overlay_reply = None
        time.sleep(0.05)
        self.backend.stop()

    # ── wiring for the session ────────────────────────────────

    def feed_output(self, text: str) -> None:
        with self.lock:
            self.transcript.append_partial(text)
        # First real content replaces the centered welcome card.
        self._welcome_card = False
        self.mark_dirty()

    def set_status(self, text: str) -> None:
        self.status_text = text
        self.mark_dirty()

    def set_counter(self, text: str) -> None:
        self.counter_text = text
        self.mark_dirty()

    def set_busy(self, on: bool, label: str = "Channeling") -> None:
        self.busy = on
        self.busy_label = label
        if not on:
            self.status_text = ""
            self.counter_text = ""  # the live counter chip is stale once the turn ends
        self.mark_dirty()

    def toast_message(self, text: str, seconds: float = 1.6) -> None:
        self.toast = text
        self._toast_until = time.monotonic() + seconds
        self.mark_dirty()

    def run_detached(self, fn) -> None:
        threading.Thread(target=self._guard, args=(fn,), daemon=True).start()

    def _guard(self, fn) -> None:
        try:
            fn()
        except SystemExit:
            self.stop()
        except Exception as exc:  # surface it like a turn error
            self.feed_output(f"\n\033[38;5;167m!! {exc}\033[0m\n")

    def submit(self, text: str) -> None:
        text = text.strip()
        if not text:
            return
        if not self._history or self._history[-1] != text:
            self._history.append(text)
        self._history_idx = len(self._history)
        if self.busy:
            self.queued = text
            self.toast_message("queued — runs when the current turn ends")
            self.mark_dirty()
            return
        stamp = time.strftime("%H:%M")
        self.feed_output(f"\033[2m{stamp}\033[0m  {text}\n")
        self.transcript.flush_partial()
        self.busy = True
        self.busy_label = "Chanting"
        self._turn_started = time.monotonic()
        self._spinner_i = 0
        self.mark_dirty()
        self.run_detached(lambda: self._run_turn(text))

    def _run_turn(self, text: str) -> None:
        from core.agent.exceptions import AbortError, HarnessError

        try:
            was_command = dispatch_session(self.session, text)
            if not was_command:
                self.session.handle(text)
        except SystemExit:
            self.stop()
            return
        except AbortError:
            pass
        except HarnessError as exc:
            self.feed_output(f"\033[38;5;167m  !! {exc}\033[0m\n")
        finally:
            self.busy = False
            self.status_text = ""
            self.mark_dirty()
        queued = self.queued
        self.queued = ""
        if queued and self.running:
            # Let the turn's own tail lines settle before the next one.
            time.sleep(0.05)
            self.submit(queued)

    # ── blocking interactive helpers (worker-thread side) ─────

    def ask_line(self, label: str, secret: bool = False, default: str = "") -> str:
        reply: queue.Queue = queue.Queue()
        self.overlay = LinePrompt(label, secret=secret, default=default)
        self._overlay_reply = reply
        self.mark_dirty()
        answer = reply.get()
        return answer if answer is not None else ""

    def ask_approval(self, prompt: str) -> str:
        reply: queue.Queue = queue.Queue()
        self.overlay = QuestionCard("allow?", prompt, choices="yna")
        self._overlay_reply = reply
        self.mark_dirty()
        answer = reply.get()
        return answer or "n"

    def choose(
        self,
        title: str,
        options: list,
        allow_filter: bool = True,
        allow_delete: bool = False,
        on_delete: Any = None,
    ) -> str | None:
        reply: queue.Queue = queue.Queue()
        self.overlay = MenuOverlay(
            title,
            options,
            allow_filter=allow_filter,
            allow_delete=allow_delete,
            on_delete=on_delete,
        )
        self._overlay_reply = reply
        self.mark_dirty()
        result = reply.get()
        return result

    def confirm(self, title: str, body: str) -> bool:
        reply: queue.Queue = queue.Queue()
        self.overlay = QuestionCard(title, body, choices="yn")
        self._overlay_reply = reply
        self.mark_dirty()
        return (reply.get() or "n") == "y"

    def _finish_overlay(self, value: Any) -> None:
        self.overlay = None
        reply = self._overlay_reply
        self._overlay_reply = None
        if reply is not None:
            reply.put(value)
        self.mark_dirty()

    # ── presenter interface ───────────────────────────────────

    @property
    def dirty(self) -> bool:
        return self._dirty

    @dirty.setter
    def dirty(self, value: bool) -> None:
        self._dirty = value

    def mark_dirty(self) -> None:
        self._dirty = True

    def next_event(self, timeout: float | None):
        return self.backend.events.get(timeout=timeout) if timeout is not None else self.backend.events.get_nowait()

    def needs_animation(self) -> bool:
        if self.busy:
            return True
        if self.selection.active and self._drag_autoscroll:
            return True
        if self.toast and time.monotonic() < self._toast_until:
            return True
        return False

    def animation_interval(self) -> float:
        if self.selection.active and self._drag_autoscroll:
            return 0.016
        return ANIMATION_INTERVAL

    def tick_animation(self) -> None:
        if self.busy:
            self._spinner_i = (self._spinner_i + 1) % len(SPINNER_FRAMES)
        if self.toast and time.monotonic() >= self._toast_until:
            self.toast = ""
        if self.selection.active and self._drag_autoscroll:
            with self.lock:
                if self._drag_autoscroll < 0:
                    self.transcript.scroll_up(2)
                else:
                    self.transcript.scroll_down(2)

    def apply_pending_resize(self) -> bool:
        pending = self._pending_resize
        if pending is None:
            return False
        cols, rows, at = pending
        if time.monotonic() - at < RESIZE_DEBOUNCE:
            return False
        self._pending_resize = None
        if (cols, rows) == (self.cols, self.rows):
            self.renderer.force_full_repaint()
            self.mark_dirty()
            return True
        self.cols, self.rows = cols, rows
        self.renderer.resize(cols, rows)
        with self.lock:
            self.transcript.set_width(cols)
        self.mark_dirty()
        return True

    def force_full_repaint(self) -> None:
        if self.renderer is not None:
            self.renderer.force_full_repaint()
        self.mark_dirty()

    # ── input routing ─────────────────────────────────────────

    def handle_event(self, event) -> None:
        if isinstance(event, Resize):
            self._pending_resize = (event.cols, event.rows, time.monotonic())
            return
        if isinstance(event, Paste):
            if isinstance(self.overlay, LinePrompt):
                self.overlay.insert(event.text)
                self.mark_dirty()
                return
            self.composer.consume_paste(event.text)
            self._history_idx = len(self._history)  # pasting edits, not recall
            self.mark_dirty()
            return
        if isinstance(event, Mouse):
            self._handle_mouse(event)
            return
        if isinstance(event, Key):
            self._handle_key(event)
            return

    def _handle_key(self, ev: Key) -> None:
        key, mods = ev.key, ev.mods
        if isinstance(self.overlay, MenuOverlay):
            self.overlay.consume_key(key, mods)
            if self.overlay.finished:
                self._finish_overlay(None if self.overlay.cancelled else self.overlay.result)
            self.mark_dirty()
            return
        if isinstance(self.overlay, (QuestionCard, LinePrompt)):
            self.overlay.consume_key(key, mods)
            if isinstance(self.overlay, QuestionCard) and self.overlay.finished:
                self._finish_overlay(self.overlay.answer)
            elif isinstance(self.overlay, LinePrompt) and self.overlay.finished:
                self._finish_overlay(None if self.overlay.cancelled else self.overlay.result)
            elif isinstance(self.overlay, LinePrompt):
                self.mark_dirty()
            return

        if key == "ctrl+q":
            self.stop()
            return
        if key == "ctrl+l":
            self.force_full_repaint()
            return
        if key == "ctrl+o":
            self.session.toggle_tool_output()
            return
        if key == "ctrl+c":
            now = time.monotonic()
            if self.busy:
                self._abort_turn()
                self._last_ctrl_c = 0.0
                return
            if now - self._last_ctrl_c < 2.0:
                self.stop()
                return
            self._last_ctrl_c = now
            self.toast_message("press ctrl+c again to quit")
            return
        if key == "esc":
            if self.busy:
                self._abort_turn()
                return
            if self.selection.active:
                self.selection.clear()
                self.mark_dirty()
                return
            return
        if key in ("pageup", "pagedown"):
            with self.lock:
                if key == "pageup":
                    self.transcript.scroll_up(max(3, self._content_height - 2))
                else:
                    self.transcript.scroll_down(max(3, self._content_height - 2))
            self.mark_dirty()
            return
        if key in ("up", "down") and "ctrl" in mods:
            # Transcript scroll on ctrl+up/down (plain arrows recall the
            # prompt history; wheel and PageUp also scroll).
            with self.lock:
                if key == "up":
                    self.transcript.scroll_up(1)
                else:
                    self.transcript.scroll_down(1)
            self.mark_dirty()
            return
        if key in ("up", "down") and not self.composer.popup_open and (
            not self.composer.buffer or self._history_idx != len(self._history)
        ):
            # Empty composer (or mid-recall): up/down cycle the session's
            # prompt history like a shell. Typing exits the recall mode.
            if key == "up":
                if self._history_idx > 0:
                    self._history_idx -= 1
                    self.composer.set_text(self._history[self._history_idx])
                    self.mark_dirty()
                    return
            else:
                if self._history_idx < len(self._history):
                    self._history_idx += 1
                    if self._history_idx < len(self._history):
                        self.composer.set_text(self._history[self._history_idx])
                    else:
                        self.composer.clear()
                    self.mark_dirty()
                    return
        if key == "home" and not self.composer.buffer and not self.composer.popup_open:
            # Symmetry with "end" below: home jumps the transcript to
            # the top when the composer has no text to move within.
            with self.lock:
                self.transcript.scroll_up(10**6)
            self.mark_dirty()
            return
        if key == "ctrl+home" or (key == "home" and "ctrl" in mods):
            with self.lock:
                # scroll_up clamps to the real top; assigning a huge raw
                # offset used to blank the viewport (end goes negative).
                self.transcript.scroll_up(10**6)
                self.transcript.follow = False
            self.mark_dirty()
            return
        if key == "ctrl+end":
            with self.lock:
                self.transcript.jump_bottom()
            self.mark_dirty()
            return
        if key == "end" and not self.composer.buffer:
            # No text to move within: end jumps the transcript to the tail.
            with self.lock:
                self.transcript.jump_bottom()
            self.mark_dirty()
            return
        if key == "enter" and not self.composer.buffer and not self.composer.popup_open:
            # Empty enter pages through capped tool output, as before.
            if self.session._pending_pages:
                self.session.page_next()
                self.mark_dirty()
            return
        if key == "ctrl+y":
            text = self._selection_text()
            if text:
                copy_text(text)
                self.toast_message("copied")
            self.mark_dirty()
            return
        before = self.composer.buffer
        self.composer.consume_key(key, mods)
        if self.composer.buffer != before:
            # The operator edited the recalled text (or typed fresh):
            # leave history recall mode.
            self._history_idx = len(self._history)
        if self.composer.submitted is not None:
            text = self.composer.submitted
            self.composer.submitted = None
            self.submit(text)
        self.mark_dirty()

    def _abort_turn(self) -> None:
        if not self.session._abort.is_set():
            self.session._abort.set()
            self.feed_output("\033[2m  (stopping after this step - ctrl+c again to quit)\033[0m\n")

    def _handle_mouse(self, ev: Mouse) -> None:
        if ev.kind == "wheel":
            if (
                self._popup_rect
                and self._popup_rect[0] <= ev.x < self._popup_rect[0] + self._popup_rect[2]
                and self._popup_rect[1] <= ev.y <= self._popup_rect[1] + self._popup_rect[3]
            ):
                comp = self.composer
                if comp.popup_open:
                    step = -1 if ev.button == _WHEEL_UP else 1
                    comp.selected = max(0, min(len(comp.completion.items) - 1, comp.selected + step))
                    self.mark_dirty()
                return
            with self.lock:
                if ev.button == _WHEEL_UP:
                    self.transcript.scroll_up(3)
                else:
                    self.transcript.scroll_down(3)
            self.mark_dirty()
            return
        if ev.kind == "press" and ev.button == 0:
            in_popup = (
                self._popup_rect is not None
                and self._popup_rect[0] <= ev.x < self._popup_rect[0] + self._popup_rect[2]
            )
            if in_popup and ev.y in self._popup_hits:
                comp = self.composer
                comp.selected = self._popup_hits[ev.y]
                comp._accept_popup()
                comp._dismissed = True
                comp.completion = None
                self._popup_rect = None
                self.mark_dirty()
                return
            if in_popup and self._popup_more_row is not None and ev.y == self._popup_more_row:
                comp = self.composer
                comp._popup_off += comp.max_popup
                self.mark_dirty()
                return
            if in_popup and self._popup_less_row is not None and ev.y == self._popup_less_row:
                comp = self.composer
                comp._popup_off = max(0, comp._popup_off - comp.max_popup)
                self.mark_dirty()
                return
            if self._content_top <= ev.y < self._content_top + self._content_height:
                self.selection.begin_press(ev.x, ev.y)
            return
        if ev.kind == "drag" and ev.button == 0:
            if self.selection._press_cell is not None:
                self.selection.begin_drag(ev.x, ev.y, self.row_at)
                self._drag_autoscroll = 0
                if self.selection.active:
                    if ev.y <= self._content_top:
                        self._drag_autoscroll = -1
                    elif ev.y >= self._content_top + self._content_height - 1:
                        self._drag_autoscroll = 1
                self.mark_dirty()
            return
        if ev.kind == "release":
            self._drag_autoscroll = 0
            if ev.button == 0 and self.selection._press_cell is not None:
                result = self.selection.end_press(
                    ev.x, ev.y, self.row_at, self._text_at
                )
                if result and result[0]:
                    copy_text(result[0])
                    n = len(result[0].splitlines())
                    self.toast_message(f"copied {n} line{'s' if n != 1 else ''}")
                self.mark_dirty()

    # ── geometry helpers ──────────────────────────────────────

    def row_at(self, screen_row: int) -> tuple[int, str] | None:
        if self._content_top <= screen_row < self._content_top + len(self._visible_rows):
            return self._visible_rows[screen_row - self._content_top]
        return None

    def _text_at(self, display_index: int) -> str:
        with self.lock:
            return self.transcript.row_text(display_index)

    def _selection_text(self) -> str:
        first_display = self._visible_rows[0][0] if self._visible_rows else 0
        spans = self.selection.overlay_rows(first_display, len(self._visible_rows))
        if not spans:
            return ""
        lines = []
        for offset, start, end in spans:
            row = self.row_at(self._content_top + offset)
            if not row:
                continue
            # The spans carry cell columns; wide (CJK) chars occupy two
            # cells, so the slice is computed in character offsets.
            text = strip_ansi(row[1])
            s = col_to_offset(row[1], start)
            e = col_to_offset(row[1], end)
            lines.append(text[s:e])
        return "\n".join(lines)

    # ── rendering ─────────────────────────────────────────────

    def render_frame(self) -> None:
        renderer = self.renderer
        if renderer is None:
            return
        buf = renderer.buffer
        cols, rows = buf.cols, buf.rows
        buf.reset()
        styles = renderer.styles
        hair_style = styles.id_for(STYLE_HAIR)
        accent = styles.id_for(STYLE_ACCENT)
        select_style = styles.id_for(STYLE_SELECT)
        warn_style = styles.id_for(STYLE_WARN)

        composer_height = self._composer_height(rows)
        content_top = 2
        content_bottom = rows - 2 - composer_height  # inclusive
        height = content_bottom - content_top + 1

        # Top info bar (coloured labels/values) + hairline.
        info = self._info_text()
        buf.set_styled_line(0, 0, info, styles, cols)
        buf.set_str(0, 1, "─" * cols, hair_style)

        # Transcript window.
        with self.lock:
            self.transcript.viewport_height = height
            visible = self.transcript.row_source(height)
        self._visible_rows = visible
        self._content_top = content_top
        self._content_height = height
        first_display = visible[0][0] if visible else 0
        for i, (_idx, text) in enumerate(visible):
            buf.set_styled_line(0, content_top + i, text, styles, cols)
        # Selection highlight.
        for offset, start, end in self.selection.overlay_rows(first_display, height):
            buf.set_style(start, content_top + offset, min(end, cols) - start, select_style)

        # Centered welcome card: shown while the transcript is still
        # empty, re-centered every frame so it follows resized windows.
        if self._welcome_card and not self.transcript.raw:
            card = self._welcome_lines
            card_w = max((visible_len(ln) for ln in card), default=0)
            x = max(0, (cols - card_w) // 2)
            y = content_top + max(0, (height - len(card)) // 2)
            for i, ln in enumerate(card):
                buf.set_styled_line(x, y + i, ln, styles, cols)

        # Bottom border row with status. The chip starts at column 2
        # (right after the corner+dash), aligned with the MANTRA prompt
        # label one row below.
        border_row = rows - 1 - composer_height
        status = self._border_text()
        marker = f" ^{self.transcript.scrolled}" if self.transcript.scrolled else ""
        if self.transcript.scrolled:
            status = (status + marker) if status else marker.lstrip()
        line = "╭─" + status + " "
        line += "─" * max(0, cols - visible_len(line) - 1) + "╮"
        if not status:
            line = "╭" + "─" * max(0, cols - 2) + "╮"
        buf.set_styled_line(0, border_row, line, styles, cols)

        # Composer box: the content rows above a solid bottom edge, so
        # the whole prompt reads as one closed rectangle with the status
        # row as its top edge.
        caret = self.composer.render(buf, rows - composer_height, composer_height - 1, cols, wall="│")
        buf.set_str(0, rows - 1, "╰" + "─" * max(0, cols - 2) + "╯", hair_style)
        renderer.set_cursor(caret, rows - 2)

        # Completion popup above the border row.
        comp = self.composer
        if comp.popup_open:
            max_rows = max(3, min(comp.max_popup, border_row - 2))
            comp._popup_off = max(0, min(comp._popup_off, max(0, len(comp.completion.items) - max_rows)))
            if comp.selected < comp._popup_off:
                comp._popup_off = comp.selected
            elif comp.selected >= comp._popup_off + max_rows:
                comp._popup_off = comp.selected - max_rows + 1
            x, y, w, h = render_completion(
                buf,
                comp.completion.items,
                comp.completion.labels,
                comp.selected,
                comp._popup_off,
                max_rows,
                border_row,
                cols,
            )
            self._popup_rect = (x, y, w, h) if h else None
            self._popup_hits = {}
            self._popup_more_row = None
            self._popup_less_row = None
            if h:
                items_y = y + 1
                shown = min(max_rows, len(comp.completion.items) - comp._popup_off)
                for i in range(shown):
                    self._popup_hits[items_y + i] = comp._popup_off + i
                if comp._popup_off > 0:
                    self._popup_less_row = items_y - 1
                if comp._popup_off + shown < len(comp.completion.items):
                    self._popup_more_row = items_y + shown
        else:
            self._popup_rect = None
            self._popup_hits = {}
            self._popup_more_row = None
            self._popup_less_row = None

        # Card overlays last (they take priority visually).
        if self.overlay is not None:
            self.overlay.render(buf, cols, rows)

        renderer.flush()

    def _composer_height(self, rows: int) -> int:
        # One extra row for the box's bottom edge: the prompt is a
        # closed rectangle, not an open U.
        if not self.composer.is_multiline:
            return 2
        lines = self.composer.buffer.count("\n") + 1
        return max(3, min(lines + 2, max(3, rows - 8)))

    def _info_text(self) -> str:
        s = self.session
        llm = getattr(s, "config", {}).get("llm", {}) if hasattr(s, "config") else {}
        model = llm.get("model", "?")
        reasoning = llm.get("reasoning_effort") or "off"
        ws = getattr(getattr(s, "sandbox", None), "root", "") or ""
        ws_short = ws.replace("\\", "/").rstrip("/").split("/")[-1] if ws else ""
        approval = getattr(getattr(s, "approvals", None), "mode", "default")
        totals = getattr(s, "totals", {}) or {}
        tokens_in = totals.get("tokens_in", 0)
        cache = totals.get("cache_hit", 0)
        rate = f"{cache * 100 // tokens_in}%" if tokens_in else "0%"

        # Coloured fields, same theme as the transcript: faint labels,
        # semantic values (model in info blue, approval by mode, cache
        # in sage), hairline separators.
        st = self.session.style
        label = lambda t: st._wrap(theme.FAINT, t)
        appr_color = {
            "auto": theme.SAGE, "plan": theme.INFO,
            "yolo": theme.WARN, "default": theme.ASH,
        }.get(approval, theme.ASH)
        parts = [
            f"{label('WORKSPACE:')} {st._wrap(theme.ASH, ws_short or '~')}",
            f"{label('MODEL:')} {st._wrap(theme.INFO, f'{model} ({reasoning})')}",
            f"{label('APPROVAL:')} {st._wrap(appr_color, approval)}",
            f"{label('CACHE:')} {st._wrap(theme.SAGE, rate)}",
        ]
        return (" " + st._wrap(theme.HAIR, "·") + " ").join(parts)

    def _border_text(self) -> str:
        if self.busy:
            # Busy chip: spinner, label, elapsed time, and the live token
            # counter (count + stream rate) ride together while a turn
            # streams. Nothing else clutters the border mid-turn.
            frame = SPINNER_FRAMES[self._spinner_i % len(SPINNER_FRAMES)]
            elapsed = int(time.monotonic() - getattr(self, "_turn_started", time.monotonic()))
            if elapsed < 60:
                elapsed_str = f"{elapsed}s"
            else:
                elapsed_str = f"{elapsed // 60}m{elapsed % 60:02d}s"
            label = self.busy_label if elapsed >= 1 else "Channeling"
            parts = [f"{frame} {label} {elapsed_str}"]
            if self.counter_text:
                parts.append(self.counter_text)
            return " · ".join(parts)
        # Idle: the top bar already carries model/approval/workspace, so
        # the border stays clean; only a queued prompt notice appears.
        if self.queued:
            return "queued prompt"
        return ""

    def _show_welcome(self) -> None:
        from core import __version__

        # Drawn as a frame overlay, not fed into the transcript, so the
        # card sits centered in the content area and re-centers on every
        # frame (i.e. follows resizes) until real content arrives.
        self._welcome_lines = [
            f"\033[{theme.BLOOD_BOLD}mM A N T R A\033[0m",
            f"\033[{theme.FAINT}mSpells Matter\033[0m",
            f"\033[{theme.HAIR}m{__version__}\033[0m",
        ]
        self._welcome_card = True
        self.mark_dirty()


# The session's slash-command router is imported lazily so the session
# module never needs to know about this one.
def dispatch_session(session, line: str) -> bool:
    from core.console import dispatch

    return dispatch(session, line)
