"""The terminal application: state, input routing, frame composition.

TuiApp owns the screen. The agent session never writes to the terminal —
it reports through the layout bridge (the same duck-typed interface the
session has always used), and the presenter loop composes frames from
state. Blocking interactive prompts (menus, key entry, approvals) run on
the turn worker thread and are answered through queues, so the UI stays
live the whole time.
"""

from __future__ import annotations

import queue
import re
import threading
import time
from typing import Any

from core import theme
from core.agent import sessions
from core.term import ansi_strip as strip_ansi, visible_len
from core.tui.backend import Backend, Key, Mouse, Paste, Resize
from core.tui.buffer import Renderer
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
from core.tui.suggest import suggestions_for, topic_of

from core.diffparse import parse_diff
from core.tui.review import ReviewState, render_review
from core.tui.sessionpanel import SessionEntry, SessionPanelState, render_session_panel

# style keys emitted by the review renderer -> theme SGR parameters
_REVIEW_STYLE_SGR = {
    "add": theme.SAGE,
    "del": theme.EMBER,
    "pair": None,
    "ctx": None,
    "dim": theme.FAINT,
    "head": theme.BONE_BOLD,
    "sel": theme.BONE_BOLD,
    "sep": theme.HAIR,
}


def _styled(text: str, sgr: str | None) -> str:
    return f"\x1b[{sgr}m{text}\x1b[0m" if sgr else text


_KEY_LOOK_RE = re.compile(r"^sk-[A-Za-z0-9_-]{10,}$|^gho_[A-Za-z0-9]{20,}$|^ghp_[A-Za-z0-9]{20,}$|^xox[abp]-[A-Za-z0-9-]{10,}$|^AIza[0-9A-Za-z_-]{30,}$|^[A-Za-z0-9]{32,}$")


def _redact_model_line_keys(text: str) -> str:
    """Mask key-shaped tokens before a /model line is echoed or stored."""
    parts = text.split()
    if len(parts) < 2 or parts[0] != "/model":
        return text
    # /model <saved-endpoint>            — single arg, no key to redact.
    if len(parts) == 2:
        return text
    if parts[1] == "key" and len(parts) >= 4:
        # /model key <name> <key>  — redact the value.
        return f"{parts[0]} {parts[1]} {parts[2]} {'*' * max(8, len(parts[3]))}"
    # /model <url> [key] [model]  — when the 3rd positional argument looks
    # like a key (a long opaque token), redact it; otherwise leave alone.
    if len(parts) >= 3 and _KEY_LOOK_RE.match(parts[2]) and not parts[2].startswith(("http://", "https://")):
        masked = "*" * max(8, len(parts[2]))
        tail = f" {parts[3]}" if len(parts) >= 4 else ""
        return f"{parts[0]} {parts[1]} {masked}{tail}"
    # Also handle the case where the URL is omitted and the second
    # argument is a saved endpoint: the key would be parts[3] in
    # /model <endpoint> <key> <model> form.
    if len(parts) >= 4 and not parts[1].startswith(("http://", "https://")) and not parts[1].startswith("key") and _KEY_LOOK_RE.match(parts[3]):
        masked = "*" * max(8, len(parts[3]))
        tail = f" {parts[4]}" if len(parts) >= 5 else ""
        return f"{parts[0]} {parts[1]} {parts[2]} {masked}{tail}"
    return text

SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
ANIMATION_INTERVAL = 0.08
DRAW_INTERVAL = 0.033
RESIZE_DEBOUNCE = 0.016

# "↓ bottom" chip timings: accent pulse on turn-end and on output
# arriving while detached; fade-out linger after returning to the tail.
CHIP_FLASH_SECONDS = 2.0
CHIP_FADE_SECONDS = 0.6
# Chip pulse throttle: streaming output re-arms the accent pulse at most
# once per this interval, so a fast token stream pulses instead of
# burning the accent solid-on for the whole turn.
CHIP_PULSE_THROTTLE = 1.0
# Auto-suggestions after a finished task: a dim "next" line of
# numbered steps at the tail of the conversation.
SUGGESTION_MAX = 3
SUGGESTION_LINGER = 45.0  # seconds the row stays before self-dismissing

_WHEEL_UP = 64
_WHEEL_DOWN = 65
# Ceiling for the completion dropdown's adaptive window: a dropdown is a
# shortcut, not a file browser, so "it fits" is not reason enough to show
# a hundred rows on a very tall terminal.
_POPUP_ROWS_CAP = 24
# Smallest frame the chrome can be laid out in: below this the info bar,
# the transcript window, the completion popup and the prompt box cannot all
# have a row of their own, so the frame degrades to a one-line notice.
MIN_COLS = 30
MIN_ROWS = 8

