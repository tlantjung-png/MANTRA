"""Tests for the single-key line editor and the console completer.

The editor talks to a real terminal, so it cannot be driven directly in a
test. Instead these tests subclass it and swap out the two terminal
touchpoints - raw mode and key reading - while capturing what it paints.
What they assert is the observable contract: a popup appears when there is
something to complete, Tab inserts the choice, Escape closes it.
"""

from __future__ import annotations

import io
import os
import re
import sys
import tempfile
import unittest
from contextlib import contextmanager
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import mantra.line_editor as line_editor
from mantra.line_editor import Completion, LineEditor, visible_len
from mantra.console import ConsoleCompleter, SLASH_COMMANDS


class ScriptedEditor(LineEditor):
    """An editor that plays back a fixed key sequence and records output."""

    def __init__(self, keys, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.keys = list(keys)
        self.frames: list[str] = []
        self._raw = ""

    @contextmanager
    def _raw_mode(self):
        yield

    def _read_key(self):
        if not self.keys:
            raise AssertionError("editor asked for a key but the script is empty")
        return self.keys.pop(0)

    def _draw(self, *args, **kwargs):
        """Capture what one repaint emits, then forward it to the real stdout."""
        buf = io.StringIO()
        real = sys.stdout
        sys.stdout = buf
        try:
            rows = super()._draw(*args, **kwargs)
        finally:
            sys.stdout = real
        self.frames.append(buf.getvalue())
        real.write(buf.getvalue())
        return rows

    def read(self, prompt=""):
        # Capture the whole session, not just the _draw calls: teardown
        # writes erase sequences that must show up in the record too.
        session = io.StringIO()
        real = sys.stdout
        sys.stdout = session
        try:
            with mock.patch.object(sys.stdin, "isatty", lambda: True), mock.patch.object(
                sys.stdout, "isatty", lambda: True
            ):
                return super().read(prompt)
        finally:
            sys.stdout = real
            self._raw = session.getvalue()

    @property
    def painted(self) -> str:
        """Everything emitted, including the teardown that erases rows."""
        return self._raw

    @property
    def while_typing(self) -> str:
        """Only what the repaints emitted - the screen as the user sees it.

        Teardown is excluded, so this is the popup still on screen before
        Enter dismisses it.
        """
        return "".join(self.frames)

    @property
    def last_frame(self) -> str:
        """The screen as it stands after the final keystroke."""
        return self.frames[-1] if self.frames else ""


class _Style:
    """Enough styling to prove the editor survives ANSI in the prompt."""

    def cyan(self, t):
        return f"\033[36m{t}\033[0m"

    def dim(self, t):
        return f"\033[2m{t}\033[0m"

    def bold(self, t):
        return f"\033[1m{t}\033[0m"

    def selected(self, t):
        return f"\033[1;38;5;131m{t}\033[0m"


class _FakeCompleter:
    """Completes tokens after '@' or a leading '/'."""

    def complete(self, buffer, cursor):
        head = buffer[:cursor]
        if head.startswith("/"):
            token = head
            start = 0
        else:
            at = head.rfind("@")
            if at < 0:
                return None
            token = head[at:]
            start = at
        pool = ["/help", "/model", "/compact"] if token.startswith("/") else ["@src/a.py", "@src/b.py"]
        hits = [p for p in pool if p.startswith(token)]
        if not hits:
            return None
        return Completion(items=hits, start=start, end=cursor)


class Screen:
    """A minimal terminal that applies the escape codes the editor emits.

    Substring assertions cannot tell a correctly positioned caret from a
    badly positioned one, since both contain the same bytes. This models
    just enough of a VT100 - carriage return, line clear, relative and
    absolute cursor moves - to know where text actually lands.
    """

    CSI = re.compile(r"\033\[([?0-9;]*)([A-Za-z])")

    def __init__(self, columns: int = 80, rows: int = 24):
        self.columns = columns
        self.rows = rows
        self.grid = [[" "] * columns for _ in range(rows)]
        self.row = 0
        self.col = 0

    def feed(self, data: str) -> None:
        index = 0
        while index < len(data):
            match = self.CSI.search(data, index)
            if not match:
                self._write(data[index:])
                return
            self._write(data[index : match.start()])
            params = match.group(1)
            # Skip synchronized output sequences (start with ? or >)
            if not params.startswith("?") and not params.startswith(">"):
                # SGR colour codes carry multi-part params (e.g.
                # 1;38;5;131) a real terminal ignores; don't int() them.
                if match.group(2) == "m":
                    index = match.end()
                    continue
                count = int(params) if params else 1
                self._csi(match.group(2), count)
            index = match.end()

    def _write(self, text: str) -> None:
        for char in text:
            if char == "\r":
                self.col = 0
            elif char == "\n":
                self.row += 1
                self.col = 0
            elif char == "\t":
                self.col = min(self.columns - 1, (self.col // 8 + 1) * 8)
            elif char >= " ":
                if self.col >= self.columns:
                    self.col = 0
                    self.row += 1
                if 0 <= self.row < self.rows:
                    self.grid[self.row][self.col] = char
                self.col += 1
            self._clamp()

    def _csi(self, final: str, count: int) -> None:
        if final == "A":
            self.row = max(0, self.row - count)
        elif final == "B":
            self.row = min(self.rows - 1, self.row + count)
        elif final == "C":
            self.col = min(self.columns - 1, self.col + count)
        elif final == "D":
            self.col = max(0, self.col - count)
        elif final == "K":
            if 0 <= self.row < self.rows:
                for column in range(self.col, self.columns):
                    self.grid[self.row][column] = " "
        self._clamp()

    def _clamp(self) -> None:
        self.row = max(0, min(self.rows - 1, self.row))
        self.col = max(0, min(self.columns - 1, self.col))

    def line(self, index: int) -> str:
        return "".join(self.grid[index]).rstrip()

    @property
    def text(self) -> str:
        return "\n".join(self.line(i) for i in range(self.rows)).strip("\n")


class ScreenRenderingTest(unittest.TestCase):
    """The frames must paint a sensible screen, not merely contain bytes."""

    def _screen(self, keys):
        """The screen as it stands while the user is still typing."""
        editor = ScriptedEditor(keys, _Style(), completer=_FakeCompleter())
        editor.keys.append("\r")
        editor.read("mantra> ")
        screen = Screen()
        screen.feed(editor.while_typing)
        return screen

    def _submitted(self, keys):
        """The screen after Enter, ready for the next command's output."""
        editor = ScriptedEditor(keys, _Style(), completer=_FakeCompleter())
        editor.keys.append("\r")
        editor.read("mantra> ")
        screen = Screen()
        screen.feed(editor.painted)
        return screen

    def test_prompt_and_typed_text_land_on_one_line(self):
        screen = self._screen(list("hello"))
        self.assertEqual(screen.line(0), "mantra> hello")

    def test_popup_sits_below_the_prompt(self):
        screen = self._screen(list("/"))
        self.assertEqual(screen.line(0), "mantra> /")
        self.assertIn("/help", screen.line(1))
        self.assertIn("/model", screen.line(2))
        self.assertIn("/compact", screen.line(3))

    def test_repaint_leaves_no_stale_rows(self):
        # Typing narrows three suggestions to one; the discarded rows must
        # be erased rather than left behind as garbage.
        screen = self._screen(list("/mod"))
        self.assertEqual(screen.line(0), "mantra> /mod")
        self.assertIn("/model", screen.line(1))
        self.assertNotIn("/help", screen.text)
        self.assertNotIn("/compact", screen.text)

    def test_prompt_with_a_leading_newline_does_not_walk_down(self):
        # The console passes "\nmantra> " to separate turns. Every repaint
        # re-emits the whole prompt, so that newline stepped the line down
        # one row per keystroke, stranding blank rows above the text.
        editor = ScriptedEditor(list("ddd"), _Style(), completer=_NoCompleter())
        editor.keys.append("\r")
        editor.read("\nmantra> ")
        screen = Screen()
        screen.feed(editor.while_typing)
        self.assertEqual(screen.line(0), "mantra> ddd")
        self.assertEqual(screen.line(1), "")

    def test_long_input_stays_on_one_row(self):
        # Same bug, many keys: the drift is one row per keypress, so a
        # short test would not have shown how bad it gets.
        editor = ScriptedEditor(list("d" * 40), _Style(), completer=_NoCompleter())
        editor.keys.append("\r")
        editor.read("\nmantra> ")
        screen = Screen(rows=60)
        screen.feed(editor.while_typing)
        self.assertEqual(screen.line(0), "mantra> " + "d" * 40)
        for row in range(1, 5):
            self.assertEqual(screen.line(row), "", f"row {row} should be empty")

    def test_leading_blank_line_is_printed_exactly_once(self):
        editor = ScriptedEditor(list("d"), _Style(), completer=_NoCompleter())
        editor.keys.append("\r")
        editor.read("\nmantra> ")
        screen = Screen()
        screen.feed(editor.painted)
        self.assertEqual(screen.line(0), "")  # the separator
        self.assertEqual(screen.line(1), "mantra> d")

    def test_shrinking_popup_erases_the_rows_it_vacates(self):
        # Off-by-one guard: clearing before moving down erases the prompt
        # row and leaves the bottom popup row behind, so narrowing 3
        # suggestions to 1 stranded '/compact' and the hint on screen.
        screen = self._screen(list("/mod"))
        self.assertEqual(screen.line(3).strip(), "")
        self.assertEqual(screen.line(4).strip(), "")
        self.assertNotIn("compact", screen.text)

    def test_screen_helper_sees_the_popup_while_typing(self):
        # Guard for the helper itself: it must not include teardown, or
        # every popup assertion would trivially see a cleared screen.
        editor = ScriptedEditor(list("/"), _Style(), completer=_FakeCompleter())
        editor.keys.append("\r")
        editor.read("mantra> ")
        screen = Screen()
        screen.feed(editor.while_typing)
        self.assertIn("/help", screen.text)

    def test_popup_rows_are_cleared_when_the_line_is_submitted(self):
        # The bug this guards: the popup is painted below the prompt, so it
        # outlived the line that produced it. The next command's output then
        # overwrote only its own width, leaving the tails of the old rows
        # on screen ('...v1· esc dismisses').
        screen = self._submitted(list("/"))
        screen.feed("model      gpt-4o-mini")
        self.assertEqual(screen.line(1).strip(), "model      gpt-4o-mini")
        self.assertNotIn("esc dismisses", screen.text)
        self.assertNotIn("/compact", screen.text)

    def test_popup_rows_are_cleared_after_ctrl_c(self):
        editor = ScriptedEditor(list("/") + ["\x03"], _Style(), completer=_FakeCompleter())
        with self.assertRaises(KeyboardInterrupt):
            editor.read("mantra> ")
        screen = Screen()
        screen.feed(editor.painted)
        self.assertNotIn("esc dismisses", screen.text)

    def test_no_popup_means_no_stray_blank_lines(self):
        # With nothing suggested, finishing must not clear rows that were
        # never drawn, which would eat a line of scrollback.
        editor = ScriptedEditor(list("hi"), _Style(), completer=_FakeCompleter())
        editor.keys.append("\r")
        editor.read("mantra> ")
        screen = Screen()
        screen.feed(editor.painted)
        self.assertEqual(screen.line(0).strip(), "mantra> hi")
        self.assertEqual(screen.line(1).strip(), "")

    def test_caret_ends_after_the_typed_character(self):
        editor = ScriptedEditor(list("ab"), _Style(), completer=_FakeCompleter())
        editor.keys.append("\r")
        editor.read("mantra> ")
        screen = Screen()
        screen.feed(editor.while_typing)
        self.assertEqual(screen.col, len("mantra> ab"))

    def test_styled_prompt_does_not_shift_the_caret(self):
        # Same buffer, prompt now carries colour codes. The caret must be
        # in the same place as with the plain prompt.
        editor = ScriptedEditor(list("ab"), _Style(), completer=_FakeCompleter())
        editor.keys.append("\r")
        editor.read("\033[36mmantra\033[0m> ")
        screen = Screen()
        screen.feed(editor.while_typing)
        self.assertEqual(screen.col, len("mantra> ab"))


class VisibleLengthTest(unittest.TestCase):
    def test_escape_codes_are_not_counted(self):
        self.assertEqual(visible_len("\033[36mmantra\033[0m> "), len("mantra> "))

    def test_plain_text_is_unchanged(self):
        self.assertEqual(visible_len("mantra> "), 8)


class EditorBehaviourTest(unittest.TestCase):
    def _run(self, keys, completer=None):
        """Play ``keys`` then Enter. Returns (line, editor)."""
        editor = ScriptedEditor(keys, _Style(), completer=completer or _FakeCompleter())
        editor.keys.append("\r")
        line = editor.read("\033[36mmantra\033[0m> ")
        return line, editor

    def test_typed_text_is_returned(self):
        line, _ = self._run(list("hello"))
        self.assertEqual(line, "hello")

    def test_popup_offers_matches_after_at(self):
        line, editor = self._run(list("@src/") + ["\r"])
        self.assertEqual(line, "@src/a.py")
        self.assertIn("@src/a.py", editor.last_frame)

    def test_popup_offers_commands_after_slash(self):
        _, editor = self._run(list("/"))
        self.assertIn("/help", editor.last_frame)
        self.assertIn("/model", editor.last_frame)

    def test_tab_accepts_the_highlighted_item(self):
        line, _ = self._run(list("/") + ["\t"])
        self.assertEqual(line, "/help")

    def test_typing_narrows_the_popup(self):
        _, editor = self._run(list("/mod"))
        self.assertIn("/model", editor.last_frame)
        self.assertNotIn("/help", editor.last_frame)

    def test_down_then_tab_picks_the_second_item(self):
        line, _ = self._run(list("/") + [line_editor.KEY_DOWN, "\t"])
        self.assertEqual(line, "/model")

    def test_escape_closes_the_popup(self):
        _, editor = self._run(list("/") + ["\x1b"])
        self.assertNotIn("tab completes", editor.last_frame)

    def test_escape_stays_dismissed_while_typing_continues(self):
        line, editor = self._run(list("/") + ["\x1b"] + list("mod"))
        self.assertEqual(line, "/mod")
        self.assertNotIn("tab completes", editor.last_frame)

    def test_tab_brings_back_a_dismissed_popup(self):
        _, editor = self._run(list("/") + ["\x1b", "m", "\t"])
        self.assertIn("/model", editor.last_frame)

    def test_backspace_edits_mid_buffer(self):
        line, _ = self._run(list("abc") + [line_editor.KEY_LEFT, "\x7f"])
        self.assertEqual(line, "ac")

    def test_ctrl_c_raises(self):
        with self.assertRaises(KeyboardInterrupt):
            self._run(["\x03"])

    def test_ctrl_d_on_empty_line_raises_eof(self):
        with self.assertRaises(EOFError):
            self._run(["\x04"])

    def test_ctrl_d_with_text_is_ignored(self):
        line, _ = self._run(list("hi") + ["\x04"])
        self.assertEqual(line, "hi")

    def test_no_completer_means_no_popup(self):
        line, editor = self._run(list("plain text"), completer=_NoCompleter())
        self.assertEqual(line, "plain text")
        self.assertNotIn("tab completes", editor.painted)

    def test_cursor_column_ignores_ansi_in_prompt(self):
        # The prompt is 8 visible columns; after one character the caret
        # must land at column 9, not past the escape bytes.
        _, editor = self._run(list("x"))
        self.assertIn("\033[9C", editor.last_frame)

    def test_popup_rows_are_erased_before_repaint(self):
        # Every repaint must clear the rows the previous one drew,
        # otherwise suggestions smear down the screen.
        _, editor = self._run(list("/mod"))
        for frame in editor.frames[1:]:
            if "\033[K" not in frame:
                self.fail(f"repaint did not clear the line: {frame!r}")


class _NoCompleter:
    def complete(self, buffer, cursor):
        return None


class NonTtyFallbackTest(unittest.TestCase):
    def test_pipes_use_plain_input(self):
        editor = LineEditor(_Style(), completer=_FakeCompleter())
        with mock.patch.object(sys.stdin, "isatty", lambda: False), mock.patch.object(
            sys.stdin, "readline", lambda: "piped line\n"
        ):
            self.assertEqual(editor.read("mantra> "), "piped line")


class ConsoleCompleterTest(unittest.TestCase):
    """The real completer, against a temporary workspace."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        for rel in ("src/app.py", "src/util.py", "README.md"):
            path = os.path.join(self.root, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("x\n")
        os.makedirs(os.path.join(self.root, ".git"), exist_ok=True)

        class _Sandbox:
            root = self.root

        class _Session:
            # The completer only reads sandbox.root and workspace.
            sandbox = _Sandbox()
            workspace = self.root

        self.completer = ConsoleCompleter(_Session())

    def tearDown(self):
        self.tmp.cleanup()

    def test_slash_at_start_lists_commands(self):
        result = self.completer.complete("/he", 3)
        self.assertIsNotNone(result)
        self.assertIn("/help", result.items)

    def test_slash_midline_is_not_a_command(self):
        self.assertIsNone(self.completer.complete("look at /he", 11))

    def test_at_lists_workspace_files(self):
        result = self.completer.complete("fix @src/ap", 11)
        self.assertIsNotNone(result)
        self.assertTrue(any("app.py" in item for item in result.items))

    def test_at_skips_git_directory(self):
        result = self.completer.complete("@", 1)
        self.assertIsNotNone(result)
        self.assertFalse(any(".git" in item for item in result.items))

    def test_at_with_no_match_gives_nothing(self):
        self.assertIsNone(self.completer.complete("@zzzznope", 9))

    def test_plain_text_offers_nothing(self):
        self.assertIsNone(self.completer.complete("hello world", 11))

    def test_command_metadata_is_complete(self):
        names = [name for name, _ in SLASH_COMMANDS]
        self.assertEqual(len(names), len(set(names)), "duplicate slash command names")
        for name, description in SLASH_COMMANDS:
            self.assertTrue(name.startswith("/"), f"{name} should start with a slash")
            self.assertTrue(description.strip(), f"{name} needs a description")


if __name__ == "__main__":
    unittest.main()
