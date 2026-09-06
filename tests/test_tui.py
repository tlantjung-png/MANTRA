"""Tests for the terminal-application layer.

The backend is faked with a scripted event queue and a recording writer,
so frames can be asserted straight off the cell grid — no ANSI parsing,
no real terminal.
"""

from __future__ import annotations

import os
import queue
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "."))
_tests_dir = os.path.dirname(os.path.abspath(__file__))
if _tests_dir not in sys.path:
    sys.path.insert(0, _tests_dir)

import core.tui.app as app_module
from core.tui.app import LayoutBridge, TuiApp
from core.tui.backend import Backend, Key, Mouse, Paste, Resize
from core.tui.buffer import Buffer, Renderer, parse_ansi_spans
from core.tui.composer import Composer
from core.tui.overlays import MenuOverlay, Option
from core.tui.selection import Selection
from core.tui.transcript import Transcript, wrap_ansi

from _helpers import make_session


class FakeBackend:
    def __init__(self, cols: int = 100, rows: int = 30):
        self.events: queue.Queue = queue.Queue()
        self.size = (cols, rows)
        self.writes: list[str] = []
        self.stopped = False

    def write(self, text: str) -> None:
        self.writes.append(text)

    def stop(self) -> None:
        self.stopped = True

    def current_size(self) -> tuple[int, int]:
        return self.size

    def emit_resize_if_changed(self) -> None:
        pass


def grid_rows(buf: Buffer) -> list[str]:
    out = []
    for y in range(buf.rows):
        out.append("".join(buf.chars[y * buf.cols : (y + 1) * buf.cols]).rstrip())
    return out


def wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


class BufferTest(unittest.TestCase):
    def setUp(self):
        from core.tui.buffer import StyleTable

        self.table = StyleTable()

    def test_ansi_spans_accumulate_until_reset(self):
        spans = parse_ansi_spans("\033[1mbold\033[0m plain \033[38;5;131mred", self.table)
        self.assertEqual(spans[0][0], "bold")
        self.assertEqual(self.table.params_for(spans[0][1]), ("1",))
        self.assertEqual(spans[1][0], " plain ")
        self.assertEqual(spans[1][1], 0)
        self.assertEqual(spans[2][0], "red")
        self.assertIn("38", self.table.params_for(spans[2][1]))

    def test_cursor_escapes_are_dropped(self):
        spans = parse_ansi_spans("a\033[2;3Hb", self.table)
        self.assertEqual("".join(s[0] for s in spans), "ab")
        self.assertTrue(all(s[1] == 0 for s in spans))

    def test_wide_char_occupies_two_cells(self):
        buf = Buffer(10, 2, self.table)
        end = buf.set_str(0, 0, "漢字")
        self.assertEqual(end, 4)
        self.assertEqual(buf.chars[0], "漢")
        self.assertEqual(buf.chars[1], "\x00")

    def test_set_str_clips_at_width(self):
        buf = Buffer(5, 1, self.table)
        end = buf.set_str(3, 0, "abcdef")
        self.assertEqual(end, 5)
        self.assertEqual("".join(buf.chars), "   ab")

    def test_flush_emits_only_changed_runs(self):
        class Rec:
            def __init__(self):
                self.chunks = []

            def write(self, text):
                self.chunks.append(text)

        rec = Rec()
        renderer = Renderer(rec, 20, 3)
        renderer.buffer.set_str(0, 0, "hello")
        renderer.flush()
        first = "".join(rec.chunks)
        self.assertIn("hello", first)
        # Second flush with no change writes only the cursor placement.
        rec.chunks.clear()
        renderer.flush()
        self.assertNotIn("hello", "".join(rec.chunks))
        # A change to one row emits only that row's position.
        rec.chunks.clear()
        renderer.buffer.set_str(0, 2, "world")
        renderer.flush()
        third = "".join(rec.chunks)
        self.assertIn("world", third)
        self.assertIn("\033[3;1H", third)
        self.assertNotIn("\033[1;1H", third)


