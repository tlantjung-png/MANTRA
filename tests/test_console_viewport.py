"""Regression tests for the console streaming refactor.

The audit found three things that made the TUI unusable on real tasks:

1. Long lines (markdown tables, audit output, diffs) were truncated at
   the right edge of the viewport instead of wrapping, so tool results
   were unreadable regardless of which tool produced them.
2. Scrolling during a stream did not work on POSIX because the terminal
   stayed in canonical mode — single keys and wheel sequences sat in
   the tty line buffer until Enter and never reached the turn reader.
3. File edits were invisible or dumped raw; now the operator sees a
   coloured before/after diff (edit_file) or a capped syntax preview
   (write_file) as the agent works.

These tests pin the layout half (wrapping / partial lines / resize /
scroll marker / snap-to-bottom) and the diff half (snapshot at
tool_call, real diff at tool_result).
"""

from __future__ import annotations

import builtins
import io
import os
import shutil
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import mantra.compact as compact
from mantra.compact import _ANSI_RE
import mantra.theme as theme
from mantra.console import ConsoleSession, Style


class FakeTerm:
    """A terminal stub: captures writes, reports a fixed size."""

    def __init__(self, cols: int, rows: int) -> None:
        self.cols = cols
        self.rows = rows
        self.output = io.StringIO()

    def write(self, text: str) -> None:
        self.output.write(text)

    def flush(self) -> None:
        pass

    def isatty(self) -> bool:
        return True


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


class LayoutWrapTest(unittest.TestCase):
    COLS, ROWS = 40, 20

    def setUp(self):
        self.term = FakeTerm(self.COLS, self.ROWS)
        self._stdout = mock.patch.object(sys, "stdout", self.term)
        self._stdout.start()
        self.addCleanup(self._stdout.stop)
        self._term_size = mock.patch(
            "mantra.compact._term_size", lambda: (self.term.cols, self.term.rows)
        )
        self._term_size.start()
        self.addCleanup(self._term_size.stop)
        self.layout = compact.CompactLayout()
        self.layout.setup(0)

    def test_long_styled_line_wraps_to_rows_within_width(self):
        styled = "\x1b[38;5;82m│ + def a_very_long_function_name(argument_one, argument_two):  # comment\n"
        self.layout.write(styled)
        self.layout.flush()
        self.assertEqual(self.layout.partial, "")
        self.assertGreater(len(self.layout.lines), 1, "long line must wrap into rows")
        for row in self.layout.lines:
            self.assertLessEqual(
                compact._vis(row), self.COLS,
                f"wrapped row overflows the viewport: {row!r}",
            )
        joined = _strip_ansi("".join(self.layout.lines))
        self.assertIn("a_very_long_function_name", joined)
        self.assertTrue(joined.endswith("comment"))

    def test_plain_long_line_wraps_and_content_survives(self):
        raw = "word " * 60
        self.layout.write(raw + "\n")
        self.layout.flush()
        joined = _strip_ansi("".join(self.layout.lines)).strip()
        self.assertEqual(len(self.layout.raw), 1)
        self.assertTrue(joined.startswith("word "))
        self.assertTrue(joined.endswith("word"))
        for row in self.layout.lines:
            self.assertLessEqual(compact._vis(row), self.COLS)

    def test_partial_line_is_wrapped_live_and_hidden_when_scrolled(self):
        # A still-growing line without a newline (the stream tail).
        for i in range(60):
            self.layout.write(f"line-{i}\n")
        self.layout.write("P" * 100)
        self.layout.flush()
        self.assertEqual(self.layout.partial, "P" * 100)
        visible = self.layout._rows_locked()
        for row in visible:
            self.assertLessEqual(compact._vis(row), self.COLS)
        self.assertIn("P", "".join(visible))
        # Scrolling up leaves the live tail out of the window entirely
        # (its row count keeps changing as it grows, which would make
        # offsets jump).
        self.layout.scroll_up(3)
        self.assertGreater(self.layout.offset, 0)
        visible2 = self.layout._rows_locked()
        self.assertNotIn("P", "".join(visible2))

    def test_partial_commits_into_wrapped_rows_on_newline(self):
        self.layout.write("Q" * 100)
        self.layout.flush()
        self.layout.write("\n")
        self.assertEqual(self.layout.partial, "")
        self.assertEqual(len(self.layout.raw), 1)
        rows = [compact._vis(r) for r in self.layout.lines]
        self.assertGreater(len(rows), 1)
        self.assertTrue(all(w <= self.COLS for w in rows))
        self.assertEqual(_strip_ansi("".join(self.layout.lines)), "Q" * 100)

    def test_scroll_to_bottom_snaps_and_clears_marker(self):
        for i in range(200):
            self.layout.write(f"line-{i}\n")
        self.layout.scroll_up(3)
        self.assertGreater(self.layout.offset, 0)
        self.layout.scroll_to_bottom()
        self.assertEqual(self.layout.offset, 0)
        self.assertEqual(self.layout._rows_locked()[-1].strip(), "line-199")

    def test_scroll_marker_shows_while_scrolled(self):
        for i in range(200):
            self.layout.write(f"line-{i}\n")
        self.term.output.seek(0)
        self.term.output.truncate(0)
        self.layout.scroll_up(3)
        self.assertGreater(self.layout.offset, 0)
        out = self.term.output.getvalue()
        self.assertIn("↑", out, "scroll marker must appear on the border row")

    def test_resize_rewraps_stored_lines(self):
        self.layout.write("R" * 100 + "\n")
        self.layout.write("S" * 100 + "\n")
        rows_at_40 = len(self.layout.lines)
        self.assertGreater(rows_at_40, 2)
        self.term.cols = 80
        self.term.rows = 20
        changed = self.layout.check_resize()
        self.assertTrue(changed)
        # Same logical lines, now wrapped wider: fewer rows, all within width.
        self.assertEqual(len(self.layout.raw), 2)
        self.assertLess(len(self.layout.lines), rows_at_40)
        for row in self.layout.lines:
            self.assertLessEqual(compact._vis(row), 80)
        joined = _strip_ansi("".join(self.layout.lines))
        self.assertEqual(joined.count("R"), 100)
        self.assertEqual(joined.count("S"), 100)

    def test_resize_syncs_prompt_renderer_in_same_step_as_recalc(self):
        """The layout is the single owner of geometry: check_resize must
        notify the attached prompt renderer (via prompt_sync) with the new
        prompt_row in the same locked step as the recalc, so a resize can
        never leave the editor drawing at a stale row."""
        from mantra.line_editor import LineEditor

        editor = LineEditor(None, popup_above=True)
        editor.fixed_row = self.layout.prompt_row
        editor.layout_ref = self.layout
        seen: list[int] = []

        def sync() -> None:
            seen.append(self.layout.prompt_row)
            editor.fixed_row = self.layout.prompt_row

        self.layout.prompt_sync = sync

        self.term.rows = 16
        changed = self.layout.check_resize()
        self.assertTrue(changed)
        self.assertEqual(self.layout.prompt_row, 16)
        # The renderer was told the new geometry during the recalc step.
        self.assertEqual(seen, [16])
        self.assertEqual(editor.fixed_row, 16)

    def test_no_prompt_sync_registered_resize_still_works(self):
        """prompt_sync is optional: a layout with no attached editor must
        resize exactly as before."""
        self.assertIsNone(self.layout.prompt_sync)
        self.term.rows = 18
        changed = self.layout.check_resize()
        self.assertTrue(changed)
        self.assertEqual(self.layout.prompt_row, 18)