STYLE_HAIR = ("38;5;238",)
STYLE_ACCENT = (theme.BLOOD,)
STYLE_SELECT = ("7",)
STYLE_WARN = (theme.WARN,)
STYLE_BONE = (theme.BONE,)
STYLE_FAINT = ("2",)


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

    def open_review(self, diff_text: str) -> None:
        """Open the full-screen diff review for the given diff text."""
        files = parse_diff(diff_text)
        if files:
            self.app.review = ReviewState(files=files)
            self.app.mark_dirty()

    def open_sessions(self) -> None:
        """Open the session manager panel from the saved sessions."""
        entries = [
            SessionEntry(
                name=e.get("name") or "?",
                workspace=e.get("workspace") or "",
                model=e.get("model") or "",
                turns=int(e.get("turns", 0) or 0),
                saved_at=e.get("saved_at") or "",
                mtime=float(e.get("mtime") or 0),
                summary=e.get("summary") or "",
            )
            for e in sessions.list_sessions()
        ]
        self.app.session_panel = SessionPanelState(entries=entries)
        self.app.session_panel_on_enter = self.app.session.resume_session
        self.app.mark_dirty()

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
        except AttributeError:
            pass  # duck-typed session without an approvals object (tests)

        self.backend = backend if backend is not None else Backend()
        self.renderer: Renderer | None = None
        self.transcript = Transcript()
        self.composer = Composer()
        self.selection = Selection()
        self.lock = threading.RLock()
        # Chip flash/fade timers (transcript exists at this point).
        self._chip_flash_until = 0.0
        self._chip_fade_until = 0.0
        self._chip_fade_missed = 0  # missed count frozen for the fade echo
        self._chip_last_pulse = 0.0  # pulse throttle anchor
        # Turn-scoped context the suggestion engine reads at turn end.
        self._turn_user_prompt = ""
        self._turn_tool_text: list[str] = []
        # The suggestion row shows at every turn end: _turn_pending marks a
        # submitted turn not yet consumed, _turn_was_command suppresses it for
        # slash-command turns (their "reply" is chrome, not agent work).
        self._turn_pending = False
        self._turn_was_command = False
        # Subjects of recent turns, newest first: lets the row jump back to
        # an older thread and inherit a subject when a turn is content-free
        # ("thanks").
        self._recent_topics: list[str] = []
        self._turn_had_error = False
        # Auto-suggestions after a finished task: an ordered list of step
        # strings, the hit rects painted for them, and the selected index
        # while the row has keyboard focus. Gated by the persisted UI
        # preference (settings file, via session config override) so
        # /suggestions off survives a restart.
        self.suggestions_enabled = self._resolve_suggestions_enabled(session)
        self.suggestions: list[str] = []
        self._suggestion_commands: list[str] = []
        self._suggestion_rects: list[tuple[int, int, int]] = []
        self._suggestion_selected: int | None = None
        self._suggestions_until = 0.0

        self.cols = 0
        self.rows = 0
        self.running = True
        self._stopped = False
        self.dirty = True
        self._history: list[str] = []      # submitted prompts, newest last
        self._history_idx = 0              # len() = the fresh (empty) slot
        self.review: ReviewState | None = None  # full-screen diff review, if open
        self.session_panel: SessionPanelState | None = None  # session manager, if open
        self.session_panel_on_enter = None  # callable(name) -> resume a session

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
        # Transcript scrollbar: (track_x, top, height, thumb_top, thumb_span)
        # from the last painted frame, and whether a thumb drag is live.
        self._scrollbar: tuple[int, int, int, int, int] | None = None
        self._scrollbar_drag = False
        self._scrollbar_anchor = 0
        # "↓ bottom" chip rect (x, y, width) while detached from the tail.
        self._scroll_to_bottom: tuple[int, int, int] | None = None

        self.overlay: Any = None          # menu / question / line prompt
        self._overlay_reply: queue.Queue | None = None
        self._overlay_lock = threading.Lock()  # serialize concurrent prompts
        self._composer_press = False  # a press started inside the prompt box
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
        # Output arriving while scrolled away: pulse the chip so the
        # operator sees the transcript growing underneath them, not
        # just when the turn ends. Throttled: a fast token stream
        # re-arms at most once per CHIP_PULSE_THROTTLE seconds, so the
        # accent blinks rather than burning solid-on all turn.
        now = time.monotonic()
        if self.transcript.scrolled > 0 and now - self._chip_last_pulse >= CHIP_PULSE_THROTTLE:
            self._chip_last_pulse = now
            self._chip_flash_until = now + CHIP_FLASH_SECONDS
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
            # Finishing a turn while the operator is scrolled away is
            # worth announcing: the chip (already showing the missed
            # count) flashes in the accent colour for a moment.
            if self.transcript.scrolled > 0:
                self._chip_flash_until = time.monotonic() + CHIP_FLASH_SECONDS
            self._maybe_show_suggestions()
        self.mark_dirty()

    @staticmethod
    def _resolve_suggestions_enabled(session) -> bool:
        """Suggestion toggle resolution: session config beats settings file.

        The session config is authoritative within the process (the
        /suggestions command keeps it in step); the settings file's
        "ui.suggestions" is the default for a fresh start.
        """
        cfg = getattr(session, "config", None) or {}
        if "suggestions" in cfg:
            return bool(cfg["suggestions"])
        try:
            from core.agent.settings import ui_prefs

            return bool(ui_prefs().get("suggestions", True))
        except Exception:
            return True  # settings unreadable: default on, never crash startup

    def _maybe_show_suggestions(self) -> None:
        """After a finished turn, derive and show next-step rows.

        Runs at every agent-turn end: the engine always returns at least
        one row, falling back to conversation-derived ones. Consumed
        exactly once per turn (set_busy(False) can fire twice), and
        slash-command turns show nothing - their output is chrome, so
        follow-up rows there would answer a reply nobody read.
        """
        if not self._turn_pending:
            return  # already consumed, or a spurious set_busy(False)
        self._turn_pending = False
        was_command, self._turn_was_command = self._turn_was_command, False
        if not self.suggestions_enabled or was_command:
            self._turn_user_prompt = ""
            self._turn_tool_text = []
            return
        prompt = self._turn_user_prompt
        # The assistant's final reply is the richest signal: it states
        # what was done ("edited the files", "all tests pass") and what
        # it would do next. The session stashes it turn-scoped.
        reply = str(getattr(self.session, "last_reply", "") or "")
        # The turn's tool evidence: what the console actually observed,
        # collected turn-scoped by the session, plus the transcript tail
        # for a session that never wired the collector (tests, --once).
        tool_text = "\n".join(self._turn_tool_text)
        if not tool_text:
            with self.lock:
                recent = list(self.transcript.raw[-80:])
            tool_text = "\n".join(
                ln for ln in recent
                if "→" in ln or ln.lstrip().startswith(
                    ("run_command", "edit_file", "write_file", "read_file")
                )
            )
        # The files this turn actually changed - the strongest evidence a
        # suggestion can be grounded in. Turn-scoped snapshots while the
        # turn is still tearing down, the session's reported set after.
        changed = []
        for key in ("_edit_snapshots", "reported_changes"):
            source = getattr(self.session, key, None)
            if isinstance(source, dict):
                changed = [str(p) for p in source.keys()]
                break
            if isinstance(source, (set, list, tuple)):
                changed = [str(p) for p in source]
                break
        # The effective subject mirrors the engine's own choice (the
        # prompt's topic, or the inherited one when the prompt was
        # content-free); it is recorded so later turns can jump back to
        # this thread or inherit it in turn.
        effective_topic = topic_of(prompt) or (self._recent_topics[0] if self._recent_topics else "")
        suggestions = suggestions_for(
            prompt, reply, tool_text, self._turn_had_error, max_items=SUGGESTION_MAX,
            recent_topics=self._recent_topics, changed_files=changed,
        )
        # The engine guarantees at least one row for every agent turn,
        # so this is a shape guard, not a filter: an empty result must
        # never reach the paint path.
        self.suggestions = [s.label for s in suggestions][:SUGGESTION_MAX]
        self._suggestion_commands = [s.command for s in suggestions][:SUGGESTION_MAX]
        if self.suggestions:
            self._suggestion_selected = None
            self._suggestions_until = time.monotonic() + SUGGESTION_LINGER
        if effective_topic:
            # Newest first, deduped (re-raising a topic moves it to the
            # front), and capped - a bounded memory, not a transcript.
            self._recent_topics = [effective_topic] + [
                t for t in self._recent_topics if t != effective_topic
            ][:7]
        self._turn_user_prompt = ""  # consumed
        self._turn_tool_text = []

    def _dismiss_suggestions(self) -> None:
        """Clear the suggestion row (expiry, submit, or Esc)."""
        self.suggestions = []
        self._suggestion_commands = []
        self._suggestion_rects = []
        self._suggestion_selected = None
        self._suggestions_until = 0.0

    def _accept_suggestion(self, index: int) -> None:
        """Run a suggestion step as if the operator had typed it."""
        if 0 <= index < len(self._suggestion_commands):
            command = self._suggestion_commands[index]
            self._dismiss_suggestions()
            self.submit(command)

    def _arm_chip_fade(self, missed: int) -> None:
        """Linger the chip as a fading echo after returning to the tail."""
        self._chip_fade_missed = missed
        self._chip_fade_until = time.monotonic() + CHIP_FADE_SECONDS
        # The jump supersedes any pending flash: without this the chip
        # would blink out until the flash expired, then fade — instead
        # of handing straight over to the echo.
        self._chip_flash_until = 0.0

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
        except Exception as exc:
            # Deliberately broad: a detached worker must never die
            # silently, and any failure it hits is surfaced to the
            # operator exactly like a turn error.
            self.feed_output(f"\n\033[38;5;167m!! {exc}\033[0m\n")

    def submit(self, text: str) -> None:
        text = text.strip()
        if not text:
            return
        # /model lines can carry an API key as the 2nd or 3rd positional
        # argument; echo and history see the redacted form, but the
        # actual command dispatched to the harness must keep the real key
        # so the endpoint switch can store and use it.
        display_text = _redact_model_line_keys(text) if text.startswith("/model") else text
        if not self._history or self._history[-1] != display_text:
            self._history.append(display_text)
        self._history_idx = len(self._history)
        # The busy check and the queue write are one critical section, so a
        # prompt arriving while a turn's finally-block is ending is either
        # seen as busy (queued) or as idle (started) — never both.
        with self.lock:
            if self.busy:
                self.queued = text
                self.toast_message("queued — runs when the current turn ends")
                self.mark_dirty()
                return
            self.busy = True
            self.busy_label = "Chanting"
            self._turn_started = time.monotonic()
            self._spinner_i = 0
            # Turn-scoped context for the post-task suggestion engine.
            self._turn_user_prompt = text
            self._turn_tool_text = []
            self._turn_had_error = False
            self._turn_pending = True
            self._turn_was_command = False
        # A detached scroll intentionally survives a new submission
        # (pinned by test_turn_end_does_not_yank_a_detached_scroll), so
        # submit never jumps — and never fades the chip either.
        stamp = time.strftime("%H:%M")
        # The operator's own line must be scannable in a wall of tool
        # output: timestamp on a subtle dark chip, the text in the accent
        # colour (the same hue as the wordmark and spinner, so "what I
        # typed" reads as one visual family). No "you" label: the chip
        # already marks the line as the operator's, and the bare word
        # reads as chatter in a wall of tool output.
        self.feed_output(
            f"\033[2m\033[48;5;236m{stamp}\033[0m"
            f" \033[38;5;204m{display_text}\033[0m\n"
        )
        self.transcript.flush_partial()
        # Typing a prompt supersedes the suggestion row.
        self._dismiss_suggestions()
        self.mark_dirty()
        self.run_detached(lambda: self._run_turn(text))

    def _run_turn(self, text: str) -> None:
        from core.agent.exceptions import AbortError, HarnessError

        try:
            was_command = dispatch_session(self.session, text)
            self._turn_was_command = was_command
            if not was_command:
                self.session.handle(text)
        except SystemExit:
            self.stop()
            return
        except AbortError:
            pass
        except HarnessError as exc:
            self._turn_had_error = True
            self.feed_output(f"\033[38;5;167m  !! {exc}\033[0m\n")
        finally:
            # Clear busy and take the queued prompt inside the same lock
            # submit() checks, so the handoff between the two threads is
            # atomic: a prompt submitted mid-teardown is either seen as
            # busy (queued) or as idle (started a fresh turn) — never lost.
            with self.lock:
                self.busy = False
                queued = self.queued
                self.queued = ""
            self.status_text = ""
            self.mark_dirty()
        if queued and self.running:
            # Let the turn's own tail lines settle before the next one.
            time.sleep(0.05)
            self.submit(queued)

    # ── blocking interactive helpers (worker-thread side) ─────

    def _prompt_with_overlay(self, overlay: Any) -> Any:
        # Serialize prompts so concurrent callers cannot overwrite the
        # shared reply slot and orphan the first waiter.
        with self._overlay_lock:
            reply: queue.Queue = queue.Queue()
            self.overlay = overlay
            self._overlay_reply = reply
            self.mark_dirty()
            return reply.get()

    def ask_line(self, label: str, secret: bool = False, default: str = "") -> str:
        answer = self._prompt_with_overlay(LinePrompt(label, secret=secret, default=default))
        return answer if answer is not None else ""

    def ask_approval(self, prompt: str) -> str:
        answer = self._prompt_with_overlay(QuestionCard("allow?", prompt, choices="yna"))
        return answer or "n"

    def choose(
        self,
        title: str,
        options: list,
        allow_filter: bool = True,
        allow_delete: bool = False,
        on_delete: Any = None,
    ) -> str | None:
        return self._prompt_with_overlay(
            MenuOverlay(
                title,
                options,
                allow_filter=allow_filter,
                allow_delete=allow_delete,
                on_delete=on_delete,
            )
        )

    def confirm(self, title: str, body: str) -> bool:
        return (self._prompt_with_overlay(QuestionCard(title, body, choices="yn")) or "n") == "y"

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
        now = time.monotonic()
        # Chip flash and fade are timed states: the loop must keep
        # ticking (and repaint once at each boundary) while they run.
        if now < self._chip_flash_until or now < self._chip_fade_until:
            return True
        # The suggestion row self-dismisses after its linger window.
        if self.suggestions and now >= self._suggestions_until:
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
        # At flash expiry repaint once to drop the accent styling.
        if self._chip_flash_until and time.monotonic() >= self._chip_flash_until:
            self._chip_flash_until = 0.0
            self.mark_dirty()
        # At fade expiry repaint once to clear the lingering chip.
        if self._chip_fade_until and time.monotonic() >= self._chip_fade_until:
            self._chip_fade_until = 0.0
            self.mark_dirty()
        # Suggestion row self-dismisses after its linger window.
        if self.suggestions and time.monotonic() >= self._suggestions_until:
            self._dismiss_suggestions()
            self.mark_dirty()
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
        if self.review is not None:
            if isinstance(event, Key):
                self._handle_review_key(event.key, event.mods)
            elif isinstance(event, Mouse):
                self._handle_review_mouse(event)
            return
        if self.session_panel is not None:
            if isinstance(event, Key):
                self._handle_session_panel_key(event.key, event.mods)
            elif isinstance(event, Mouse):
                self._handle_session_panel_mouse(event)
            return
        if isinstance(event, Paste):
            if isinstance(self.overlay, LinePrompt):
                self.overlay.insert(event.text)
                self.mark_dirty()
                return
            # A modal (menu / question card) is open: drop the paste rather
            # than routing it to the hidden composer buffer.
            if self.overlay is not None and not isinstance(self.overlay, LinePrompt):
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
            if self.composer.popup_open:
                # The completion popup owns ESC first: dismiss it here
                # before the generic handlers claim the key, otherwise
                # the popup can never be closed by ESC and keeps
                # swallowing wheel events over its rect.
                self.composer._dismissed = True
                self.composer.completion = None
                self._popup_rect = None
                self.mark_dirty()
                return
            if self.selection.active:
                self.selection.clear()
                self.mark_dirty()
                return
            if self.suggestions:
                self._dismiss_suggestions()
                self.mark_dirty()
                return
            return
        # While the suggestion row is up it owns Tab and the number
        # keys: Tab moves between steps, 1-9 accept directly.
        if self.suggestions and self.transcript.follow and not self.busy:
            if key == "tab":
                n = len(self.suggestions)
                cur = self._suggestion_selected
                self._suggestion_selected = 0 if cur is None else (cur + 1) % n
                self.mark_dirty()
                return
            if key in "123456789" and len(key) == 1:
                idx = int(key) - 1
                if idx < len(self.suggestions):
                    self._accept_suggestion(idx)
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
                was_follow = self.transcript.follow
                missed = self.transcript.missed
                self.transcript.jump_bottom()
            if not was_follow:
                self._arm_chip_fade(missed)
            self.mark_dirty()
            return
        if key == "end" and not self.composer.buffer:
            # No text to move within: end jumps the transcript to the tail.
            with self.lock:
                was_follow = self.transcript.follow
                missed = self.transcript.missed
                self.transcript.jump_bottom()
            if not was_follow:
                self._arm_chip_fade(missed)
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
            if self._overlay_takes_wheel(ev):
                return
            # The wheel always scrolls the conversation; the completion
            # popup never claims it. That dropdown is a tall floating box
            # over the lower half of the transcript, so letting it swallow
            # wheel events left the conversation unscrollable whenever a
            # "/" or "@" completion was open - which is most of the time
            # while a path or command is being typed. The list is walked
            # with the arrow keys, or paged by clicking an item / its
            # "… more" row.
            if self._wheel_over_scrollbar(ev):
                return
            # The wheel always scrolls the conversation, even over the
            # prompt box: routing notches to a multi-line composer made the
            # box swallow the wheel, so the operator could not scroll the
            # transcript from the lower half of the window.
            with self.lock:
                # ~10 rows per notch: a full page overshot past the
                # context around the target, three rows took forever.
                # Small viewports still scroll by most of their height.
                step = min(10, max(3, self._content_height - 2))
                was_follow = self.transcript.follow
                missed = self.transcript.missed
                if ev.button == _WHEEL_UP:
                    self.transcript.scroll_up(step)
                else:
                    self.transcript.scroll_down(step)
                # Wheeling back down to the tail re-follows; the chip
                # echoes the jump as a fading remnant.
                if not was_follow and self.transcript.follow:
                    self._arm_chip_fade(missed)
            self.mark_dirty()
            return
        if ev.kind == "press" and ev.button == 0:
            if self._overlay_takes_click(ev):
                return
            # Full-rect hit test, not a column test: a press that shares
            # the dropdown's column but lands outside its rows belongs to
            # the transcript, and must start a selection like any other.
            in_popup = self._popup_rect is not None and (
                self._popup_rect[0] <= ev.x < self._popup_rect[0] + self._popup_rect[2]
                and self._popup_rect[1] <= ev.y < self._popup_rect[1] + self._popup_rect[3]
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
            if self._press_composer(ev.x, ev.y):
                return
            if self._press_scroll_to_bottom(ev.x, ev.y):
                return
            if self._press_scrollbar(ev.x, ev.y):
                return
            if self._press_suggestion(ev.x, ev.y):
                return
            if self._content_top <= ev.y < self._content_top + self._content_height:
                self.selection.begin_press(ev.x, ev.y)
            return
        if ev.kind == "drag" and ev.button == 0:
            if self._scrollbar_drag:
                self._drag_scrollbar(ev.y)
                return
            if self._drag_composer(ev.x, ev.y):
                return
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
            if self._scrollbar_drag:
                self._scrollbar_drag = False
                self.mark_dirty()
                return
            if self._composer_press:
                self._composer_press = False
                comp = self.composer
                if not comp.has_selection():
                    comp.clear_selection()
                self.mark_dirty()
                return
            if ev.button == 0 and self.selection._press_cell is not None:
                result = self.selection.end_press(
                    ev.x, ev.y, self.row_at, self._text_at
                )
                if result and result[0]:
                    copy_text(result[0])
                    n = len(result[0].splitlines())
                    self.toast_message(f"copied {n} line{'s' if n != 1 else ''}")
                self.mark_dirty()

    # ── composer mouse editing ────────────────────────────────

    def _press_suggestion(self, x: int, y: int) -> bool:
        """Click on a suggestion step: accept it (submit its command)."""
        for i, (sx, sy, sw) in enumerate(self._suggestion_rects):
            if sy == y and sx <= x < sx + sw:
                self._accept_suggestion(i)
                return True
        return False

    def _press_scroll_to_bottom(self, x: int, y: int) -> bool:
        """Click on the \"↓ bottom\" chip: jump back to the live tail."""
        chip = self._scroll_to_bottom
        if chip is None:
            return False
        chip_x, chip_y, chip_w = chip
        if y != chip_y or not chip_x <= x < chip_x + chip_w:
            return False
        with self.lock:
            missed = self.transcript.missed
            self.transcript.jump_bottom()
        # The jump is echoed: the chip lingers in fading style for a
        # second instead of popping out of existence.
        self._arm_chip_fade(missed)
        self.mark_dirty()
        return True

    def _press_scrollbar(self, x: int, y: int) -> bool:
        """Begin a scrollbar interaction at screen cell ``(x, y)``.

        Only the bar's own column consumes the press: a click on
        ordinary transcript text must still start a selection even when
        it shares a row with the track. A press on the thumb starts a
        drag anchored at the grab point so the thumb never jumps under
        the cursor; a press on the bare track pages the transcript
        toward that end. Returns True when the press was consumed.
        """
        bar = self._scrollbar
        if bar is None:
            return False
        track_x, top, height, thumb_top, thumb_span = bar
        if x != track_x or y < top or y >= top + height:
            return False
        self._scrollbar_drag = True
        self._scrollbar_anchor = y - (top + thumb_top)  # grab offset within the thumb
        if not (top + thumb_top) <= y < top + thumb_top + thumb_span:
            # Track press (not the thumb): page one viewport toward the
            # press. The step is a fixed viewport page - not the distance
            # to the press - and the transcript clamps it at either end.
            height_v = self.transcript.viewport_height
            toward_tail = y < top + thumb_top  # press above thumb: page up
            with self.lock:
                step = max(3, height_v - 2)
                if toward_tail:
                    self.transcript.scroll_down(step)
                else:
                    self.transcript.scroll_up(step)
            # Anchor mid-thumb after the page so the subsequent drag is
            # relative to wherever the thumb now sits.
            self._scrollbar_anchor = thumb_span // 2
        self.mark_dirty()
        return True

    def _wheel_over_scrollbar(self, ev: Mouse) -> bool:
        """Wheel on the bar's own column drags the thumb one step.

        The bar is a drag target, not just a readout: a notch on the track
        walks the thumb by exactly one thumb-step (the inverse of the
        render mapping), so the scrollbar can be operated with the wheel
        and lands between the page-sized jumps the conversation uses.
        While a drag is in progress the held button owns the bar, so a
        stray notch cannot snatch the thumb out from under the cursor.
        Returns True when the notch belonged to the bar.
        """
        bar = self._scrollbar
        if bar is None:
            return False
        if self._scrollbar_drag:
            # A held button owns the bar: falling through to the page step
            # here would drag the thumb out from under the cursor.
            return True
        track_x, top, height, _thumb_top, thumb_span = bar
        if ev.x != track_x or not (top <= ev.y < top + height):
            return False
        total = self.transcript.total_rows()
        height_v = self.transcript.viewport_height
        # One thumb-step: how far the view moves when the thumb is dragged
        # a single row (the same mapping, read backwards).
        step = max(1, max(1, total - height_v) // max(1, height - thumb_span))
        with self.lock:
            if ev.button == _WHEEL_UP:
                self.transcript.scroll_up(step)
            else:
                self.transcript.scroll_down(step)
        self.mark_dirty()
        return True

    def _drag_scrollbar(self, y: int) -> None:
        """Move the thumb so the grab point stays under the cursor."""
        bar = self._scrollbar
        if bar is None:
            return
        _track_x, top, height, _thumb_top, thumb_span = bar
        anchor = max(0, min(thumb_span - 1, getattr(self, "_scrollbar_anchor", 0)))
        thumb_top = max(0, min(height - thumb_span, y - anchor - top))
        total = self.transcript.total_rows()
        height_v = self.transcript.viewport_height
        max_offset = max(1, total - height_v)
        # Exact inverse of the render mapping (thumb top runs 0 at the
        # oldest content down to the track bottom near the tail), so the
        # grab point stays put instead of the thumb sliding inverted.
        offset = max_offset - thumb_top * max_offset // max(1, height - thumb_span)
        with self.lock:
            self.transcript.scroll_to_offset(offset)
        self.mark_dirty()

    def _composer_box(self) -> tuple[int, int, int] | None:
        """(cols, rows, box_height) for hit-testing the prompt box."""
        cols, rows = self.cols, self.rows
        if cols <= 0 or rows <= 0 or self.overlay is not None:
            return None
        return (cols, rows, self._composer_height(rows))

    def _press_composer(self, x: int, y: int) -> bool:
        """Press inside the prompt box: place the caret, start a selection."""
        box = self._composer_box()
        if box is None:
            return False
        cols, rows, box_height = box
        if not (rows - box_height <= y <= rows - 2):
            return False
        comp = self.composer
        off = comp.offset_at(y, x, cols, rows, box_height)
        if off is None:
            return True  # inside the box chrome: swallow, don't touch transcript
        self.selection.clear()
        comp.cursor = off
        comp.sel_anchor = off
        comp.sel_head = off
        self._composer_press = True
        self._history_idx = len(self._history)
        self.mark_dirty()
        return True

    def _drag_composer(self, x: int, y: int) -> bool:
        """Drag after a prompt-box press: extend the selection."""
        if not self._composer_press:
            return False
        box = self._composer_box()
        if box is None:
            return True
        cols, rows, box_height = box
        comp = self.composer
        off = comp.offset_at(y, x, cols, rows, box_height)
        if off is None:
            return True
        comp.sel_head = off
        comp.cursor = off
        self.mark_dirty()
        return True

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
        select_style = styles.id_for(STYLE_SELECT)

        # Below the minimum size the chrome cannot be laid out without
        # overlapping itself (the info bar, the transcript, the popup and
        # the prompt box all compete for the same few rows), so the frame
        # degrades to a single line of guidance. An open modal is still
        # drawn - clamped to the frame - so an approval waiting behind it
        # is never lost.
        if cols < MIN_COLS or rows < MIN_ROWS:
            self._render_minimal_frame(buf, styles, cols, rows)
            renderer.flush()
            return

        # Full-screen diff review replaces the whole chrome.
        if self.review is not None:
            self._render_review_frame(buf, styles, cols, rows)
            renderer.flush()
            return
        # Session manager panel replaces the whole chrome too.
        if self.session_panel is not None:
            self._render_session_panel_frame(buf, styles, cols, rows)
            renderer.flush()
            return

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
            # Scrollbar geometry, computed under the same lock: the
            # thumb mirrors the viewport's position in the whole pool
            # (display rows plus the live partial tail).
            scrolled = self.transcript.offset
            total_rows = self.transcript.total_rows()
        now = time.monotonic()  # shared by the thumb pulse and the chip
        self._visible_rows = visible
        self._content_top = content_top
        self._content_height = height
        first_display = visible[0][0] if visible else 0
        for i, (_idx, text) in enumerate(visible):
            buf.set_styled_line(0, content_top + i, text, styles, cols)
        # Scrollbar on the right edge while the view is detached from
        # the tail: hairline track with a bone-coloured thumb sized to
        # the viewport's share of the pool (never smaller than one row).
        # Thumb position follows convention: at the oldest content it
        # rides the top of the track, toward the tail it slides down.
        if scrolled > 0 and total_rows > height and height >= 4:
            track_x = cols - 1
            thumb_span = min(height, max(1, height * height // total_rows))
            max_offset = total_rows - height
            thumb_top = (height - thumb_span) * (max_offset - scrolled) // max_offset
            buf.set_str(track_x, content_top, "│" * height, hair_style)
            # Accent pulse: while new output streams in detached, the
            # thumb matches the chip's accent state so "the conversation
            # is growing" reads on the edge as well.
            if now < self._chip_flash_until:
                for cy in range(content_top + thumb_top, content_top + thumb_top + thumb_span):
                    buf.set_str(track_x, cy, "█", styles.id_for(STYLE_ACCENT))
            else:
                bone = styles.id_for(STYLE_BONE)
                for cy in range(content_top + thumb_top, content_top + thumb_top + thumb_span):
                    buf.set_str(track_x, cy, "█", bone)
            # Contrast band: the single text column hugging the thumb
            # renders faint so the thumb reads as an overlay, not as
            # content, without smearing a wide gutter into the text.
            faint = styles.id_for(STYLE_FAINT)
            if track_x > 0:
                for cy in range(content_top + thumb_top, content_top + thumb_top + thumb_span):
                    buf.set_style(track_x - 1, cy, 1, faint)
            self._scrollbar = (track_x, content_top, height, thumb_top, thumb_span)
        else:
            self._scrollbar = None

        # "↓ bottom" jump chip while the view is detached from the tail:
        # one click re-follows the live output without wheeling down. A
        # counter rides along ("+14") when output streamed in while
        # detached, so the operator can see how far behind they are.
        #
        # Two timed states layered on top of the live chip:
        #  - flash: output streamed in or a turn just finished while
        #    detached — chip and count render in the accent colour for
        #    CHIP_FLASH_SECONDS.
        #  - fade: just returned to the tail — the chip (with the missed
        #    count it had at jump time) lingers in faint style for
        #    CHIP_FADE_SECONDS instead of vanishing instantly.
        now = time.monotonic()
        chip_flashing = now < self._chip_flash_until
        chip_fading = not chip_flashing and now < self._chip_fade_until
        if scrolled > 0 and height >= 1:
            missed = getattr(self.transcript, "missed", 0)
            label_style = theme.BLOOD_BOLD if chip_flashing else theme.INFO
            count_style = theme.BLOOD if chip_flashing else theme.FAINT
            chip = f"\033[{label_style}m↓ bottom\033[0m"
            if missed:
                chip += f" \033[{count_style}m+{missed}\033[0m"
            chip_w = visible_len(chip)
            chip_x = max(0, cols - 2 - chip_w)  # one cell clear of the track
            chip_y = content_top + height - 1
            buf.set_styled_line(chip_x, chip_y, chip, styles, cols)
            self._scroll_to_bottom = (chip_x, chip_y, chip_w)
        elif chip_fading:
            # Echo of the jump: frozen count, faint styling, no hit rect
            # (it must not swallow clicks meant for the text below).
            chip = f"\033[{theme.FAINT}m↓ bottom\033[0m"
            if self._chip_fade_missed:
                chip += f" \033[{theme.FAINT}m+{self._chip_fade_missed}\033[0m"
            chip_w = visible_len(chip)
            chip_x = max(0, cols - 2 - chip_w)
            chip_y = content_top + height - 1
            buf.set_styled_line(chip_x, chip_y, chip, styles, cols)
            self._scroll_to_bottom = None
        else:
            self._scroll_to_bottom = None

        # Post-task suggestion row: one dim line of numbered next steps
        # at the tail of the content area, directly under the finished
        # turn's output. Plain text, not chips: a row of boxes reads as
        # chrome the operator has to decode, while "next  1 run the
        # tests · 2 commit" is the same information in the same voice as
        # the conversation it follows. 1-9 accept, a click on a step runs
        # it; the row self-dismisses after SUGGESTION_LINGER, on submit,
        # or on Esc.
        self._suggestion_rects = []
        if self.suggestions and self.transcript.follow:
            if now < self._suggestions_until:
                x = 1
                y = content_top + height - 1
                label = f"{theme.FAINT}m next "
                buf.set_styled_line(x, y, f"\033[{label}\033[0m", styles, cols)
                x += visible_len(" next ")
                for i, text in enumerate(self.suggestions):
                    if i == self._suggestion_selected:
                        step = f"\033[{theme.INFO}m {i + 1} {text} \033[0m"
                    else:
                        step = f"\033[{theme.HAIR}m {i + 1} {text} \033[0m"
                    w = visible_len(step)
                    if x + w > cols - 1:
                        break  # no room for more steps this frame
                    buf.set_styled_line(x, y, step, styles, cols)
                    self._suggestion_rects.append((x, y, w))
                    x += w + 1  # one blank cell between steps
            else:
                self._dismiss_suggestions()
        elif self.suggestions and not self.transcript.follow:
            # Detached from the tail: hold the row (no expiry countdown
            # while hidden) so returning re-reveals it.
            self._suggestions_until = max(self._suggestions_until, time.monotonic() + 5.0)

        # Transient toast (state flips, copies, quit warnings): centered
        # on the top hairline row, riding over the rule for its
        # lifetime; the next frame after expiry restores the line.
        if self.toast and now < self._toast_until:
            toast_line = f" {self.toast} "
            tw = visible_len(toast_line)
            if tw < cols - 2:
                tx = max(0, (cols - tw) // 2)
                buf.set_styled_line(tx, 1, f"\033[{theme.WARN}m{toast_line}\033[0m", styles, cols)
        # Selection highlight.
        for offset, start, end in self.selection.overlay_rows(first_display, height):
            buf.set_style(start, content_top + offset, min(end, cols) - start, select_style)

        # Centered welcome card: shown while the transcript is still
        # empty, re-centered every frame so it follows resized windows.
        if self._welcome_card and not self.transcript.raw and height > 0:
            # Only the lines that fit the content area are drawn: a short
            # window clips the card instead of letting it paint over the
            # status row and the prompt box below it.
            card = self._welcome_lines[:height]
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
        # While detached, the missed count rides beside the offset marker
        # (" ^12 +3") so the growth stays visible while typing — even
        # when the chip itself is scrolled out of view.
        missed_now = getattr(self.transcript, "missed", 0)
        if self.transcript.scrolled and missed_now:
            marker += f" +{missed_now}"
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
        for sy, c0, c1 in self.composer.selection_spans(cols, rows, composer_height):
            if c1 > c0:
                buf.set_style(c0, sy, min(c1, cols) - c0, select_style)
        buf.set_str(0, rows - 1, "╰" + "─" * max(0, cols - 2) + "╯", hair_style)
        renderer.set_cursor(caret, rows - 2)

        # Completion popup above the border row.
        comp = self.composer
        # Adaptive window: the dropdown grows with the viewport (a tall
        # terminal shows more of the list than the composer's own
        # preference) but never eats the conversation, and always leaves
        # room for the box borders plus the two overflow rows
        # ("… n above" / "… n more") above the status line. Under two item
        # rows there is nothing worth drawing, so the dropdown is skipped
        # rather than colliding with the info bar.
        space = border_row - content_top - 4
        if comp.popup_open and space >= 2:
            max_rows = max(1, min(max(comp.max_popup, space // 2), space, _POPUP_ROWS_CAP))
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
                comp.column_for(comp.completion.start, cols),
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

    def _render_minimal_frame(self, buf, styles, cols: int, rows: int) -> None:
        """The whole frame when the terminal is too small to lay out.

        One line of guidance instead of a chrome that paints over itself,
        plus an open modal card (clamped to the frame) so a pending
        approval stays visible and answerable.
        """
        notice = f"terminal too small ({cols}x{rows}) - resize to continue"
        if rows >= 1 and cols >= 8:
            line = notice[: max(0, cols - 1)]
            x = max(0, (cols - visible_len(line)) // 2)
            buf.set_styled_line(x, 0, f"\033[{theme.WARN}m{line}\033[0m", styles, cols)
        if self.overlay is not None:
            self.overlay.render(buf, cols, rows)

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
        approval = getattr(getattr(s, "approvals", None), "mode", "yolo")
        totals = getattr(s, "totals", {}) or {}
        tokens_in = totals.get("tokens_in", 0)
        cache = totals.get("cache_hit", 0)
        rate = f"{cache * 100 // tokens_in}%" if tokens_in else "0%"

        # Coloured fields, same theme as the transcript: faint labels,
        # semantic values (model in info blue, approval by mode, cache
        # in sage), hairline separators.
        st = self.session.style

        def label(t: str) -> str:
            return st._wrap(theme.FAINT, t)
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
        if getattr(s, "last_error", None):
            # A recent failure waits: /fix sends it to the agent.
            parts.append(st._wrap(theme.EMBER, "[!] FIX"))
        return (" " + st._wrap(theme.HAIR, "·") + " ").join(parts)

    # ── full-screen diff review ─────────────────────────────

    def _render_review_frame(self, buf, styles, cols: int, rows: int) -> None:
        review = self.review
        frame = render_review(review, cols, rows)
        review._total = frame.total
        viewport = max(1, rows - 2)
        review.clamp(frame.total, viewport)
        # Header row: current file + stats.
        buf.set_styled_line(0, 0, _styled(frame.header[:cols], theme.BONE_BOLD), styles, cols)
        # Sidebar (files), body (hunks), footer (key hints).
        body_top = 1
        body_bottom = rows - 2
        for y, row in enumerate(frame.sidebar[: max(0, body_bottom - body_top)]):
            buf.set_styled_line(0, body_top + y, _styled(row.text, _REVIEW_STYLE_SGR.get(row.style)), styles, cols)
        for y, row in enumerate(frame.body):
            if body_top + y > body_bottom:
                break
            buf.set_styled_line(frame.sidebar_w, body_top + y, _styled(row.text, _REVIEW_STYLE_SGR.get(row.style)), styles, cols)
        buf.set_styled_line(0, rows - 1, _styled(frame.footer[:cols], theme.FAINT), styles, cols)

    def _handle_review_key(self, key: str, mods: frozenset) -> None:
        review = self.review
        if review is None:
            return
        viewport = max(1, self.rows - 2)
        total = int(getattr(review, "_total", 0) or 0)
        if key in ("q", "esc"):
            self.review = None
        elif key in ("up", "k"):
            review.step(-1, total, viewport)
        elif key in ("down", "j"):
            review.step(1, total, viewport)
        elif key in ("pageup", "pagedown"):
            review.step((-1 if key == "pageup" else 1) * max(3, viewport - 2), total, viewport)
        elif key in ("home", "end"):
            review.offset = 0 if key == "home" else max(0, total - viewport)
        elif key in ("tab", "l") or (key == "tab" and "shift" in mods) or key == "h":
            review.next_file(-1 if key == "h" or (key == "tab" and "shift" in mods) else 1)
        elif key == "s":
            review.split = not review.split
        self.mark_dirty()

    def _overlay_takes_click(self, ev: Mouse) -> bool:
        """A click inside an open modal card belongs to the card.

        A menu highlights the option under the pointer (clicking the
        highlighted one accepts it); an approval card's drawn buttons
        answer it. Any other cell inside the card is still swallowed, so a
        modal never lights up a selection on the transcript behind it -
        but clicks outside the card keep reaching the conversation.
        """
        overlay = self.overlay
        if not isinstance(overlay, (MenuOverlay, QuestionCard, LinePrompt)):
            return False
        if not overlay.click(ev.x, ev.y):
            return False
        if overlay.finished:
            if isinstance(overlay, MenuOverlay):
                self._finish_overlay(None if overlay.cancelled else overlay.result)
            else:
                self._finish_overlay(overlay.answer)
        self.mark_dirty()
        return True

    def _overlay_takes_wheel(self, ev: Mouse) -> bool:
        """Route a wheel notch to an open modal card when it is over it.

        A menu box takes the notch to walk its options, an approval card to
        scroll a long body, and a key prompt to slide a value wider than
        the card; wheeling *outside* the card keeps scrolling the
        conversation behind it, so a modal never makes the transcript
        unreachable. A card that cannot act on the notch (a body that
        fits, a value that fits, a one-entry menu) does not claim it: the
        notch falls through to the conversation instead of being swallowed
        by a surface with nothing left to move.
        """
        overlay = self.overlay
        if overlay is None or not isinstance(
            overlay, (MenuOverlay, QuestionCard, LinePrompt)
        ):
            return False
        rect = getattr(overlay, "rect", None)
        if rect is None:
            return False
        x, y, w, h = rect
        if not (x <= ev.x < x + w and y <= ev.y < y + h):
            return False
        if not overlay.wants_wheel():
            return False
        overlay.consume_wheel(-1 if ev.button == _WHEEL_UP else 1)
        self.mark_dirty()
        return True

    def _handle_review_mouse(self, ev: Mouse) -> None:
        """Wheel the full-screen diff review: a quarter page per notch.

        Mouse events reaching ``handle_event`` while the review owns the
        screen used to be dropped, which left the diff view unscrollable
        with the wheel.
        """
        review = self.review
        if review is None or ev.kind != "wheel":
            return
        viewport = max(1, self.rows - 2)
        total = int(getattr(review, "_total", 0) or 0)
        step = max(3, viewport // 4)
        review.step(-step if ev.button == _WHEEL_UP else step, total, viewport)
        self.mark_dirty()

    # ── session manager panel ────────────────────────────────

    def _handle_session_panel_mouse(self, ev: Mouse) -> None:
        """Wheel the session list one entry per notch (two rows each)."""
        panel = self.session_panel
        if panel is None or ev.kind != "wheel":
            return
        total = len(panel.entries)
        entry_viewport = max(1, max(2, self.rows - 2) // 2)
        panel.next(-1 if ev.button == _WHEEL_UP else 1, total)
        if panel.index < panel.offset:
            panel.offset = panel.index
        elif panel.index >= panel.offset + entry_viewport:
            panel.offset = panel.index - entry_viewport + 1
        self.mark_dirty()

    def _render_session_panel_frame(self, buf, styles, cols: int, rows: int) -> None:
        panel = self.session_panel
        frame = render_session_panel(panel, cols, rows)
        panel.clamp(frame.total, max(1, rows - 2))
        buf.set_styled_line(0, 0, _styled(frame.header[:cols], theme.BONE_BOLD), styles, cols)
        body_top = 1
        body_bottom = rows - 2
        for y, row in enumerate(frame.rows):
            if body_top + y > body_bottom:
                break
            buf.set_styled_line(0, body_top + y, _styled(row.text, _REVIEW_STYLE_SGR.get(row.style)), styles, cols)
        buf.set_styled_line(0, rows - 1, _styled(frame.footer[:cols], theme.FAINT), styles, cols)

    def _handle_session_panel_key(self, key: str, mods: frozenset) -> None:
        panel = self.session_panel
        if panel is None:
            return
        total = len(panel.entries)
        # Each entry renders two rows; keep scroll math in entry units.
        body_rows = max(2, self.rows - 2)
        entry_viewport = max(1, body_rows // 2)
        if key in ("q", "esc"):
            self.session_panel = None
        elif key in ("up", "k"):
            panel.next(-1, total)
            if panel.index < panel.offset:
                panel.offset = panel.index
        elif key in ("down", "j"):
            # j moves the cursor down one entry, scrolling the offset
            # forward if the cursor would leave the visible window.
            panel.index = min(panel.index + 1, total - 1)
            if panel.index >= panel.offset + entry_viewport:
                panel.offset = panel.index - entry_viewport + 1
        elif key in ("pageup", "pagedown"):
            step = max(1, entry_viewport - 1)
            panel.next(-step if key == "pageup" else step, total)
            panel.offset = max(0, min(total - entry_viewport, panel.index))
        elif key in ("home", "end"):
            panel.index = 0 if key == "home" else max(0, total - 1)
            panel.offset = max(0, panel.index - entry_viewport + 1) if key == "end" else 0
        elif key in ("enter", "return"):
            entry = panel.entry
            self.session_panel = None
            if entry is not None and self.session_panel_on_enter is not None:
                # Resume on a worker thread: it restores state and
                # prints into the transcript through the layout bridge.
                import threading

                threading.Thread(
                    target=self.session_panel_on_enter, args=(entry.name,), daemon=True
                ).start()
        self.mark_dirty()

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


# Imported lazily so the import order never matters: core.console imports
# this module's helpers at its tail, and the tests patch both namespaces.
def dispatch_session(session, line: str) -> bool:
    from core.console import dispatch

    return dispatch(session, line)