class TranscriptTest(unittest.TestCase):
    def test_sanitize_ingest_keeps_sgr_esc_byte(self):
        # Regression: the control-char strip used to eat the ESC byte of
        # kept SGR sequences, leaving literal "[2m" codes that rendered
        # as text and never coloured the line.
        from core.tui.transcript import sanitize_ingest

        line = "\x1b[2m18:13\x1b[0m  Hello\n\x1b[1;38;5;167mENCHANTER\x1b[0m Hi\n"
        kept = sanitize_ingest(line)
        self.assertIn("\x1b[2m", kept)
        self.assertIn("\x1b[1;38;5;167m", kept)
        self.assertIn("\x1b[0m", kept)
        self.assertNotIn("\n[2m", kept)  # no ESC-less code text survives
        # Non-SGR escapes are still dropped entirely.
        self.assertEqual(sanitize_ingest("a\x1b[2Jb"), "ab")

    def test_partial_lines_commit_on_newline(self):
        t = Transcript()
        t.set_width(40)
        t.append_partial("hel")
        self.assertEqual(t.raw, [])
        t.append_partial("lo\nworld\n")
        self.assertEqual(t.raw, ["hello", "world"])
        self.assertEqual(t.partial, "")

    def test_wrapping_is_cached_per_width(self):
        t = Transcript()
        t.set_width(10)
        t.append("a" * 25)
        self.assertEqual(len(t.display), 3)
        t.set_width(30)
        self.assertEqual(len(t.display), 1)

    def test_follow_and_scroll_semantics(self):
        t = Transcript()
        t.set_width(40)
        t.viewport_height = 10
        for i in range(20):
            t.append(f"line {i}")
        self.assertEqual(t.offset, 0)
        t.scroll_up(3)
        self.assertEqual(t.offset, 3)
        self.assertFalse(t.follow)
        t.append("new line")
        self.assertEqual(t.offset, 3)  # detached: the tail does not drag
        # Scrolling past the end clamps instead of showing blank rows.
        t.scroll_up(500)
        self.assertEqual(t.offset, len(t.display) + (1 if t.partial else 0) - 10)
        t.scroll_down(20)
        self.assertTrue(t.follow)
        self.assertEqual(t.offset, 0)

    def test_wrap_ansi_keeps_styling_across_rows(self):
        lines = wrap_ansi("\033[1m" + "x" * 15 + "\033[0m", 10)
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[0].startswith("\033[1m"))
        self.assertTrue(lines[1].startswith("\033[1m"))


class ComposerTest(unittest.TestCase):
    def test_typing_and_submit(self):
        c = Composer()
        for ch in "hi":
            c.consume_key(ch)
        self.assertEqual(c.buffer, "hi")
        c.consume_key("enter")
        self.assertEqual(c.submitted, "hi")
        self.assertEqual(c.buffer, "")

    def test_completion_accept_with_enter(self):
        c = Composer()
        c.completer = type("C", (), {"complete": staticmethod(
            lambda buffer, cursor: None
        )})
        c.buffer = "/mo"
        c.cursor = 3
        c.completion = type("Cp", (), {
            "start": 0, "end": 3,
            "items": ["/model", "/mode"],
            "labels": None,
            "label": lambda self, i: self.items[i],
        })()
        c.selected = 0
        c._dismissed = False
        c.consume_key("enter")
        self.assertEqual(c.buffer, "/model")

    def test_backspace_and_word_motion(self):
        c = Composer()
        c.buffer = "hello world"
        c.cursor = 11
        c.consume_key("backspace")
        self.assertEqual(c.buffer, "hello worl")
        c.consume_key("ctrl+left")
        self.assertEqual(c.cursor, 6)
        c.consume_key("ctrl+w")
        self.assertEqual(c.buffer, "worl")

    def test_newline_key_inserts_break(self):
        c = Composer()
        for ch in "ab":
            c.consume_key(ch)
        c.consume_key("newline")
        c.consume_key("c")
        self.assertEqual(c.buffer, "ab\nc")
        self.assertTrue(c.is_multiline)

    def test_paste_normalises_newlines(self):
        c = Composer()
        c.consume_paste("a\r\nb\rc")
        self.assertEqual(c.buffer, "a\nb\nc")