class BoxChromeTest(unittest.TestCase):
    """The bottom two rows are a literal closed box around the prompt.

    Border row (rows-1) carries ╭ ╮ corners with the spinner / status
    text riding inside; the prompt row (the last screen row) is the box
    interior, closed by a right │ so input never escapes the box. The
    ↑ scroll marker also rides inside the border corners while scrolled.
    """

    COLS, ROWS = 60, 12

    def setUp(self):
        self.term = FakeTerm(self.COLS, self.ROWS)
        self._stdout = mock.patch.object(sys, "stdout", self.term)
        self._stdout.start()
        self.addCleanup(self._stdout.stop)
        self._term_size = mock.patch(
            "mantra.compact._term_size", lambda: (self.term.cols, self.term.rows)
        )
        self._term_size.start()
        self.addCleanup(self._term_size.stop)
        self.layout = compact.CompactLayout()
        self.layout.setup(0)

    def _row(self, row: int) -> str:
        """Plain-text content of an absolute screen row.

        Replays the captured writes through a tiny cursor-tracking grid
        (positions, absolute-column moves, erase-to-end-of-line) so ANSI
        style codes inside a row do not confuse the extraction.
        """
        import re
        cols, rows = self.COLS, self.ROWS
        grid = [[" "] * cols for _ in range(rows)]
        cur_r = cur_c = 0
        tokens = re.findall(r"\x1b\[[0-9;?]*[a-zA-Z]|[^\x1b]+", self.term.output.getvalue())
        for tok in tokens:
            if tok.startswith("\x1b["):
                m = re.match(r"\x1b\[([0-9;?]*)([a-zA-Z])", tok)
                body, cmd = m.group(1), m.group(2)
                if cmd == "H":
                    if ";" in body:
                        r, c = body.split(";")
                        cur_r, cur_c = int(r) - 1, int(c) - 1
                    else:
                        cur_r, cur_c = max(0, int(body or "1") - 1), 0
                elif cmd == "G":
                    cur_c = int(body) - 1
                elif cmd == "K" and body == "2":
                    for x in range(cur_c, cols):
                        grid[cur_r][x] = " "
                continue
            for ch in tok:
                if 0 <= cur_r < rows and 0 <= cur_c < cols:
                    grid[cur_r][cur_c] = ch
                cur_c += 1
        return "".join(grid[row - 1]).rstrip()

    def _border(self) -> str:
        """The plain-text content of the border row (rows-1)."""
        return self._row(self.layout.border_row)

    def _prompt(self) -> str:
        """The plain-text content of the prompt row (rows)."""
        return self._row(self.layout.prompt_row)

    def test_border_row_has_corners_when_idle(self):
        border = self._border()
        self.assertTrue(border.startswith("╭"), f"border must open with a corner: {border!r}")
        self.assertTrue(border.rstrip().endswith("╮"), f"border must close with a corner: {border!r}")

    def test_status_text_rides_inside_the_border_corners(self):
        self.layout.draw_border_status("⠋ Chanting 12s")
        border = self._border()
        self.assertIn("Chanting 12s", border)
        self.assertIn("⠋", border)
        # Corners still wrap the status text - it is inside the box, not
        # a bare line replacing the border.
        self.assertTrue(border.lstrip().startswith("╭"), border)
        self.assertTrue(border.rstrip().endswith("╮"), border)

    def test_prompt_row_is_closed_by_a_right_wall(self):
        self.layout.draw_prompt("")
        prompt = self._prompt()
        self.assertIn("│ MANTRA >", prompt)
        self.assertTrue(prompt.rstrip().endswith("│"), f"prompt row must close with the wall: {prompt!r}")
        # The interior is padded, so the wall sits on the very last column.
        self.assertEqual(len(prompt.rstrip()), self.COLS, "box interior + wall must span the full width")

    def test_scroll_marker_rides_inside_the_border_corners(self):
        for i in range(200):
            self.layout.write(f"line-{i}\n")
        self.term.output.seek(0)
        self.term.output.truncate(0)
        self.layout.scroll_up(3)
        border = self._border()
        self.assertIn("↑", border)
        # Marker sits before the right corner, still inside the box.
        self.assertLess(border.index("↑"), border.rindex("╮"))

    def test_top_info_bar_is_above_the_content(self):
        out = _strip_ansi(self.term.output.getvalue())
        info = out.split("\n")[0]
        self.assertIn("WORKSPACE", info)
        self.assertIn("MODEL", info)


class TurnScrollReaderDispatchTest(unittest.TestCase):
    """The mid-turn reader maps scroll keys / wheel / Ctrl+C correctly."""

    COLS, ROWS = 60, 20

    def setUp(self):
        self.term = FakeTerm(self.COLS, self.ROWS)
        self._stdout = mock.patch.object(sys, "stdout", self.term)
        self._stdout.start()
        self.addCleanup(self._stdout.stop)
        self._term_size = mock.patch(
            "mantra.compact._term_size", lambda: (self.term.cols, self.term.rows)
        )
        self._term_size.start()
        self.addCleanup(self._term_size.stop)
        from mantra.console import _TurnScrollReader
        from mantra.line_editor import MouseEvent, KEY_PAGE_UP, KEY_PAGE_DOWN, KEY_UP, KEY_DOWN, KEY_RESIZE

        self.MouseEvent = MouseEvent
        self.KEY_PAGE_UP, self.KEY_PAGE_DOWN = KEY_PAGE_UP, KEY_PAGE_DOWN
        self.KEY_UP, self.KEY_DOWN, self.KEY_RESIZE = KEY_UP, KEY_DOWN, KEY_RESIZE

        self.layout = compact.CompactLayout()
        self.layout.setup(0)
        for i in range(200):
            self.layout.write(f"line-{i}\n")

        self.session = mock.Mock()
        self.session._abort = threading.Event()
        self.session._scroll_preload = []
        self.reader = _TurnScrollReader.__new__(_TurnScrollReader)
        # Bypass __init__ so only _dispatch is exercised.
        self.reader._session = self.session
        self.reader._layout = self.layout

    def test_page_keys_scroll(self):
        self.reader._dispatch(self.KEY_PAGE_UP)
        self.assertGreater(self.layout.offset, 0)
        self.reader._dispatch(self.KEY_PAGE_DOWN)
        self.assertEqual(self.layout.offset, 0)

    def test_wheel_scrolls_like_page_keys(self):
        up = self.MouseEvent(64, 10, 12, True)   # wheel up (SGR)
        down = self.MouseEvent(65, 10, 12, True)  # wheel down (SGR)
        self.reader._dispatch(up)
        self.assertGreater(self.layout.offset, 0)
        self.reader._dispatch(down)
        self.assertEqual(self.layout.offset, 0)

    def test_regular_keys_are_preloaded_not_lost(self):
        self.reader._dispatch("h")
        self.reader._dispatch("i")
        self.assertEqual(self.session._scroll_preload, ["h", "i"])

    def test_ctrl_c_sets_abort_without_buffering(self):
        self.reader._dispatch("\x03")
        self.assertTrue(self.session._abort.is_set())
        self.assertEqual(self.session._scroll_preload, [])

    def test_ctrl_o_toggles_output_boxes_not_preload(self):
        self.reader._dispatch("\x0f")
        self.session.toggle_tool_output.assert_called_once()
        self.assertEqual(self.session._scroll_preload, [])

    def test_windows_console_vk_and_wheel_decoding(self):
        from mantra.console import _win_vk_to_key, _win_wheel_delta

        # VK codes: 0x21/0x22 PageUp/PageDown, 0x26/0x28 Up/Down.
        self.assertEqual(_win_vk_to_key(0x21), self.KEY_PAGE_UP)
        self.assertEqual(_win_vk_to_key(0x22), self.KEY_PAGE_DOWN)
        self.assertEqual(_win_vk_to_key(0x26), self.KEY_UP)
        self.assertEqual(_win_vk_to_key(0x28), self.KEY_DOWN)
        self.assertIsNone(_win_vk_to_key(0x41))  # plain 'A' - not a scroll key
        # Wheel deltas: positive = up, negative = down (int16 in the high word).
        self.assertEqual(_win_wheel_delta(0x00780000), 120)
        self.assertEqual(_win_wheel_delta(0xFF880000), -120)
        self.assertEqual(_win_wheel_delta(0x00000000), 0)

    def test_resize_key_reflows_layout(self):
        with mock.patch.object(self.layout, "check_resize", wraps=self.layout.check_resize) as chk:
            self.reader._dispatch(self.KEY_RESIZE)
            chk.assert_called_once()


