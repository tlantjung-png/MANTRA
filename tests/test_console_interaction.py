"""Full interactive console simulation.

Drives the real CompactLayout (the streaming viewport) together with the
real LineEditor through the exact wiring ``repl()`` sets up — wheel and
PageUp/PageDown scroll the viewport, mouse drags copy to the clipboard,
Ctrl+V and bracketed paste insert — while content keeps streaming into
the layout the way an agent's reply does. This is a fake *terminal*
(scripted keys + captured writes), not a fake editor or layout, so the
assertions cover the observable contract of the interactive session:

- typing (read) returns the typed text,
- backspace/arrows/word jumps (write/edit) change the buffer,
- copy-paste works via mouse drag, Ctrl+V, and bracketed paste,
- wheel and keyboard scrolling move the viewport the same way,
- scrolling while content streams keeps the user's view in place.

Run from the project root:  python -m unittest discover -s tests -v
"""

from __future__ import annotations

import io
import os
import re
import sys
import unittest
from contextlib import contextmanager
from unittest import mock

# Bootstrap imports so the suite runs from a checkout without installation.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import mantra.compact as compact
import mantra.line_editor as line_editor
from mantra.line_editor import LineEditor, MouseEvent


_SGR_RE = re.compile(r"\033\[[0-9;]*m")


def _plain(text: str) -> str:
    """Strip SGR color codes so output assertions read the text."""
    return _SGR_RE.sub("", text)


class FakeTerm:
    """A scriptable terminal: captures everything written, reports a size."""

    def __init__(self, cols: int = 80, rows: int = 24) -> None:
        self.cols = cols
        self.rows = rows
        self.output = io.StringIO()

    def write(self, text: str) -> None:
        self.output.write(text)

    def flush(self) -> None:
        pass

    def isatty(self) -> bool:
        return True