class SelectionTest(unittest.TestCase):
    def _rows(self, y=None):
        rows = {3: (10, "hello world"), 4: (11, "second line")}
        return rows.get(y)

    def _text(self, idx):
        return {10: "hello world", 11: "second line"}.get(idx, "")

    def test_click_without_drag_clears(self):
        sel = Selection()
        sel.begin_press(2, 3)
        self.assertIsNone(sel.end_press(2, 3, self._rows, self._text))
        self.assertFalse(sel.active)

    def test_drag_selects_two_rows(self):
        sel = Selection()
        sel.begin_press(0, 3)
        sel.begin_drag(11, 4, self._rows)
        self.assertTrue(sel.active)
        result = sel.end_press(11, 4, self._rows, self._text)
        text, was_drag = result
        self.assertTrue(was_drag)
        self.assertIn("hello world", text)
        self.assertIn("second line", text)

    def test_small_motion_never_selects(self):
        sel = Selection()
        sel.begin_press(2, 3)
        sel.begin_drag(3, 3, self._rows)
        self.assertFalse(sel.active)


class BackendParseTest(unittest.TestCase):
    """The terminal input parser must translate SGR mouse reports the way
    the selection logic expects, and the app must request the mouse mode
    that delivers drag reports at all."""

    def _parse(self, buf: str):
        backend = Backend()
        backend._parse_stream(buf, 0)
        return backend.events

    def test_sgr_drag_report_becomes_drag_event(self):
        # Button bit 32 marks motion with a button held; the selection
        # only starts on "drag" events.
        events = self._parse("\x1b[<32;6;7M")
        self.assertEqual(events.qsize(), 1)
        ev = events.get_nowait()
        self.assertIsInstance(ev, Mouse)
        self.assertEqual(ev.kind, "drag")
        self.assertEqual(ev.button, 0)
        self.assertEqual((ev.x, ev.y), (5, 6))

    def test_sgr_press_and_release(self):
        events = self._parse("\x1b[<0;6;7M\x1b[<0;6;7m")
        kinds = [events.get_nowait().kind for _ in range(events.qsize())]
        self.assertEqual(kinds, ["press", "release"])

    def test_sgr_drag_burst_consumes_every_report(self):
        # A fast drag packs several reports into one read; each must be
        # decoded, not just the one at the end of the buffer.
        events = self._parse("\x1b[<0;6;7M\x1b[<32;7;8M\x1b[<32;8;9M\x1b[<0;8;9m")
        kinds = [events.get_nowait().kind for _ in range(events.qsize())]
        self.assertEqual(kinds, ["press", "drag", "drag", "release"])

    def test_mouse_mode_requests_button_event_tracking(self):
        # Regression: only ?1000 (press/release) was enabled, so real
        # terminals never sent drag reports and selection could not start.
        self.assertIn("?1002h", Backend._MOUSE_ON)

    def test_windows_arrow_keys_decode_by_virtual_key(self):
        # A real Windows KEY_EVENT for an arrow carries VK_UP etc. with a
        # NUL uChar. The NUL used to be read as Ctrl+Space, which dropped
        # every arrow key before the virtual-key lookup ran - so the
        # completion popup could not be navigated with the keyboard.
        from types import SimpleNamespace

        backend = Backend()

        def decode(vk, uchar="\x00", mods=frozenset()):
            key = SimpleNamespace(wVirtualKeyCode=vk, uChar=uchar, dwControlKeyState=0)
            return backend._win_key_to_event(key, mods)

        self.assertEqual(decode(0x26), [Key("up")])
        self.assertEqual(decode(0x28), [Key("down")])
        self.assertEqual(decode(0x25), [Key("left")])
        self.assertEqual(decode(0x27), [Key("right")])
        self.assertEqual(decode(0x21), [Key("pageup")])
        self.assertEqual(decode(0x22), [Key("pagedown")])
        # Ctrl+Space (NUL) keeps its control identity instead of a space.
        self.assertEqual(decode(0x20, mods=frozenset({"ctrl"})),
                         [Key("ctrl+space", frozenset({"ctrl"}))])