class ToolOutputDisplayTest(unittest.TestCase):
    """run_command output and read_file content show up in the transcript."""

    def setUp(self):
        from test_console_session import make_session

        self.workspace = tempfile.mkdtemp(prefix="mantra-toolout-")
        self._make_session = make_session

    def _session(self):
        return self._make_session(self.workspace, [])

    def _silence(self):
        return mock.patch.object(sys, "stdout", io.StringIO())

    def test_command_observation_is_boxed_with_exit_code(self):
        session = self._session()
        session._last_command = 'python -c "print(1)"'
        obs = (
            "exit_code: 0\n"
            "stdout:\n"
            "<<<UNTRUSTED_TASK_OUTPUT\n"
            "hello from cmd\n"
            ">>>\n"
        )
        shown = session._render_command_result(obs)
        self.assertIn("┌ $ python -c", shown)
        self.assertIn("│ exit_code: 0", shown)
        self.assertIn("│ hello from cmd", shown)
        # Fencing markers are harness noise, not for the operator.
        self.assertNotIn("UNTRUSTED_TASK_OUTPUT", shown)

    def test_failing_command_exit_line_is_visible(self):
        session = self._session()
        shown = session._render_command_result("exit_code: 1\nstdout:\nboom\n")
        self.assertIn("│ exit_code: 1", shown)
        self.assertIn("│ boom", shown)

    def test_read_observation_is_boxed_with_path_title(self):
        session = self._session()
        session._last_read_path = "greet.py"
        shown = session._render_read_result("def greet():\n    return 1\n")
        self.assertIn("┌ read greet.py", shown)
        self.assertIn("│ def greet():", shown)
        self.assertIn("│     return 1", shown)

    def test_long_command_output_is_tail_capped(self):
        session = self._session()
        obs = "exit_code: 0\nstdout:\n" + "\n".join(f"line-{i}" for i in range(300))
        shown = session._render_command_result(obs)
        self.assertIn("more lines", shown)
        self.assertIn("│ exit_code: 0", shown)
        self.assertIn("│ stdout:", shown)
        self.assertIn("line-0", shown)
        # The cut content is queued: paging reaches the real tail.
        self.assertEqual(len(session._pending_pages), 1)
        buf = io.StringIO()
        with mock.patch.object(sys, "stdout", buf):
            guard = 0
            while session.page_next() and guard < 100:
                guard += 1
        self.assertEqual(session._pending_pages, [])
        self.assertIn("line-299", buf.getvalue())

    def test_capped_git_diff_and_shell_output_are_paged_too(self):
        session = self._session()
        big = "\n".join(f"chunk-{i}" for i in range(800))
        shown = session._render_command_result(
            "exit_code: 0\nstdout:\n" + big, kind="git diff"
        )
        self.assertIn("more lines", shown)
        self.assertEqual(len(session._pending_pages), 1)
        self.assertEqual(session._pending_pages[0][0], "git diff")
        session._last_shell_task = "tsk_0009"
        shell = session._render_shell_output(
            "<<<UNTRUSTED_TASK_OUTPUT\n" + big + "\n>>>\nnext_offset: 99"
        )
        self.assertIn("more lines", shell)
        self.assertEqual(len(session._pending_pages), 2)
        self.assertEqual(session._pending_pages[1][0], "shell_output tsk_0009")
        buf = io.StringIO()
        with mock.patch.object(sys, "stdout", buf):
            guard = 0
            while session.page_next() and guard < 200:
                guard += 1
        self.assertEqual(session._pending_pages, [])
        text = buf.getvalue()
        self.assertIn("(continued)", text)
        self.assertIn("chunk-799", text)
        self.assertIn("next_offset", text)

    def test_huge_read_is_capped_by_viewport_rows(self):
        session = self._session()
        session._last_read_path = "big.txt"
        body = "\n".join(f"line-{i}" for i in range(5000))
        shown = session._render_read_result(body)
        self.assertIn("┌ read big.txt", shown)
        self.assertIn("│ line-0", shown)
        self.assertIn("more lines", shown)
        self.assertNotIn("│ line-4999", shown)
        # The overflow content is queued for empty-Enter paging.
        self.assertEqual(len(session._pending_pages), 1)
        self.assertEqual(session._pending_pages[0][0], "read big.txt")
        # Very wide lines consume the row budget fast (wrapping).
        wide = "\n".join("w" * 400 for _ in range(200))
        wide_shown = session._render_read_result(wide)
        self.assertIn("more lines", wide_shown)

    def test_empty_enter_pages_through_queued_read(self):
        session = self._session()
        session._last_read_path = "big.txt"
        body = "\n".join(f"line-{i}" for i in range(600))
        shown = session._render_read_result(body)
        self.assertIn("more lines", shown)
        self.assertEqual(len(session._pending_pages), 1)
        # Page 1 continues where the preview stopped: no duplicated lines.
        buf = io.StringIO()
        with mock.patch.object(sys, "stdout", buf):
            guard = 0
            while session.page_next() and guard < 100:
                guard += 1
        text = buf.getvalue()
        self.assertIn("read big.txt (continued)", text)
        self.assertIn("line-599", text)
        # Queued content fully drained, page boxes never duplicated.
        self.assertEqual(session._pending_pages, [])
        self.assertLessEqual(text.count("(continued)"), 30)

    def test_toggle_flips_flag_and_prints_feedback(self):
        session = self._session()
        self.assertTrue(session._show_tool_output)
        with self._silence() as buf:
            session.toggle_tool_output()
        self.assertFalse(session._show_tool_output)
        self.assertIn("tool output boxes off", buf.getvalue())
        session.toggle_tool_output()
        self.assertTrue(session._show_tool_output)

    def test_observations_skipped_when_toggled_off(self):
        session = self._session()
        with mock.patch.object(session, "_render_command_result", wraps=session._render_command_result) as render:
            session._on_tool_observation("run_command", "exit_code: 0\nstdout:\nhi\n", 1)
            render.assert_called_once()
        session._show_tool_output = False
        with mock.patch.object(session, "_render_command_result", wraps=session._render_command_result) as render2:
            session._on_tool_observation("run_command", "exit_code: 0\nstdout:\nhi\n", 1)
            render2.assert_not_called()

    def test_git_diff_and_shell_output_render_inline(self):
        session = self._session()
        shown = session._render_command_result(
            "exit_code: 0\nstdout:\n<<<UNTRUSTED_TASK_OUTPUT\n"
            "diff --git a/x.py b/x.py\n-old line\n+new line\n>>>\n",
            kind="git diff",
        )
        self.assertIn("┌ git diff", shown)
        self.assertIn("│ -old line", shown)
        self.assertIn("│ +new line", shown)
        session._last_shell_task = "tsk_0001"
        shell = session._render_shell_output(
            "<<<UNTRUSTED_TASK_OUTPUT\nline a\nline b\n>>>\n"
            "next_offset: 42 (use from_offset=42 next)"
        )
        self.assertIn("┌ shell_output tsk_0001", shell)
        self.assertIn("│ line a", shell)
        self.assertIn("next_offset", shell)

    def test_full_turn_shows_command_and_read_inline(self):
        from mantra.implementations.llm.mock_client import (
            final_response,
            tool_call_response,
        )

        with open(os.path.join(self.workspace, "note.txt"), "w", encoding="utf-8") as fh:
            fh.write("first line\nsecond line\n")
        command = f'"{sys.executable}" -c "print(\'hello from cmd\')"'
        session = self._make_session(
            self.workspace,
            [
                tool_call_response("read_file", {"path": "note.txt"}),
                tool_call_response("run_command", {"command": command}),
                final_response("done reading"),
            ],
        )
        with self._silence() as buf:
            result = session.handle("show me the note and run a check")
        self.assertIsNotNone(result)
        text = buf.getvalue()
        self.assertIn("┌ read note.txt", text)
        self.assertIn("│ first line", text)
        self.assertIn("hello from cmd", text)
        self.assertIn("│ exit_code: 0", text)