class ScriptedEditor(LineEditor):
    """The real editor, with only the terminal touchpoints scripted."""

    def __init__(self, keys, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.keys = list(keys)

    @contextmanager
    def _raw_mode(self):
        yield

    def _read_key(self):
        if not self.keys:
            raise AssertionError("editor asked for a key but the script is empty")
        return self.keys.pop(0)


class _Style:
    def cyan(self, t):
        return f"\033[36m{t}\033[0m"

    def dim(self, t):
        return f"\033[2m{t}\033[0m"

    def bold(self, t):
        return f"\033[1m{t}\033[0m"

    def selected(self, t):
        return f"\033[1;38;5;131m{t}\033[0m"


class _NoCompleter:
    def complete(self, buffer, cursor):
        return None


class InteractionSimulation(unittest.TestCase):
    """The console under a scripted terminal, while content streams."""

    COLS, ROWS = 80, 24

    def setUp(self):
        self.term = FakeTerm(self.COLS, self.ROWS)
        self._stdout = mock.patch.object(sys, "stdout", self.term)
        self._stdout.start()
        self.addCleanup(self._stdout.stop)
        self._term_size = mock.patch(
            "mantra.compact._term_size", lambda: (self.COLS, self.ROWS)
        )
        self._term_size.start()
        self.addCleanup(self._term_size.stop)
        self._editor_size = mock.patch(
            "mantra.line_editor.term_size", lambda: (self.COLS, self.ROWS)
        )
        self._editor_size.start()
        self.addCleanup(self._editor_size.stop)
        self._tty = mock.patch.object(sys.stdin, "isatty", lambda: True)
        self._tty.start()
        self.addCleanup(self._tty.stop)

        self.layout = compact.CompactLayout()
        self.layout.setup(0)

    # ── harness helpers ────────────────────────────────────────

    def _run(self, keys, prompt="mantra> "):
        """Play ``keys`` then Enter; return (line, editor)."""
        editor = ScriptedEditor(
            list(keys) + ["\r"],
            _Style(),
            completer=_NoCompleter(),
            popup_above=True,
            on_page_up=lambda: self.layout.scroll_up(3),
            on_page_down=lambda: self.layout.scroll_down(3),
        )
        editor.fixed_row = self.layout.prompt_row
        editor.layout_ref = self.layout
        editor.viewport_getter = lambda: list(self.layout.lines)
        line = editor.read(prompt)
        return line, editor

    def _stream(self, count: int = 30, prefix: str = "line") -> None:
        """Simulate an agent streaming ``count`` lines into the viewport."""
        for i in range(count):
            self.layout.write(f"{prefix}-{i}\n")

    # ── read / write / edit ────────────────────────────────────

    def test_typing_returns_the_text(self):
        line, _ = self._run(list("fix the bug"))
        self.assertEqual(line, "fix the bug")

    def test_backspace_and_arrows_edit_the_buffer(self):
        line, _ = self._run(
            list("hello")
            + [line_editor.KEY_LEFT, line_editor.KEY_LEFT, "\x7f"]
            + list("p!")
        )
        # "hello", caret back to the third char, delete it, type "p!":
        # "he" + "p!" + "lo"
        self.assertEqual(line, "hep!lo")

    def test_word_jumps_and_home_end(self):
        line, _ = self._run(
            list("alpha beta gamma")
            + [line_editor.KEY_CTRL_LEFT, line_editor.KEY_CTRL_LEFT, "\x7f"]  # to "gamma", then "beta", delete the space before it
            + [line_editor.KEY_HOME, "!"]                                   # prepend
            + [line_editor.KEY_END, "?"]                                    # append
        )
        self.assertEqual(line, "!alphabeta gamma?")

    def test_shift_enter_inserts_newline(self):
        line, _ = self._run(
            list("a") + [line_editor.KEY_SHIFT_ENTER] + list("b")
        )
        self.assertEqual(line, "a\nb")

    # ── copy / paste ──────────────────────────────────────────

    def test_mouse_drag_on_prompt_copies_the_selection(self):
        copied: list[str] = []
        with mock.patch.object(line_editor, "_set_clipboard_text", copied.append):
            # Type "hello world", then drag on the prompt row. Prompt column
            # c maps to buffer index c - len("mantra> ") - 1, so "world"
            # (indexes 6..10) needs columns 15 and 20.
            keys = list("hello world")
            # MouseEvent(button, column, row, pressed).
            keys.append(MouseEvent(0, 15, self.layout.prompt_row, True))
            keys.append(MouseEvent(0, 20, self.layout.prompt_row, False))
            line, editor = self._run(keys)
        self.assertEqual(line, "hello world")
        self.assertTrue(copied, "drag must copy to the clipboard")
        self.assertEqual(copied[0], "world")

    def test_mouse_click_on_viewport_line_copies_it(self):
        copied: list[str] = []
        self._stream(5, prefix="chat")
        with mock.patch.object(line_editor, "_set_clipboard_text", copied.append):
            line, editor = self._run(
                [
                    MouseEvent(0, 3, self.layout.content_top + 2, True),
                    MouseEvent(0, 3, self.layout.content_top + 2, False),
                    "\r",
                ]
            )
        self.assertEqual(line, "")
        self.assertTrue(copied, "clicking a conversation line must copy it")
        self.assertEqual(copied[0], "chat-2")

    def test_ctrl_v_pastes_clipboard(self):
        with mock.patch.object(line_editor, "_get_clipboard_text", lambda: "pasted text"):
            line, _ = self._run(list("pre ") + ["\x16"] + list(" post"))
        self.assertEqual(line, "pre pasted text post")

    def test_bracketed_paste_inserts(self):
        # ESC[200~ / ESC[201~ wrap the pasted body.
        pasted = "\x1b[200~multi\nline paste\x1b[201~"
        line, _ = self._run(list("pre ") + [pasted] + list(" post"))
        self.assertEqual(line, "pre multi\nline paste post")

    def test_huge_multiline_paste_does_not_submit(self):
        """A paste far larger than the old 10k cap must arrive whole in
        the buffer - embedded newlines are text, not Enter - and only a
        real Enter afterwards submits the line."""
        body = "\n".join(f"{i:04d}: " + "word " * 40 for i in range(400))
        pasted = "\x1b[200~" + body + "\x1b[201~"
        # ``_run`` appends the real Enter: read() returns exactly when
        # that key arrives, with the entire body intact in the buffer.
        line, _ = self._run([pasted] + list("tail"))
        self.assertEqual(line, body + "tail")
        self.assertEqual(len(line), len(body) + len("tail"))

    def test_multiline_buffer_renders_an_expanding_box(self):
        """A multi-line buffer draws as a real box that grows upward: the
        top border (corners + size chip) rises above the content, every
        line sits on its own walled row, and the caret line stays on the
        prompt row - so a pasted block is readable and visibly enclosed."""
        pasted = "\x1b[200~alpha\nbeta\ngamma\x1b[201~"
        line, editor = self._run([pasted])
        self.assertEqual(line, "alpha\nbeta\ngamma")
        out = _plain(self.term.output.getvalue())
        # No collapse markers anywhere.
        self.assertNotIn(" ↵ ", out)
        prompt = self.layout.prompt_row  # 24 with the test term
        # The box top edge rises above the lines and carries the chip.
        self.assertIn(f"\033[{prompt - 3};1H\033[2K╭─ · 3 lines · 16 chars", out)
        # Each pasted line sits on its own walled row above the prompt.
        self.assertIn(f"\033[{prompt - 2};1H\033[2K│         alpha", out)
        self.assertIn(f"\033[{prompt - 1};1H\033[2K│         beta", out)
        self.assertIn(f"\033[{prompt};1H\033[2K│ mantra> gamma", out)

    def test_box_top_border_rises_with_more_pasted_lines(self):
        """The box truly expands: each pasted line adds a walled row and
        pushes the top border (corners + chip) one row higher, so a large
        paste is visibly enclosed instead of lines floating over content."""
        prompt = self.layout.prompt_row  # 24 with the test term

        # Two lines -> top border sits at prompt - 2.
        line, _ = self._run(["\x1b[200~a\nb\x1b[201~"])
        out = _plain(self.term.output.getvalue())
        self.assertEqual(line, "a\nb")
        self.assertIn(f"\033[{prompt - 2};1H\033[2K╭─", out)
        self.assertIn(f"\033[{prompt - 1};1H\033[2K│         a", out)
        self.assertIn(f"\033[{prompt};1H\033[2K│ mantra> b", out)

        # Five lines -> border rises two more rows to prompt - 5, with
        # every intermediate row walled.
        line, _ = self._run(["\x1b[200~l1\nl2\nl3\nl4\nl5\x1b[201~"])
        out = _plain(self.term.output.getvalue())
        self.assertEqual(line, "l1\nl2\nl3\nl4\nl5")
        self.assertIn(f"\033[{prompt - 5};1H\033[2K╭─", out)
        self.assertIn(f"\033[{prompt - 4};1H\033[2K│         l1", out)
        self.assertIn(f"\033[{prompt - 3};1H\033[2K│         l2", out)
        self.assertIn(f"\033[{prompt - 2};1H\033[2K│         l3", out)
        self.assertIn(f"\033[{prompt - 1};1H\033[2K│         l4", out)
        self.assertIn(f"\033[{prompt};1H\033[2K│ mantra> l5", out)

    def test_tall_multiline_buffer_clips_with_line_count_marker(self):
        """When a paste is taller than the viewport, the top row shows a
        dim '… N more above' plus the total line count, so a huge paste
        reports exactly how many lines entered the buffer."""
        body = "\n".join(f"line-{i:03d}" for i in range(30))
        pasted = "\x1b[200~" + body + "\x1b[201~"
        line, _ = self._run([pasted])
        self.assertEqual(line, body)
        self.assertEqual(line.count("\n") + 1, 30)
        out = _plain(self.term.output.getvalue())
        # Total line count is explicit.
        self.assertIn("· 30 lines", out)
        # The top of the clip names the hidden lines.
        self.assertIn("more above", out)
        # Lines beyond the clip are not drawn.
        self.assertNotIn("line-000", out)
        self.assertIn("line-029", out)

    def test_single_line_buffer_stays_on_the_input_row(self):
        """No newline in the buffer -> no box: nothing is drawn above the
        prompt row and no line-count chip appears."""
        line, editor = self._run(list("just one line"))
        self.assertEqual(line, "just one line")
        out = _plain(self.term.output.getvalue())
        self.assertNotIn("lines", out)
        self.assertNotIn("more above", out)
        self.assertNotIn(" ↵ ", out)

    def test_wide_single_line_paste_shows_size_chip(self):
        """A single very wide line (no newlines) is clipped horizontally,
        but a dim right-edge chip reports the line and character count so
        the truncation is never silent."""
        body = "word-" * 120  # ~600 chars, far wider than the prompt row
        pasted = "\x1b[200~" + body + "\x1b[201~"
        line, _ = self._run([pasted])
        self.assertEqual(line, body)
        out = _plain(self.term.output.getvalue())
        self.assertIn("· 1 line · 600 chars", out)

    def test_wide_cjk_line_clips_by_visible_width_not_char_count(self):
        """Double-width (CJK) input must be fitted and clipped by visible
        columns, not Python character count: 70 '你' are 70 chars but 140
        columns, so a len()-based fit check wrongly declares the line as
        fitting and draws far past the box edge. The buffer must stay
        intact and the drawn prompt row must never exceed the terminal
        width."""
        from mantra.line_editor import visible_len

        body = "你" * 70  # 70 chars, 140 visible columns
        pasted = "\x1b[200~" + body + "\x1b[201~"
        line, _ = self._run([pasted])
        self.assertEqual(line, body)
        out = _plain(self.term.output.getvalue())
        # The line was clipped (did not fit), so the size chip is shown.
        self.assertIn("· 1 line · 70 chars", out)
        # The final prompt-row write stays inside the terminal width:
        # measure the text right after the prompt label up to the first
        # non-SGR escape (the chip/wall cursor moves), all codes stripped.
        last = out.rfind("mantra> ")
        self.assertGreaterEqual(last, 0)
        tail = out[last + len("mantra> "):]
        m = re.match(r"([^\x1b]*(?:\x1b\[[0-9;]*m[^\x1b]*)*)", tail)
        self.assertIsNotNone(m)
        drawn_vis = visible_len("mantra> ") + visible_len(m.group(1))
        self.assertLessEqual(drawn_vis, self.COLS)

    def test_editor_draw_follows_layout_geometry_not_stale_fixed_row(self):
        """The layout owns geometry: even with a stale cached fixed_row,
        _draw must reposition onto the layout's prompt_row, so a resize
        repaint can never land the prompt at the wrong row."""
        editor = ScriptedEditor(
            ["\r"],
            _Style(),
            completer=_NoCompleter(),
            popup_above=True,
        )
        editor.layout_ref = self.layout
        # A stale cached row that no longer matches the layout.
        editor.fixed_row = self.layout.prompt_row - 1
        line = editor.read("mantra> ")
        self.assertEqual(line, "")
        out = _plain(self.term.output.getvalue())
        # The draw repositioned onto the layout's row, not the stale one.
        self.assertIn(f"\033[{self.layout.prompt_row};1H", out)

    def test_arrow_keys_move_between_buffer_lines_in_the_box(self):
        """While the multi-line prompt is open, Up/Down move the caret
        between the pasted lines (column-preserving) instead of scrolling
        the transcript."""
        pasted = "\x1b[200~aaa\nbb\nccccc\x1b[201~"
        # Up once from the end of 'ccccc', type X -> lands at end of 'bb'.
        line, _ = self._run([pasted, line_editor.KEY_UP, "X"])
        self.assertEqual(line, "aaa\nbbX\nccccc")
        # Up twice (col 5 -> clamped to 2 on 'bb' -> stays 2 on 'aaa'),
        # type Q, then Down back to 'bb' (col 2 -> end), type R.
        line, _ = self._run([pasted, line_editor.KEY_UP, line_editor.KEY_UP, "Q", line_editor.KEY_DOWN, "R"])
        self.assertEqual(line, "aaQa\nbbR\nccccc")

    def test_vertical_caret_keeps_column_and_clamps(self):
        """The line-movement helper preserves the caret column across
        lines of different lengths and stops at the first/last line."""
        editor = ScriptedEditor([], _Style(), completer=_NoCompleter())
        buffer = "abc\ndefgh\nij"
        # Caret at end of 'defgh' (col 5); up keeps col 5 clamped to 3 on
        # 'abc' -> its end.
        start = len("abc\n")
        cur = editor._vertical_caret(buffer, start + 5, -1)
        self.assertEqual(buffer[cur - 3:cur], "abc")  # col clamped to 3
        # Same column (3) going back down onto 'defgh' -> after 'def'.
        cur2 = editor._vertical_caret(buffer, cur, 1)
        self.assertEqual(buffer[cur2 - 3:cur2], "def")
        # Clamps at the edges.
        self.assertEqual(editor._vertical_caret(buffer, 0, -1), 0)
        self.assertEqual(editor._vertical_caret(buffer, len(buffer), 1), len(buffer))

    # ── the prompt sits inside a literal box ────────────────────

    def test_prompt_row_stays_closed_while_typing(self):
        """The bottom two rows are a box: corners on the border row and a
        right wall on the prompt row. Typing must not erase the wall - the
        editor reserves the last column and re-emits it on every draw, so
        long input can never escape the box."""
        line, _ = self._run(list("hello world"))
        self.assertEqual(line, "hello world")
        out = self.term.output.getvalue()
        # Border row carries the corners.
        self.assertIn("╭", out)
        self.assertIn("╮", out)
        # The prompt row is closed with a right wall on the last column.
        wall = self.layout.wall_glyph()
        self.assertTrue(wall, "boxed layout must expose the wall glyph")
        self.assertIn(f"\033[{self.COLS}G" + _plain(wall), _plain(out))

    def test_wall_column_survives_a_wide_single_line(self):
        """Even a single line wider than the screen keeps the box wall:
        the text window and size chip stop one column short and the wall
        is re-emitted at the last column."""
        body = "word-" * 120  # ~600 chars, far wider than the prompt row
        pasted = "\x1b[200~" + body + "\x1b[201~"
        line, _ = self._run([pasted])
        self.assertEqual(line, body)
        out = _plain(self.term.output.getvalue())
        self.assertIn("· 1 line · 600 chars", out)
        # Wall still present at the last column after the clip + chip.
        self.assertIn(f"\033[{self.COLS}G│", out)

    # ── scroll: keyboard ───────────────────────────────────────

    def test_page_up_down_scrolls_the_viewport(self):
        self._stream(40)
        self.assertEqual(self.layout.offset, 0)  # pinned at the newest line

        line, editor = self._run([line_editor.KEY_PAGE_UP])
        self.assertEqual(self.layout.offset, 3)  # scroll_up(3)
        line, editor = self._run([line_editor.KEY_PAGE_DOWN])
        self.assertEqual(self.layout.offset, 0)  # back to the bottom

    def test_arrow_keys_scroll_when_no_popup(self):
        self._stream(40)
        line, editor = self._run([line_editor.KEY_UP])
        self.assertEqual(self.layout.offset, 3)

    # ── scroll: mouse wheel ────────────────────────────────────

    def test_wheel_up_scrolls_the_viewport(self):
        self._stream(40)
        self.assertEqual(self.layout.offset, 0)
        line, editor = self._run([MouseEvent(64, 40, self.layout.prompt_row, True)])
        self.assertEqual(self.layout.offset, 3)

    def test_wheel_down_scrolls_back(self):
        self._stream(40)
        self._run([MouseEvent(64, 40, self.layout.prompt_row, True)])
        self.assertEqual(self.layout.offset, 3)
        line, editor = self._run([MouseEvent(65, 40, self.layout.prompt_row, True)])
        self.assertEqual(self.layout.offset, 0)

    def test_wheel_scroll_clamps_at_the_edges(self):
        self._stream(5)  # fewer lines than the viewport: nothing to scroll
        line, editor = self._run([MouseEvent(64, 40, self.layout.prompt_row, True)])
        self.assertEqual(self.layout.offset, 0)
        # Now push way past the top of history.
        self._stream(60, prefix="more")
        for _ in range(50):
            self._run([MouseEvent(64, 40, self.layout.prompt_row, True)])
        height = self.layout.content_bottom - self.layout.content_top + 1
        self.assertEqual(self.layout.offset, max(0, 65 - height))
        # And wheel down clamps at the bottom.
        for _ in range(50):
            self._run([MouseEvent(65, 40, self.layout.prompt_row, True)])
        self.assertEqual(self.layout.offset, 0)

    # ── streaming while scrolling ──────────────────────────────

    def test_streaming_keeps_the_scrolled_position(self):
        self._stream(30)
        line, editor = self._run([line_editor.KEY_PAGE_UP])
        self.assertEqual(self.layout.offset, 3)
        # The agent keeps answering: more lines arrive. The user's view must
        # not be yanked back to the bottom mid-scroll.
        self._stream(10, prefix="tail")
        self.assertEqual(self.layout.offset, 3)
        # New content is visible once the user scrolls back down.
        line, editor = self._run([line_editor.KEY_PAGE_DOWN])
        self.assertEqual(self.layout.offset, 0)

    def test_wheel_and_keyboard_scroll_interleave(self):
        self._stream(40)
        self._run([line_editor.KEY_PAGE_UP])
        self._run([MouseEvent(64, 40, self.layout.prompt_row, True)])
        self.assertEqual(self.layout.offset, 6)
        self._run([MouseEvent(65, 40, self.layout.prompt_row, True)])
        self._run([line_editor.KEY_PAGE_DOWN])
        self.assertEqual(self.layout.offset, 0)


class TurnScrollReaderTest(unittest.TestCase):
    """The background reader that scrolls while a task streams.

    This is the machinery ``handle()`` runs around ``loop.run``: SGR mouse
    reporting is enabled only for the turn, wheel (64/65) and the scroll
    keys move the viewport, and anything else is buffered into the
    session's preload list so the next prompt delivers it.
    """

    def _reader(self, session=None):
        from mantra.console import _TurnScrollReader

        class _FakeLayout:
            def __init__(self):
                self.offset = 0
                self.scrolls = []

            def scroll_up(self, amount=3):
                self.scrolls.append(("up", amount))
                self.offset += amount

            def scroll_down(self, amount=3):
                self.scrolls.append(("down", amount))
                self.offset -= amount

        layout = _FakeLayout()
        if session is None:
            session = mock.Mock()
            session._scroll_preload = []
        reader = _TurnScrollReader(session, layout)
        return reader, layout

    def test_wheel_up_scrolls_viewport(self):
        reader, layout = self._reader()
        reader._dispatch(MouseEvent(64, 40, 12, True))
        self.assertEqual(layout.scrolls, [("up", 3)])

    def test_wheel_down_scrolls_viewport(self):
        reader, layout = self._reader()
        reader._dispatch(MouseEvent(65, 40, 12, True))
        self.assertEqual(layout.scrolls, [("down", 3)])

    def test_page_keys_scroll_viewport(self):
        reader, layout = self._reader()
        reader._dispatch(line_editor.KEY_PAGE_UP)
        reader._dispatch(line_editor.KEY_UP)
        reader._dispatch(line_editor.KEY_PAGE_DOWN)
        reader._dispatch(line_editor.KEY_DOWN)
        self.assertEqual(layout.scrolls, [("up", 3), ("up", 3), ("down", 3), ("down", 3)])

    def test_other_keys_are_buffered_for_the_next_prompt(self):
        reader, layout = self._reader()
        reader._dispatch("h")
        reader._dispatch("i")
        reader._dispatch(line_editor.KEY_LEFT)
        reader._dispatch("\r")
        self.assertEqual(layout.scrolls, [])
        self.assertEqual(reader._session._scroll_preload, ["h", "i", line_editor.KEY_LEFT, "\r"])

    def test_clicks_are_not_buffered(self):
        reader, layout = self._reader()
        reader._dispatch(MouseEvent(0, 10, 12, True))   # press
        reader._dispatch(MouseEvent(0, 12, 12, False))  # release
        self.assertEqual(layout.scrolls, [])
        self.assertEqual(reader._session._scroll_preload, [])

    def test_mouse_is_never_captured_around_the_turn(self):
        """Native selection must keep working mid-stream: the reader
        writes nothing to the terminal on start or stop (no SGR mouse
        reporting), so the host's mouse handling is never disturbed."""
        reader, layout = self._reader()
        term = FakeTerm()
        # A mock thread: we are testing what the reader *writes*, not
        # that it reads keys, and an unstarted thread cannot be joined.
        reader._thread = mock.Mock()
        with mock.patch.object(sys, "stdout", term):
            reader.start()
            reader.stop()
        out = term.output.getvalue()
        self.assertEqual(out, "")
        self.assertNotIn("?1000", out)
        self.assertNotIn("?1006", out)
        self.assertNotIn("?1002", out)


class PreloadDrainTest(unittest.TestCase):
    """Keys buffered during a turn must reach the editor's next read."""

    def test_editor_delivers_preloaded_keys_first(self):
        editor = LineEditor(_Style())
        editor.preload = ["h", "i"]
        self.assertEqual(editor._read_key(), "h")
        self.assertEqual(editor._read_key(), "i")

    def test_paste_assembly_reads_whole_multiline_body(self):
        """A bracketed paste is collected whole - every line, in order -
        with no stray bytes left to hit the submit key."""
        from mantra.line_editor import _assemble_bracketed_paste

        body = "line one\nline two with spaces\n\nfinal\n"
        chars = iter(body + "\x1b[201~")
        pasted = _assemble_bracketed_paste(
            lambda: next(chars), lambda: True
        )
        self.assertEqual(pasted, body)

    def test_paste_assembly_keeps_everything_past_old_caps(self):
        """Large pastes must never lose content: a body far bigger than
        the old 10k cap comes through whole, ending exactly at the
        closing bracket."""
        from mantra.line_editor import _assemble_bracketed_paste

        body = ("y" * 200 + "\n") * 300  # ~60k chars, many lines
        chars = iter(body + "\x1b[201~")
        pasted = _assemble_bracketed_paste(lambda: next(chars), lambda: True)
        self.assertEqual(pasted, body)

    def test_paste_assembly_gives_up_after_idle(self):
        """A truncated transmission (no closing bracket) cannot hang the
        prompt: an idle gap ends the read."""
        from mantra.line_editor import _assemble_bracketed_paste

        body = "partial"
        state = {"left": list(body)}

        def read_char():
            if not state["left"]:
                return ""
            return state["left"].pop(0)

        def ready():
            return bool(state["left"])

        pasted = _assemble_bracketed_paste(read_char, ready, idle_grace=0.02)
        self.assertEqual(pasted, "partial")

    def test_read_loop_consumes_preload_before_terminal(self):
        class Preloaded(ScriptedEditor):
            """Consult the preload buffer before the scripted terminal,
            exactly like the real ``_read_key`` does."""

            def _read_key(self):
                if self.preload:
                    return self.preload.pop(0)
                return super()._read_key()

        editor = Preloaded(["\r"], _Style(), completer=_NoCompleter())
        editor.preload = list("hi")
        with mock.patch.object(sys.stdin, "isatty", lambda: True), mock.patch.object(
            sys.stdout, "isatty", lambda: True
        ):
            line = editor.read("mantra> ")
        self.assertEqual(line, "hi")


if __name__ == "__main__":
    unittest.main()