class MenuOverlayTest(unittest.TestCase):
    def test_filter_narrows_and_enter_selects(self):
        menu = MenuOverlay("pick", [Option("alpha"), Option("beta")])
        for ch in "alp":
            menu.consume_key(ch)
        menu.consume_key("enter")
        self.assertEqual(menu.result, "alpha")

    def test_delete_removes_without_selecting(self):
        removed = []
        menu = MenuOverlay(
            "pick", [Option("alpha"), Option("beta")],
            allow_delete=True, on_delete=removed.append,
        )
        menu.consume_key("d")
        self.assertEqual(removed, ["alpha"])
        self.assertIsNone(menu.result)
        menu.consume_key("enter")
        self.assertEqual(menu.result, "beta")


def _make_app(script, backend=None):
    workspace = os.path.join(
        os.environ.get("TEMP", tempfile.gettempdir()), f"mantra-tui-{os.getpid()}-{threading.get_ident()}"
    )
    os.makedirs(workspace, exist_ok=True)
    _TEMP_WORKSPACES.append(workspace)
    session = make_session(workspace, script)
    backend = backend or FakeBackend()
    app = TuiApp(session, backend=backend)
    app._init_surface()
    app._show_welcome()
    return app, session, backend


import shutil  # noqa: E402
import tempfile  # noqa: E402

# Workspaces _make_app (and the popup test) create under TEMP; removed in
# AppIntegrationTest.tearDown so the suite leaves no stray directories
# (each one may contain a git repo, which would otherwise linger forever).
_TEMP_WORKSPACES: list[str] = []