class SessionPreferencePersistenceTest(unittest.TestCase):
    """Ctrl+O visibility preference survives save/load and autosave/resume."""

    def setUp(self):
        from test_console_session import make_session
        import mantra.core.sessions as sessions

        self.workspace = tempfile.mkdtemp(prefix="mantra-pref-")
        self._make_session = make_session
        self.sessions = sessions
        self._store = tempfile.mkdtemp(prefix="mantra-sessions-")
        # Redirect the session store so autosave never touches the real one.
        self._dir_patch = mock.patch.object(
            self.sessions,
            "sessions_dir",
            lambda: __import__("pathlib").Path(self._store),
        )
        self._dir_patch.start()
        self.addCleanup(self._dir_patch.stop)

    def test_save_load_roundtrip_keeps_preference(self):
        from mantra.implementations.llm.mock_client import final_response

        path = os.path.join(self.workspace, "pref-session.json")
        session = self._make_session(
            self.workspace,
            [final_response("hello, world")],
        )
        with mock.patch.object(sys, "stdout", io.StringIO()):
            session.handle("say hello")
            session._show_tool_output = False
            self.assertTrue(session.save_session(path))
        fresh = self._make_session(self.workspace, [])
        self.assertTrue(fresh._show_tool_output)
        with mock.patch.object(sys, "stdout", io.StringIO()):
            self.assertTrue(fresh.load_session(path))
        self.assertFalse(fresh._show_tool_output)

    def test_autosave_resume_roundtrip_keeps_preference(self):
        from mantra.implementations.llm.mock_client import final_response

        session = self._make_session(
            self.workspace,
            [final_response("hello, world")],
        )
        with mock.patch.object(sys, "stdout", io.StringIO()):
            session.handle("say hello")
            session._show_tool_output = False
            session.autosave()
        name = session.session_name
        self.assertTrue(name)
        fresh = self._make_session(self.workspace, [])
        self.assertTrue(fresh._show_tool_output)
        with mock.patch.object(sys, "stdout", io.StringIO()):
            self.assertTrue(fresh.resume_session(name))
        self.assertFalse(fresh._show_tool_output)

    def test_save_load_roundtrip_carries_pager_queue(self):
        from mantra.implementations.llm.mock_client import final_response

        path = os.path.join(self.workspace, "paged-session.json")
        session = self._make_session(self.workspace, [final_response("hello")])
        with mock.patch.object(sys, "stdout", io.StringIO()):
            session.handle("say hello")
        session._pending_pages.append(("read big.txt", [f"tail-{i}" for i in range(60)]))
        with mock.patch.object(sys, "stdout", io.StringIO()):
            self.assertTrue(session.save_session(path))
        fresh = self._make_session(self.workspace, [])
        self.assertEqual(fresh._pending_pages, [])
        buf = io.StringIO()
        with mock.patch.object(sys, "stdout", buf):
            self.assertTrue(fresh.load_session(path))
        # Queue came back with content and a hint so paging is discoverable.
        self.assertEqual(len(fresh._pending_pages), 1)
        self.assertEqual(fresh._pending_pages[0][0], "read big.txt")
        self.assertEqual(len(fresh._pending_pages[0][1]), 60)
        self.assertIn("press Enter with an empty prompt to page", buf.getvalue())
        # And the pager drains the restored queue normally.
        with mock.patch.object(sys, "stdout", io.StringIO()):
            guard = 0
            while fresh.page_next() and guard < 100:
                guard += 1
        self.assertEqual(fresh._pending_pages, [])

    def test_autosave_resume_carries_pager_queue(self):
        from mantra.implementations.llm.mock_client import final_response

        session = self._make_session(self.workspace, [final_response("hello")])
        with mock.patch.object(sys, "stdout", io.StringIO()):
            session.handle("say hello")
        session._pending_pages.append(
            ("git diff", ["+added", "-removed"] + [f"hunk-{i}" for i in range(40)])
        )
        with mock.patch.object(sys, "stdout", io.StringIO()):
            session.autosave()
        name = session.session_name
        self.assertTrue(name)
        fresh = self._make_session(self.workspace, [])
        self.assertEqual(fresh._pending_pages, [])
        buf = io.StringIO()
        with mock.patch.object(sys, "stdout", buf):
            self.assertTrue(fresh.resume_session(name))
        self.assertEqual(len(fresh._pending_pages), 1)
        self.assertEqual(fresh._pending_pages[0][0], "git diff")
        self.assertEqual(fresh._pending_pages[0][1][0], "+added")
        self.assertIn("interrupted run remain", buf.getvalue())
        self.assertIn("press Enter with an empty prompt to page", buf.getvalue())

    def _fake_list(self):
        return [
            {
                "name": "open-session",
                "workspace": self.workspace,
                "model": "mock",
                "turns": 2,
                "saved_at": "2026-09-01 10:00",
                "summary": "boxes visible",
            },
            {
                "name": "hidden-session",
                "workspace": self.workspace,
                "model": "mock",
                "turns": 1,
                "saved_at": "2026-09-01 11:00",
                "summary": "boxes hidden",
                "show_tool_output": False,
            },
        ]

    def test_show_sessions_marks_hidden_output_sessions(self):
        session = self._make_session(self.workspace, [])
        buf = io.StringIO()
        with mock.patch.object(self.sessions, "list_sessions", self._fake_list):
            with mock.patch.object(sys, "stdout", buf):
                session.show_sessions()
        text = buf.getvalue()
        self.assertIn("open-session", text)
        self.assertIn("hidden-session", text)
        # Marker appears on the hidden session's detail row, not the open one's.
        lines = text.splitlines()
        open_detail = lines[lines.index("  open-session") + 1]
        hidden_detail = lines[lines.index("  hidden-session") + 1]
        self.assertNotIn("output boxes hidden", open_detail)
        self.assertIn("output boxes hidden", hidden_detail)

    def test_pick_session_menu_marks_hidden_output_sessions(self):
        session = self._make_session(self.workspace, [])
        captured = {}

        def fake_menu(owner, title, options, hint=""):
            captured["options"] = options
            return None

        import mantra.console as console_mod

        with mock.patch.object(self.sessions, "list_sessions", self._fake_list):
            with mock.patch.object(console_mod, "_menu", fake_menu):
                with mock.patch.object(sys, "stdout", io.StringIO()):
                    session.pick_session()
        by_name = {opt.value: opt.hint for opt in captured["options"]}
        self.assertNotIn("output boxes hidden", by_name["open-session"])
        self.assertIn("output boxes hidden", by_name["hidden-session"])


class FileChangeDiffTest(unittest.TestCase):
    """Snapshots taken at tool_call become real before/after diffs."""

    def setUp(self):
        from test_console_session import make_session

        self.workspace = tempfile.mkdtemp(prefix="mantra-viewport-")
        self._make_session = make_session

    def _session(self):
        return self._make_session(self.workspace, [])

    def _silence(self):
        return mock.patch.object(sys, "stdout", io.StringIO())

    def test_edit_shows_before_after_diff(self):
        old = "def greet():\n    return 1\n"
        with open(os.path.join(self.workspace, "a.py"), "w", encoding="utf-8") as fh:
            fh.write(old)
        session = self._session()
        with self._silence():
            session._on_event(
                "tool_call",
                {"step": 1, "tool": "edit_file", "args": {"path": "a.py"}},
            )
        self.assertEqual(session._last_edit_path, "a.py")
        self.assertEqual(session._edit_snapshots["a.py"], old)
        with open(os.path.join(self.workspace, "a.py"), "w", encoding="utf-8") as fh:
            fh.write("def greet():\n    return 2\n")
        shown = session._render_file_change("edit_file", "OK edited a.py")
        stripped = _strip_ansi(shown)
        self.assertIn("✎ edited a.py", shown)
        # The diff is shown as old/new panes: the before-state holds the
        # removed line, the after-state the added line (no +/- prefixes).
        self.assertIn("old", stripped)
        self.assertIn("new", stripped)
        self.assertIn("    return 1", stripped)
        self.assertIn("    return 2", stripped)

    def test_write_of_new_file_gives_preview(self):
        session = self._session()
        with self._silence():
            session._on_event(
                "tool_call",
                {"step": 1, "tool": "write_file", "args": {"path": "new.txt"}},
            )
        self.assertIsNone(session._edit_snapshots["new.txt"])
        with open(os.path.join(self.workspace, "new.txt"), "w", encoding="utf-8") as fh:
            fh.write("hello world\n")
        shown = session._render_file_change("write_file", "OK wrote new.txt")
        self.assertIn("✓ wrote new.txt", shown)
        self.assertIn("hello world", _strip_ansi(shown))

    def test_overwrite_write_shows_diff(self):
        with open(os.path.join(self.workspace, "b.py"), "w", encoding="utf-8") as fh:
            fh.write("x = 1\n")
        session = self._session()
        with self._silence():
            session._on_event(
                "tool_call",
                {"step": 1, "tool": "write_file", "args": {"path": "b.py"}},
            )
        with open(os.path.join(self.workspace, "b.py"), "w", encoding="utf-8") as fh:
            fh.write("x = 2\n")
        shown = session._render_file_change("write_file", "OK wrote b.py")
        stripped = _strip_ansi(shown)
        self.assertIn("old", stripped)
        self.assertIn("new", stripped)
        self.assertIn("x = 1", stripped)
        self.assertIn("x = 2", stripped)

    def test_snapshots_cleared_at_end_of_turn(self):
        from mantra.implementations.llm.mock_client import (
            ScriptedLLMClient,
            final_response,
            tool_call_response,
        )

        with open(os.path.join(self.workspace, "a.py"), "w", encoding="utf-8") as fh:
            fh.write("def greet():\n    return 1\n")
        session = self._make_session(
            self.workspace,
            [
                tool_call_response("read_file", {"path": "a.py"}),
                tool_call_response(
                    "edit_file",
                    {"path": "a.py", "old_string": "return 1", "new_string": "return 2"},
                ),
                final_response("fixed"),
            ],
        )
        with self._silence():
            result = session.handle("make it return 2")
        self.assertIsNotNone(result)
        self.assertEqual(session._last_edit_path, None)
        self.assertEqual(session._edit_snapshots, {})
        with open(os.path.join(self.workspace, "a.py"), encoding="utf-8") as fh:
            self.assertIn("return 2", fh.read())


class PagerEndToEndTest(unittest.TestCase):
    """The exact operator flow, driven through the real REPL.

    A real interactive terminal cannot be driven from this sandbox, so the
    full loop is exercised end-to-end through ``repl()`` instead: a turn
    whose first tool result overflows a screenful (queued for paging), a
    mid-run Ctrl+O (the same toggle the turn-scroll reader dispatch calls)
    hiding every later box, and empty-Enter presses after the turn paging
    through the capped read - one screenful per press.
    """

    def setUp(self):
        from test_console_session import make_session
        import mantra.core.sessions as sessions

        self.workspace = tempfile.mkdtemp(prefix="mantra-pager-e2e-")
        self._make_session = make_session
        # Real turns autosave at the end; keep that out of the real
        # store even when this file is run directly (pytest already
        # redirects via the root conftest).
        self._store = tempfile.mkdtemp(prefix="mantra-pager-store-")
        self._dir_patch = mock.patch.object(
            sessions,
            "sessions_dir",
            lambda: __import__("pathlib").Path(self._store),
        )
        self._dir_patch.start()
        self.addCleanup(self._dir_patch.stop)
        self.addCleanup(shutil.rmtree, self._store, True)

    def _run_repl(self, session, lines):
        from mantra.console import Style, repl

        it = iter(lines)

        def _read(prompt=""):
            return next(it)

        buf = io.StringIO()
        with mock.patch.object(sys, "stdout", buf):
            repl(session, Style(enabled=False), reader=_read)
        return buf.getvalue()

    def test_ctrl_o_mid_run_then_empty_enter_pages_the_capped_read(self):
        from mantra.implementations.llm.mock_client import (
            final_response,
            tool_call_response,
        )

        body = "\n".join(f"line-{i:04d}" for i in range(150)) + "\n"
        with open(os.path.join(self.workspace, "big.py"), "w", encoding="utf-8") as fh:
            fh.write(body)
        session = self._make_session(
            self.workspace,
            [
                tool_call_response("read_file", {"path": "big.py"}),
                tool_call_response(
                    "run_command",
                    {"command": f'"{sys.executable}" -c "print(\'second-box-hidden\')"'},
                ),
                final_response("done paging"),
            ],
        )
        # The real turn-scroll reader dispatch maps Ctrl+O to
        # toggle_tool_output() while the turn streams. Simulate the key
        # arriving right after the first box is drawn.
        fired = []
        orig = session._on_tool_observation

        def hooked(tool, observation, step):
            out = orig(tool, observation, step)
            if not fired:
                fired.append(True)
                session.toggle_tool_output()
            return out

        session._on_tool_observation = hooked
        # Over-supply empty Enters: extra presses after the queue drains
        # are no-ops, so the drain always completes before /exit.
        text = self._run_repl(session, ["read and run", "", "", "", "", "", "", "", "/exit"])
        # The first read box was drawn while boxes were on, capped and queued.
        self.assertIn("┌ read big.py", text)
        self.assertIn("press Enter (empty prompt) to page", text)
        # Mid-run toggle feedback landed in the transcript.
        self.assertIn("tool output boxes off", text)
        # The later run_command box stayed hidden: only STEP rows printed.
        self.assertNotIn("second-box-hidden", text)
        # End-of-turn hint told the operator how to page.
        self.assertIn("page through it", text)
        # Empty-Enter paging drained the whole read through the real REPL.
        self.assertIn("read big.py (continued)", text)
        self.assertIn("line-0149", text)
        self.assertIn("(end of read big.py)", text)
        self.assertEqual(session._pending_pages, [])
        # The flag stays off until the operator toggles back.
        self.assertFalse(session._show_tool_output)