class AppIntegrationTest(unittest.TestCase):
    def tearDown(self):
        for ws in list(_TEMP_WORKSPACES):
            shutil.rmtree(ws, ignore_errors=True)
        _TEMP_WORKSPACES.clear()
    def test_turn_lands_in_the_grid(self):
        from core.scripted import LLMResponse

        app, session, backend = _make_app([LLMResponse(content="Hello there")])
        app.render_frame()
        app.submit("Hello")
        self.assertTrue(wait_until(lambda: not app.busy))
        app.render_frame()
        rows = grid_rows(app.renderer.buffer)
        joined = "\n".join(rows)
        self.assertIn("Hello", joined)
        self.assertIn("Hello there", joined)
        # The usage footer was printed by the session through the bridge.
        self.assertTrue(any("STEP" in row or "in context" in row or "CTX" in row for row in rows))

    def test_typing_reaches_the_composer_and_renders(self):
        app, session, backend = _make_app([])
        for ch in "hi there":
            app.handle_event(Key(ch))
        app.render_frame()
        joined = "\n".join(grid_rows(app.renderer.buffer))
        self.assertIn("hi there", joined)

    def test_escape_aborts_only_when_busy(self):
        from core.scripted import LLMResponse

        app, session, backend = _make_app([LLMResponse(content="slow reply")])
        app.submit("go")
        self.assertTrue(wait_until(lambda: app.busy))
        app.handle_event(Key("esc"))
        self.assertTrue(session._abort.is_set())
        self.assertTrue(wait_until(lambda: not app.busy))

    def test_approval_card_round_trip(self):
        app, session, backend = _make_app([])
        results = []
        app.run_detached(lambda: results.append(app.ask_approval("may I write files?")))
        self.assertTrue(wait_until(lambda: app.overlay is not None))
        self.assertTrue(wait_until(lambda: app._overlay_reply is not None))
        # Render: the card is on screen.
        app.render_frame()
        joined = "\n".join(grid_rows(app.renderer.buffer))
        self.assertIn("allow?", joined)
        app.handle_event(Key("y"))
        self.assertTrue(wait_until(lambda: results == ["y"]))
        self.assertIsNone(app.overlay)

    def test_menu_round_trip(self):
        app, session, backend = _make_app([])
        results = []
        app.run_detached(lambda: results.append(
            app.choose("pick one", [Option("first"), Option("second")])
        ))
        self.assertTrue(wait_until(lambda: app.overlay is not None))
        app.handle_event(Key("enter"))
        self.assertTrue(wait_until(lambda: results == ["first"]))

    def test_resize_reflows_the_grid(self):
        app, session, backend = _make_app([])
        old = app_module.RESIZE_DEBOUNCE
        app_module.RESIZE_DEBOUNCE = 0.0
        try:
            app.handle_event(Resize(70, 22))
            self.assertTrue(wait_until(lambda: app.apply_pending_resize()))
            app.render_frame()
            self.assertEqual(app.renderer.buffer.cols, 70)
            self.assertEqual(app.renderer.buffer.rows, 22)
        finally:
            app_module.RESIZE_DEBOUNCE = old

    def test_wheel_scrolls_and_shows_the_marker(self):
        app, session, backend = _make_app([])
        with app.lock:
            for i in range(60):
                app.transcript.append(f"line {i}")
        app.render_frame()
        app.handle_event(Mouse("wheel", 64, 5, 5, frozenset()))
        self.assertGreater(app.transcript.scrolled, 0)
        app.render_frame()
        joined = "\n".join(grid_rows(app.renderer.buffer))
        self.assertIn("^", joined)

    def test_empty_composer_arrow_keys_scroll_the_transcript(self):
        # Regression: up/down on an empty composer were a no-op, so
        # arrow-key users could not scroll after a reply.
        app, session, backend = _make_app([])
        with app.lock:
            for i in range(60):
                app.transcript.append(f"line {i}")
        app.handle_event(Key("up"))
        self.assertGreater(app.transcript.offset, 0)
        self.assertFalse(app.transcript.follow)
        app.handle_event(Key("home"))
        self.assertEqual(app.transcript.scrolled, app.transcript.offset)  # jumped up
        app.handle_event(Key("down"))
        self.assertLess(app.transcript.offset, 60)
        app.handle_event(Key("end"))
        self.assertEqual(app.transcript.offset, 0)
        self.assertTrue(app.transcript.follow)

    def test_arrow_keys_do_not_scroll_while_typing(self):
        # With text in the composer, up/down stay caret navigation.
        app, session, backend = _make_app([])
        with app.lock:
            for i in range(60):
                app.transcript.append(f"line {i}")
        for ch in "hi":
            app.handle_event(Key(ch))
        app.handle_event(Key("up"))
        self.assertEqual(app.transcript.offset, 0)
        self.assertTrue(app.transcript.follow)

    def test_prompt_box_is_a_closed_rectangle(self):
        # The prompt is a full rectangle: status row as the top edge,
        # wall rows for the input, and a solid bottom edge underneath.
        app, session, backend = _make_app([])
        app.render_frame()
        grid = grid_rows(app.renderer.buffer)
        top = grid[app.rows - 3]
        content = grid[app.rows - 2]
        bottom = grid[app.rows - 1]
        self.assertTrue(top.startswith("╭") and top.endswith("╮"))
        self.assertTrue(content.startswith("│") and content.endswith("│"))
        self.assertTrue(bottom.startswith("╰") and bottom.endswith("╯"))
        self.assertEqual(len(bottom), app.renderer.buffer.cols)

    def test_multiline_chip_sits_inside_the_rectangle(self):
        # Multiline input keeps a single top edge; the line-count chip is
        # a divider row inside the box, not a second box top.
        app, session, backend = _make_app([])
        for ch in "ab":
            app.handle_event(Key(ch))
        app.handle_event(Key("newline"))
        app.handle_event(Key("c"))
        app.render_frame()
        grid = grid_rows(app.renderer.buffer)
        bottom_rows = "\n".join(grid[app.rows - 6 :])
        self.assertEqual(bottom_rows.count("╭"), 1)
        self.assertEqual(bottom_rows.count("╰"), 1)
        chip_row = next(r for r in grid if "lines" in r)
        self.assertTrue(chip_row.startswith("│") and chip_row.endswith("│"))
        self.assertNotIn("╭", chip_row)

    def test_status_chip_does_not_leak_prompt_label(self):
        # The live token counter rides the prompt body ("│ MANTRA > 1 tok ·");
        # only the counter part belongs in the border chip.
        app, session, backend = _make_app([])
        session.layout.draw_prompt("\033[2m│ \033[0m\033[1mMANTRA >\033[0m 1 tok ·")
        self.assertEqual(app.counter_text, "1 tok ·")
        self.assertNotIn("MANTRA", app.counter_text)

    def test_counter_chip_clears_when_busy_ends(self):
        app, session, backend = _make_app([])
        app.set_counter("1 tok ·")
        app.set_busy(False)
        self.assertEqual(app.counter_text, "")

    def test_busy_border_shows_elapsed_and_counter(self):
        # While a turn runs the border chip shows the spinner, the label,
        # the elapsed time and the live token counter with its rate - but
        # no queued/toast side chips.
        import re

        app, session, backend = _make_app([])
        app._turn_started = time.monotonic() - 2
        app.set_busy(True, label="Chanting")
        app.counter_text = "1 tok · · 12 tok/s"
        app.queued = "something"
        app.toast = "note"
        app.render_frame()
        border = grid_rows(app.renderer.buffer)[app.rows - 3]
        self.assertIn("Chanting", border)
        self.assertRegex(border, r"\d+s")
        self.assertIn("1 tok ·", border)
        self.assertIn("tok/s", border)
        self.assertNotIn("queued", border)
        self.assertNotIn("note", border)

    def test_idle_border_is_clean(self):
        # The model/approval summary lives in the top bar; the idle
        # border chip stays clean and shows only a queued prompt notice.
        app, session, backend = _make_app([])
        app.render_frame()
        border = grid_rows(app.renderer.buffer)[app.rows - 3]
        self.assertNotIn("approval", border)
        self.assertNotIn("gpt-4o", border)
        self.assertEqual(border.strip("╭╮─ "), "")
        app.queued = "message"
        app.render_frame()
        border = grid_rows(app.renderer.buffer)[app.rows - 3]
        self.assertIn("queued prompt", border)
        app.queued = ""
        app.toast = "copied 1 line"
        app.render_frame()
        border = grid_rows(app.renderer.buffer)[app.rows - 3]
        self.assertNotIn("copied 1 line", border)

    def test_completion_popup_navigates_with_arrows(self):
        # The @ / completion popup must be navigable with the arrow keys
        # and accepted with Enter - the keyboard path an operator uses.
        from core.console import ConsoleCompleter
        from core.scripted import final_response

        workspace = os.path.join(
            os.environ.get("TEMP", tempfile.gettempdir()),
            f"mantra-popup-{os.getpid()}-{threading.get_ident()}",
        )
        os.makedirs(workspace, exist_ok=True)
        _TEMP_WORKSPACES.append(workspace)
        for name in ("note.txt", "notes.md"):
            with open(os.path.join(workspace, name), "w", encoding="utf-8") as fh:
                fh.write("x\n")
        session = make_session(workspace, [final_response("hi")])
        app = TuiApp(session, backend=FakeBackend(cols=100, rows=24))
        app._init_surface()
        app._show_welcome()
        app.composer.completer = ConsoleCompleter(session)
        for ch in "@no":
            app.handle_event(Key(ch))
        self.assertTrue(app.composer.popup_open)
        self.assertEqual(app.composer.selected, 0)
        app.handle_event(Key("down"))
        self.assertEqual(app.composer.selected, 1)
        app.handle_event(Key("up"))
        self.assertEqual(app.composer.selected, 0)
        app.handle_event(Key("enter"))
        self.assertEqual(app.composer.buffer, "@note.txt")

    def test_busy_chip_shows_counter_during_a_streaming_turn(self):
        # The counter rides the live delta stream: a real streaming turn
        # must put "tok ·" and "tok/s" into the busy chip, not just a
        # preset counter_text.
        from core.scripted import LLMResponse, ScriptedLLMClient

        class _Slow(ScriptedLLMClient):
            def chat(self, messages, tools=None, on_delta=None):
                response = LLMResponse(content="one two three four five six seven eight")
                if on_delta:
                    for word in response.content.split(" "):
                        on_delta(word + " ")
                        time.sleep(0.03)
                return response

        app, session, backend = _make_app([_Slow([LLMResponse(content="x")])])
        app.submit("go")
        self.assertTrue(wait_until(lambda: app.counter_text != "", 5))
        app.render_frame()
        border = grid_rows(app.renderer.buffer)[app.rows - 3]
        self.assertIn("tok ·", border)
        self.assertIn("tok/s", border)
        wait_until(lambda: not app.busy, 10)

    def test_turn_end_does_not_yank_a_detached_scroll(self):
        # Regression: handle()'s tail force-scrolled the transcript to
        # the bottom at the end of every turn, so scrolling up while or
        # just after a task ran looked broken. A detached scroll must
        # survive the turn's end.
        from core.scripted import final_response

        app, session, backend = _make_app([final_response("done")])
        with app.lock:
            for i in range(40):
                app.transcript.append(f"line {i}")
            app.transcript.scroll_up(5)
            detached = app.transcript.offset
        self.assertGreater(detached, 0)
        self.assertFalse(session.layout.following)
        app.submit("go")
        wait_until(lambda: not app.busy, 10)
        app.render_frame()
        self.assertEqual(app.transcript.offset, detached)
        self.assertFalse(session.layout.following)

    def test_busy_chip_waits_for_real_tokens_before_showing_counter(self):
        # While the model thinks (no tokens yet) the busy chip must not
        # show fake "0 tok" values; the counter appears with the first
        # real streamed token.
        from core.scripted import LLMResponse, ScriptedLLMClient

        class _Thinking(ScriptedLLMClient):
            def chat(self, messages, tools=None, on_delta=None):
                time.sleep(0.6)  # model "thinks" before the first token
                response = LLMResponse(content="hello there world")
                if on_delta:
                    for word in response.content.split(" "):
                        on_delta(word + " ")
                        time.sleep(0.03)
                return response

        app, session, backend = _make_app([_Thinking([LLMResponse(content="x")])])
        app.submit("go")
        self.assertTrue(wait_until(lambda: app.busy, 5))
        time.sleep(0.2)  # still thinking: no tokens have streamed
        app.render_frame()
        border = grid_rows(app.renderer.buffer)[app.rows - 3]
        self.assertNotIn("tok", border)
        self.assertNotIn("0 tok", border)
        # Once tokens stream, the counter shows real numbers promptly.
        self.assertTrue(wait_until(lambda: app.counter_text != "", 5))
        app.render_frame()
        border = grid_rows(app.renderer.buffer)[app.rows - 3]
        self.assertIn("tok ·", border)
        self.assertIn("tok/s", border)
        wait_until(lambda: not app.busy, 10)

    def test_welcome_card_is_centered_and_follows_resizes(self):
        # The welcome card is drawn centered in the content area (not
        # top-left), re-centers when the window is resized, and is
        # replaced by the first real content.
        app, session, backend = _make_app([])
        app.render_frame()
        grid = grid_rows(app.renderer.buffer)
        row = next(r for r in grid if "M A N T R A" in r)
        self.assertGreater(len(row) - len(row.lstrip()), 0)  # horizontally centered
        self.assertGreater(grid.index(row), 2)               # not at the top
        app.handle_event(Resize(50, 16))
        wait_until(lambda: app.apply_pending_resize(), 2)
        app.render_frame()
        grid = grid_rows(app.renderer.buffer)
        row = next(r for r in grid if "M A N T R A" in r)
        self.assertGreater(len(row) - len(row.lstrip()), 0)
        app.feed_output("first real line\n")
        app.render_frame()
        joined = "\n".join(grid_rows(app.renderer.buffer))
        self.assertNotIn("M A N T R A", joined)

    def test_layout_bridge_reports_lines(self):
        app, session, backend = _make_app([])
        bridge = LayoutBridge(app)
        with app.lock:
            app.transcript.clear()
        app.feed_output("a\nb\n")
        self.assertEqual(bridge.lines, ["a", "b"])
        self.assertTrue(bridge.active)

    def test_ctrl_y_copies_selection_after_scroll(self):
        # Regression: _selection_text used overlay_rows(0, ...) while the
        # visible window starts at a non-zero display row once the
        # transcript overflows, so ctrl+y silently copied nothing.
        from core.scripted import final_response

        app, session, backend = _make_app([final_response("hi")])
        with app.lock:
            for i in range(40):
                app.transcript.append(f"filler line {i}")
            app.transcript.append("target alpha row")
            app.transcript.append("target beta row")
        app.render_frame()
        grid = grid_rows(app.renderer.buffer)
        top, height = app._content_top, app._content_height
        alpha_y = next(i for i, r in enumerate(grid) if "alpha" in r and top <= i < top + height)
        beta_y = next(i for i, r in enumerate(grid) if "beta" in r and top <= i < top + height)
        captured = []
        app_module.copy_text = lambda t: captured.append(t)
        app.handle_event(Mouse("press", 0, 0, alpha_y, frozenset()))
        app.handle_event(Mouse("drag", 0, 10, beta_y, frozenset()))
        app.render_frame()
        app.handle_event(Key("ctrl+y"))
        self.assertTrue(captured, msg="ctrl+y copied nothing")
        self.assertIn("alpha", captured[0])
        self.assertIn("beta", captured[0])


class ConPtyHarnessTest(unittest.TestCase):
    """The ConPTY harness: spawn a real child console process, reap it.

    The payload script writes a marker file, so the test asserts the
    child actually executed without depending on the console output pipe
    round-trip. Windows-only: the pseudoconsole API does not exist on
    POSIX.
    """

    def test_harness_spawns_and_reaps_a_child(self):
        if os.name != "nt":
            self.skipTest("ConPTY is Windows-only")
        from conpty_harness import make_console

        work = tempfile.mkdtemp(prefix="mantra-conpty-")
        self.addCleanup(shutil.rmtree, work, True)
        marker = os.path.join(work, "child-ran.txt")
        payload = os.path.join(work, "payload.py")
        with open(payload, "w", encoding="utf-8") as fh:
            fh.write(f"open({marker!r}, 'w').write('ran')\n")
        pty = make_console(cols=40, rows=10, args=[sys.executable, payload])
        self.assertGreater(pty.pid, 0)
        try:
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline and not os.path.isfile(marker):
                time.sleep(0.1)
            self.assertTrue(os.path.isfile(marker), "ConPTY child never ran")
        finally:
            pty.close()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