class CompactPagerParityTest(unittest.TestCase):
    """The same flow through the real REPL with a live CompactLayout.

    Plain frame mode prints to the terminal's scrollback and has no
    viewport; compact mode renders into CompactLayout, which scrolls and
    wraps. The empty-Enter pager and the tool-output boxes are session
    level, so they must behave identically with a layout attached - the
    capped read is paged through the same queue, and the viewport is
    back at the bottom (offset 0) exactly where the operator left it.
    """

    COLS, ROWS = 100, 24

    def setUp(self):
        from test_console_session import make_session
        import mantra.core.sessions as sessions

        self.workspace = tempfile.mkdtemp(prefix="mantra-compact-e2e-")
        self._make_session = make_session
        self._store = tempfile.mkdtemp(prefix="mantra-compact-store-")
        self._dir_patch = mock.patch.object(
            sessions,
            "sessions_dir",
            lambda: __import__("pathlib").Path(self._store),
        )
        self._dir_patch.start()
        self.addCleanup(self._dir_patch.stop)
        self.addCleanup(shutil.rmtree, self._store, True)
        self.term = FakeTerm(self.COLS, self.ROWS)
        self.term.isatty = lambda: False
        self._stdout = mock.patch.object(sys, "stdout", self.term)
        self._stdout.start()
        self.addCleanup(self._stdout.stop)
        self._term_size = mock.patch(
            "mantra.compact._term_size", lambda: (self.term.cols, self.term.rows)
        )
        self._term_size.start()
        self.addCleanup(self._term_size.stop)

    def _run(self, session, layout, lines):
        from mantra.console import Style, repl

        feed = iter(lines)
        real_input = builtins.input
        builtins.input = lambda *args: next(feed)
        try:
            layout.setup(0, session, Style(enabled=False))
            session.layout = layout
            repl(session, Style(enabled=False))
        finally:
            builtins.input = real_input
        return _strip_ansi("\n".join(layout.raw))

    def test_capped_read_pages_and_viewport_returns_to_bottom(self):
        from mantra.implementations.llm.mock_client import (
            final_response,
            tool_call_response,
        )

        body = "\n".join(f"line-{i:04d}" for i in range(150)) + "\n"
        with open(os.path.join(self.workspace, "big.py"), "w", encoding="utf-8") as fh:
            fh.write(body)
        session = self._make_session(
            self.workspace,
            [
                tool_call_response("read_file", {"path": "big.py"}),
                tool_call_response(
                    "run_command",
                    {"command": f'"{sys.executable}" -c "print(\'second-box-hidden\')"'},
                ),
                final_response("done paging"),
            ],
        )
        fired = []
        orig = session._on_tool_observation

        # Re-wire the observer to toggle output boxes mid-turn, like the
        # plain-frame variant of this test.
        def hooked(tool, observation, step):
            out = orig(tool, observation, step)
            if not fired:
                fired.append(True)
                session.toggle_tool_output()
            return out

        session._on_tool_observation = hooked
        layout = compact.CompactLayout()
        text = self._run(
            session, layout, ["read and run", "", "", "", "", "", "", "", "/exit"]
        )
        # Same transcript markers as the plain frame-mode run.
        self.assertIn("┌ read big.py", text)
        self.assertIn("press Enter (empty prompt) to page", text)
        self.assertIn("tool output boxes off", text)
        self.assertNotIn("second-box-hidden", text)
        self.assertIn("page through it", text)
        self.assertIn("read big.py (continued)", text)
        self.assertIn("line-0149", text)
        self.assertIn("(end of read big.py)", text)
        self.assertEqual(session._pending_pages, [])
        self.assertFalse(session._show_tool_output)
        # Compact-mode parity: the viewport is at the bottom where the
        # operator left it - no scroll marker, next prompt on the last row.
        self.assertEqual(layout.offset, 0)
        self.assertNotIn("↑", text)

    def test_scrolling_while_paging_keeps_content_intact(self):
        """Scroll + page interleave exactly as in plain mode: scrolling the
        viewport never disturbs the pager queue or its drain order."""
        from mantra.console import Style, repl
        from mantra.implementations.llm.mock_client import (
            final_response,
            tool_call_response,
        )

        body = "\n".join(f"chunk-{i:04d}" for i in range(120)) + "\n"
        with open(os.path.join(self.workspace, "big.txt"), "w", encoding="utf-8") as fh:
            fh.write(body)
        session = self._make_session(
            self.workspace,
            [
                tool_call_response("read_file", {"path": "big.txt"}),
                final_response("done"),
            ],
        )
        layout = compact.CompactLayout()
        # Page through with a scroll-up (as if the operator wheeled up to
        # re-read between presses) halfway through the drain.
        feed = iter(["read it", "", "", "", "", "", "", "/exit"])
        calls = {"n": 0}

        def scrolling_input(*args):
            calls["n"] += 1
            value = next(feed)
            if calls["n"] == 4 and value == "":
                layout.scroll_up(6)
            return value

        real_input = builtins.input
        builtins.input = scrolling_input
        try:
            layout.setup(0, session, Style(enabled=False))
            session.layout = layout
            repl(session, Style(enabled=False))
        finally:
            builtins.input = real_input
        text = _strip_ansi("\n".join(layout.raw))
        self.assertIn("chunk-0119", text)
        self.assertIn("(end of read big.txt)", text)
        self.assertEqual(session._pending_pages, [])
        self.assertIn("read big.txt (continued)", text)
        # Scrolling mid-paging neither dropped content nor wedged the
        # drain; the operator is simply left scrolled up where they were.
        self.assertGreater(layout.offset, 0)
        layout.scroll_to_bottom()
        self.assertEqual(layout.offset, 0)


class StreamSelectionDeferTest(unittest.TestCase):
    """Streaming repaints defer while the host performs a native selection.

    The terminal host owns the mouse during streaming (quick-edit on,
    mouse-input off on Windows), so a click-drag selection over the
    transcript must never be disturbed by viewport repaints. Fragments
    that arrive mid-drag are buffered and flushed once the drag ends.
    """

    def setUp(self):
        from test_console_session import make_session

        self.workspace = tempfile.mkdtemp(prefix="mantra-seldef-")
        self.addCleanup(shutil.rmtree, self.workspace, ignore_errors=True)
        self.session = make_session(self.workspace, [])
        # Identity renderer: the deferral logic is what is under test, not
        # the markdown renderer's partial-line buffering.
        self.session._stream_renderer = mock.Mock(
            render_piece=lambda p: p, flush=lambda: ""
        )
        self.writes: list[str] = []
        layout = mock.Mock()
        layout.active = True
        layout.write = self.writes.append
        self.session.layout = layout

    def test_deltas_defer_during_selection_then_flush_in_order(self):
        selecting = {"on": True}
        with mock.patch(
            "mantra.console.selection_in_progress",
            side_effect=lambda: selecting["on"],
        ):
            self.session._on_delta("hel")
            self.session._on_delta("lo")
            # No repaint while the host is selecting.
            self.assertEqual(self.writes, [])
            selecting["on"] = False
            self.session._on_delta("!")
        # Header first, then the deferred fragments, then the live one,
        # all in arrival order.
        self.assertTrue(self.writes[0].startswith("ENCHANTER"))
        self.assertEqual("".join(self.writes[1:]), "hel" + "lo" + "!")
        self.assertEqual(self.session._deferred_stream, [])

    def test_flush_deferred_stream_lands_before_tail(self):
        # Fragments buffered while selecting must flush even when the
        # stream ends right after the selection (no further deltas).
        selecting = {"on": True}
        with mock.patch(
            "mantra.console.selection_in_progress",
            side_effect=lambda: selecting["on"],
        ):
            self.session._on_delta("tail-")
            self.session._flush_deferred_stream()
        self.assertEqual("".join(self.writes[1:]), "tail-")
        self.assertEqual(self.session._deferred_stream, [])

    def test_selection_in_progress_safe_when_no_console(self):
        # Outside a real console (tests, pipes, POSIX) the probe must
        # report False so streaming is never accidentally suppressed.
        from mantra.term import selection_in_progress

        self.assertFalse(selection_in_progress())


class DiffPaneRenderTest(unittest.TestCase):
    """Behaviour of the old/new pane renderer (_diff_pane_rows).

    Pins the dedupe rules: a context line is identical on both sides, so
    it must appear exactly once - in whichever pane holds the change
    nearest it - and a pane left with no lines at all (a pure insertion
    or deletion) must be skipped rather than printed as an empty header.
    """

    def _style(self, enabled: bool):
        style = Style.__new__(Style)
        style.enabled = enabled
        return style

    def _session(self, enabled: bool = False):
        """A bare session with just enough state for the pane renderers."""
        session = ConsoleSession.__new__(ConsoleSession)
        session.style = self._style(enabled)
        session.layout = None
        session._spinner = None
        session._pending_pages = []
        session._pending_styled = []
        return session

    def _rows(self, diff_text: str, enabled: bool = False, max_lines: int = 400) -> list[str] | None:
        return self._session(enabled)._diff_pane_rows(diff_text, max_lines=max_lines)

    def _diff(self, old: str, new: str) -> str:
        import difflib

        return "\n".join(
            difflib.unified_diff(
                old.splitlines(),
                new.splitlines(),
                fromfile="x.py (before)",
                tofile="x.py (after)",
                lineterm="",
                n=2,
            )
        )

    def test_context_lines_appear_once(self):
        # A mid-function change leaves shared context on both sides; each
        # such line must be shown once, never duplicated across the panes.
        old = "def greet():\n    name = input()\n    print(f\"hi {name}\")\n    return name"
        new = "def greet():\n    name = input()\n    print(f\"hello {name}\")\n    return name"
        rows = self._rows(self._diff(old, new))
        self.assertIsNotNone(rows)
        for ctx in ("def greet():", "    name = input()", "    return name"):
            self.assertEqual(
                [r for r in rows if r == f"│ {ctx}"],
                [f"│ {ctx}"],
                f"context {ctx!r} must appear exactly once",
            )
        joined = "\n".join(rows)
        self.assertIn('print(f"hi {name}")', joined)
        self.assertIn('print(f"hello {name}")', joined)

    def test_context_goes_to_the_pane_with_the_nearest_change(self):
        # 'top' sits one line from the removal (old pane) and two from the
        # addition; 'bottom' is the mirror image, so it lands in new.
        text = (
            "--- a/x.py\n+++ b/x.py\n"
            "@@ -1,4 +1,4 @@\n"
            " top-line\n"
            "-removed-things\n"
            "+added-things\n"
            " bottom-line\n"
        )
        rows = self._rows(text)
        self.assertIsNotNone(rows)
        idx_old = rows.index("│ old")
        idx_new = rows.index("│ new")
        self.assertLess(rows.index("│ top-line"), idx_new)
        self.assertGreater(rows.index("│ top-line"), idx_old)
        self.assertLess(rows.index("│ removed-things"), idx_new)
        self.assertGreater(rows.index("│ bottom-line"), idx_new)
        self.assertGreater(rows.index("│ added-things"), idx_new)

    def test_pure_insertion_skips_the_old_pane(self):
        rows = self._rows(self._diff("a\nb", "a\nmiddle\nb"))
        self.assertEqual(rows, ["│ new", "│ a", "│ middle", "│ b"])

    def test_pure_deletion_skips_the_new_pane(self):
        rows = self._rows(self._diff("a\nmiddle\nb", "a\nb"))
        self.assertEqual(rows, ["│ old", "│ a", "│ middle", "│ b"])

    def test_unparseable_content_returns_none(self):
        # Plain output, +/- lines without hunks, or empty text is not a
        # structured diff, so callers keep the generic renderer.
        self.assertIsNone(self._rows("plain output\nmore\n"))
        self.assertIsNone(self._rows("-removed\n+added\n"))
        self.assertIsNone(self._rows(""))

    def test_file_chip_only_for_git_style_diffs(self):
        # difflib labels end in '(before)'/'(after)' - the box title names
        # the file already, so no per-file chip is added.
        rows = self._rows(self._diff("a\nx", "a\ny"))
        self.assertNotIn("x.py", "\n".join(rows))
        # git-style a/ b/ headers get the chip so multi-file diffs stay
        # readable.
        git = "--- a/x.py\n+++ b/x.py\n@@ -1,2 +1,2 @@\n a\n-x\n+y\n"
        self.assertIn("│ x.py", self._rows(git))

    def test_removed_and_added_lines_use_soft_text_colours(self):
        text = "--- a/x.py\n+++ b/x.py\n@@ -1,2 +1,2 @@\n a\n-remove me\n+add me\n"
        rows = self._rows(text, enabled=True)
        self.assertIsNotNone(rows)
        remove_row = next(r for r in rows if "remove me" in r)
        add_row = next(r for r in rows if "add me" in r)
        self.assertIn(f"\x1b[{theme.DIFF_REMOVE}m", remove_row)
        self.assertIn(f"\x1b[{theme.DIFF_ADD}m", add_row)
        # Text colour only: no background codes anywhere in the panes.
        self.assertNotIn("48;", "".join(rows))

    def test_multiple_hunks_render_panes_per_hunk(self):
        # Two hunks in the same file: each hunk opens its own old/new
        # panes (context deduped within the hunk), but the file chip is
        # emitted once for the whole group.
        text = (
            "--- a/tool.py\n+++ b/tool.py\n"
            "@@ -1,3 +1,3 @@\n"
            " def run():\n"
            "-    old_call()\n"
            "+    new_call()\n"
            "     return True\n"
            "@@ -10,3 +10,3 @@\n"
            " def stop():\n"
            "-    stop_old()\n"
            "+    stop_new()\n"
            "     return False\n"
        )
        rows = self._rows(text)
        self.assertIsNotNone(rows)
        self.assertEqual(
            rows,
            [
                "│ tool.py",
                "│ old",
                "│ def run():",
                "│     old_call()",
                "│ new",
                "│     new_call()",
                "│     return True",
                "│ old",
                "│ def stop():",
                "│     stop_old()",
                "│ new",
                "│     stop_new()",
                "│     return False",
            ],
        )

    def test_multi_file_git_diff_groups_and_chips_each_file(self):
        # Real git output carries 'diff --git' / 'index' noise between
        # files. Each file group gets its own chip and hunks, in order.
        text = (
            "diff --git a/one.py b/one.py\n"
            "index 111..222 100644\n"
            "--- a/one.py\n+++ b/one.py\n"
            "@@ -1,2 +1,2 @@\n"
            " first\n"
            "-old-one\n"
            "+new-one\n"
            "diff --git a/two.py b/two.py\n"
            "index 333..444 100644\n"
            "--- a/two.py\n+++ b/two.py\n"
            "@@ -1,2 +1,2 @@\n"
            " second\n"
            "-old-two\n"
            "+new-two\n"
        )
        rows = self._rows(text)
        self.assertIsNotNone(rows)
        # File chips and hunks stay grouped and ordered; the noise lines
        # never leak into the panes.
        # 'first' sits beside the removal, so it lives in the old pane
        # only; nothing from one.py bleeds into the two.py group.
        self.assertEqual(
            rows,
            [
                "│ one.py",
                "│ old",
                "│ first",
                "│ old-one",
                "│ new",
                "│ new-one",
                "│ two.py",
                "│ old",
                "│ second",
                "│ old-two",
                "│ new",
                "│ new-two",
            ],
        )

    def test_binary_file_noise_does_not_abort_text_file_panes(self):
        # A binary pair has no hunks; its 'Binary files ... differ' note
        # must be skipped so the text file's hunks still render as panes.
        text = (
            "diff --git a/data.bin b/data.bin\n"
            "Binary files a/data.bin and b/data.bin differ\n"
            "diff --git a/code.py b/code.py\n"
            "index 111..222 100644\n"
            "--- a/code.py\n+++ b/code.py\n"
            "@@ -1,2 +1,2 @@\n"
            " def run():\n"
            "-    old()\n"
            "+    new()\n"
        )
        rows = self._rows(text)
        self.assertIsNotNone(rows)
        self.assertEqual(
            rows,
            [
                "│ code.py",
                "│ old",
                "│ def run():",
                "│     old()",
                "│ new",
                "│     new()",
            ],
        )

    def test_max_lines_truncates_and_notes_the_remainder(self):
        removed = "".join(f"-r{i}\n" for i in range(25))
        added = "".join(f"+a{i}\n" for i in range(25))
        text = f"--- a/x.py\n+++ b/x.py\n@@ -1,25 +1,25 @@\n{removed}{added}"
        # file chip + 2 pane chips + 50 changed lines = 53 rows; keep 20.
        rows = self._rows(text, max_lines=20)
        self.assertIsNotNone(rows)
        self.assertEqual(len(rows), 21)
        self.assertIn("│ x.py", rows)
        self.assertIn("│ old", rows)
        # The remainder note keeps the box's gutter like every other row.
        self.assertEqual(rows[-1], "│ … 33 more diff lines")
        self.assertEqual([r for r in rows[:-1] if r.startswith("│ r")], [f"│ r{i}" for i in range(18)])

    def test_diff_that_fits_budget_is_shown_whole(self):
        session = self._session()
        rows = ["│ old", "│     a = 1", "│ new", "│     a = 2"]
        shown = session._render_diff_pages("git diff", rows)
        self.assertIn("a = 1", shown)
        self.assertIn("a = 2", shown)
        self.assertIn("└", shown)
        # Small diffs never touch the pager queue.
        self.assertEqual(session._pending_pages, [])
        self.assertEqual(session._pending_styled, [])

    def test_overflowing_diff_pages_without_recolouring_rows(self):
        session = self._session()
        rows = (
            ["│ old"]
            + [f"│ removed-{i:02d}" for i in range(30)]
            + ["│ new"]
            + [f"│ added-{i:02d}" for i in range(30)]
        )
        shown = session._render_diff_pages("git diff", rows)
        self.assertIn("removed-00", shown)
        self.assertIn("page through the diff", shown)
        # One page stays on screen; the rest is queued as pre-styled rows.
        self.assertEqual(len(session._pending_pages), 1)
        self.assertEqual(session._pending_styled, [True])
        self.assertEqual(session._pending_pages[0][0], "git diff")
        self.assertEqual(len(session._pending_pages[0][1]), len(rows) - 24)

        buf = io.StringIO()
        with mock.patch.object(sys, "stdout", buf):
            guard = 0
            while session.page_next() and guard < 20:
                guard += 1
        self.assertEqual(session._pending_pages, [])
        self.assertEqual(session._pending_styled, [])
        text = buf.getvalue()
        self.assertIn("(continued)", text)
        self.assertIn("(end of git diff)", text)
        # Styled rows print verbatim: no double gutter from _row.
        self.assertNotIn("│ │", text)
        # Every line reaches the screen exactly once (preview + pages).
        combined = shown + text
        for i in range(30):
            self.assertEqual(combined.count(f"removed-{i:02d}"), 1, f"removed-{i:02d} duplicated or lost")
            self.assertEqual(combined.count(f"added-{i:02d}"), 1, f"added-{i:02d} duplicated or lost")


class StyleApiTest(unittest.TestCase):
    """Style exposes exactly the pruned palette surface.

    After the neon-primitive sweep, every colour the console draws must
    come from the Blood & Bone tokens, so Style carries only the SGR
    weights still used (bold/dim/strike) plus the semantic wrappers the
    UI actually calls. Re-adding an 8-colour or bright primitive (or an
    unused semantic wrapper) is a drift back to raw ANSI - this pins the
    method set so that drift shows up as a failing test.
    """

    # Wrappers the console calls today. Structural members (_wrap,
    # __init__) are not styling methods and are not part of the contract.
    ALLOWED = {
        # SGR weight / decoration primitives still used directly.
        "bold",
        "dim",
        "strike",
        # Blood & Bone semantic palette.
        "brand",
        "selected",
        "warn",
        "ember",
        "bone",
        "ash",
        "hair",
    }
    # Pruned primitives that must never come back.
    FORBIDDEN = {
        "red", "green", "yellow", "blue", "magenta", "cyan", "grey",
        "bright_red", "bright_green", "bright_yellow", "bright_blue",
        "bright_magenta", "bright_cyan", "bright_white",
        "bg_grey", "on_grey", "on_grey_light",
    }

    def _methods(self):
        return {
            name
            for name, value in vars(Style).items()
            if callable(value) and not name.startswith("_")
        }

    def test_style_exposes_only_the_pruned_method_set(self):
        self.assertEqual(self._methods(), self.ALLOWED)

    def test_pruned_primitives_are_not_attributes(self):
        style = Style(enabled=False)
        for name in self.FORBIDDEN:
            self.assertFalse(hasattr(style, name), f"Style.{name} must stay pruned")

    def test_palette_wrappers_map_to_theme_tokens(self):
        style = Style(enabled=True)
        self.assertEqual(style.brand("M"), f"\033[{theme.BLOOD_BOLD}mM\033[0m")
        self.assertEqual(style.selected("M"), f"\033[{theme.BLOOD_BOLD}mM\033[0m")
        self.assertEqual(style.warn("M"), f"\033[{theme.WARN}mM\033[0m")
        self.assertEqual(style.ember("M"), f"\033[{theme.EMBER}mM\033[0m")
        self.assertEqual(style.bone("M"), f"\033[{theme.BONE}mM\033[0m")
        self.assertEqual(style.ash("M"), f"\033[{theme.ASH}mM\033[0m")
        self.assertEqual(style.hair("M"), f"\033[{theme.HAIR}mM\033[0m")

    def test_wrappers_pass_text_through_when_disabled(self):
        style = Style(enabled=False)
        for name in self.ALLOWED:
            self.assertEqual(getattr(style, name)("M"), "M", name)


if __name__ == "__main__":
    unittest.main()
