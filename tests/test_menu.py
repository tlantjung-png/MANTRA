"""Tests for the selectable menu every /command with children opens.

The menu reads single keystrokes and mouse reports off a terminal, which
a test does not have. So the pure parts - filtering, cursor movement,
rendering, and translating a click into a row - are checked directly, and
the handful of places that talk to the terminal are stubbed to a script.
"""

from __future__ import annotations

import io
import os
import sys
import unittest
from unittest import mock

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_TESTS_DIR)
for _path in (os.path.join(_PROJECT_ROOT, "src"), _PROJECT_ROOT, _TESTS_DIR):
    if _path not in sys.path:
        sys.path.insert(0, _path)
from mantra.console import Style
from mantra.core.menu import (
    KEY_BACKSPACE,
    KEY_CANCEL,
    KEY_DOWN,
    KEY_ENTER,
    KEY_UP,
    Menu,
    MouseEvent,
    Option,
    choose,
    options_from,
    visible_len,
)


class _Off(Style):
    """A style with no ANSI, and no VT side effects on Windows."""

    def __init__(self) -> None:
        self.enabled = False


def _menu(options, **kwargs) -> Menu:
    return Menu(_Off(), "pick one", options_from(options), **kwargs)


class OptionTest(unittest.TestCase):
    """Rows are built from whatever the caller happens to have."""

    def test_label_defaults_to_the_value(self):
        self.assertEqual(Option(value="gpt-4o").label, "gpt-4o")

    def test_text_joins_label_and_hint(self):
        self.assertEqual(Option("gpt-4o", hint="thinks").text, "gpt-4o  thinks")

    def test_text_without_a_hint_is_just_the_label(self):
        self.assertEqual(Option("gpt-4o").text, "gpt-4o")

    def test_plain_strings_become_options(self):
        options = options_from(["a", "b"])
        self.assertEqual([o.value for o in options], ["a", "b"])
        self.assertEqual(options[0].label, "a")

    def test_pairs_become_value_and_hint(self):
        options = options_from([("a", "current"), ("b", "")])
        self.assertEqual(options[0].value, "a")
        self.assertEqual(options[0].hint, "current")
        self.assertEqual(options[1].hint, "")

    def test_options_pass_straight_through(self):
        given = Option("a", hint="x")
        self.assertIs(options_from([given])[0], given)

    def test_anything_is_coerced_to_a_string(self):
        self.assertEqual(options_from([1, 2])[0].value, "1")


class VisibleLenTest(unittest.TestCase):
    def test_escapes_are_not_counted(self):
        self.assertEqual(visible_len("\033[36mhello\033[0m"), 5)

    def test_plain_text_is_its_length(self):
        self.assertEqual(visible_len("hello"), 5)


class FilterTest(unittest.TestCase):
    """Typing narrows a long catalogue; that is how 300 models stay usable."""

    def test_matches_are_case_insensitive(self):
        menu = _menu(["Gpt-4o", "o3-mini"])
        menu.query = "gpt"
        self.assertEqual([o.value for o in menu.matches], ["Gpt-4o"])

    def test_the_hint_is_searchable_too(self):
        menu = _menu([("o3-mini", "thinks"), ("gpt-4o", "")])
        menu.query = "thinks"
        self.assertEqual([o.value for o in menu.matches], ["o3-mini"])

    def test_no_query_means_everything(self):
        menu = _menu(["a", "b"])
        self.assertEqual(len(menu.matches), 2)

    def test_filtering_can_be_turned_off(self):
        # The effort menu is five fixed rows; filtering it would only
        # hide the option the operator is looking for.
        menu = _menu(["low", "high"], allow_filter=False)
        menu.query = "low"
        self.assertEqual(len(menu.matches), 2)

    def test_nothing_matching_is_shown_not_silently_empty(self):
        menu = _menu(["a"])
        menu.query = "zzz"
        self.assertEqual(menu.matches, [])
        joined = "\n".join(menu._rows())
        self.assertIn("no matches", joined)


class CursorTest(unittest.TestCase):
    """The highlight must not run off either end of the list."""

    def test_down_stops_at_the_last_row(self):
        menu = _menu(["a", "b"])
        for _ in range(5):
            menu._handle(KEY_DOWN)
        self.assertEqual(menu.cursor, 1)

    def test_up_stops_at_the_first_row(self):
        menu = _menu(["a", "b"])
        menu._handle(KEY_DOWN)
        for _ in range(5):
            menu._handle(KEY_UP)
        self.assertEqual(menu.cursor, 0)

    def test_enter_returns_the_highlighted_value(self):
        menu = _menu(["a", "b"])
        menu._handle(KEY_DOWN)
        self.assertEqual(menu._handle(KEY_ENTER), "b")

    def test_enter_on_a_filtered_list_returns_the_visible_row(self):
        menu = _menu(["alpha", "beta"])
        menu._handle("b")
        self.assertEqual(menu._handle(KEY_ENTER), "beta")

    def test_cancel_returns_empty_rather_than_none(self):
        # "" means "the operator said no"; None means "there is no
        # terminal". Callers need to tell those apart.
        self.assertEqual(_menu(["a"])._handle(KEY_CANCEL), "")

    def test_enter_with_nothing_to_choose_keeps_waiting(self):
        menu = _menu(["a"])
        menu.query = "zzz"
        self.assertIsNone(menu._handle(KEY_ENTER))

    def test_typing_resets_the_cursor(self):
        menu = _menu(["a", "b"])
        menu._handle(KEY_DOWN)
        menu._handle("b")
        self.assertEqual(menu.cursor, 0)

    def test_backspace_edits_the_query(self):
        menu = _menu(["a"])
        for char in "ab":
            menu._handle(char)
        self.assertEqual(menu.query, "ab")
        menu._handle(KEY_BACKSPACE)
        self.assertEqual(menu.query, "a")

    def test_keys_below_space_are_ignored(self):
        # Control bytes are not filter text; treating them as such would
        # put raw escapes into the query.
        menu = _menu(["a"])
        menu._handle("\x01")
        self.assertEqual(menu.query, "")


class RenderTest(unittest.TestCase):
    """What lands on screen."""

    def test_the_title_is_the_first_row(self):
        self.assertIn("pick one", _menu(["a"])._rows()[0])

    def test_the_cursor_is_marked(self):
        menu = _menu(["a", "b"])
        rows = menu._rows()
        self.assertIn("›", rows[1])
        self.assertNotIn("›", rows[2])

    def test_only_max_rows_are_drawn(self):
        import re

        menu = _menu([f"m{i}" for i in range(20)], max_rows=3)
        rows = menu._rows()
        # Count option rows only: the "n more" line mentions "more",
        # not a model, and must not be mistaken for one.
        drawn = [r for r in rows if re.match(r"^ [› ] m\d+$", r)]
        self.assertEqual(len(drawn), 3)

    def test_hidden_rows_are_counted(self):
        rows = _menu([f"m{i}" for i in range(20)], max_rows=3)._rows()
        self.assertTrue(any("more" in r for r in rows))

    def test_a_long_list_offers_filtering(self):
        rows = _menu([f"m{i}" for i in range(20)], max_rows=3)._rows()
        self.assertTrue(any("type to filter" in r for r in rows))

    def test_the_query_is_echoed(self):
        menu = _menu(["a"])
        menu.query = "ab"
        self.assertTrue(any("ab" in r for r in menu._rows()))

    def test_the_hint_is_shown_last(self):
        rows = Menu(_Off(), "t", options_from(["a"]), hint="esc cancels")._rows()
        self.assertIn("esc cancels", rows[-1])

    def test_a_short_list_does_not_offer_filtering(self):
        # An invitation to type is noise when there is nothing to find.
        rows = _menu(["a", "b"])._rows()
        self.assertFalse(any("type to filter" in r for r in rows))


class MouseTest(unittest.TestCase):
    """Clicks are reported as absolute terminal rows, not list indices."""

    def setUp(self):
        self.menu = _menu(["a", "b", "c"])
        # Rendering is what tells the menu where its rows are, so a
        # click arriving before the first draw has no meaning.
        self.menu._rows()

    def test_a_click_on_a_row_selects_it(self):
        event = MouseEvent(button=0, column=1, row=2, pressed=True)
        self.assertEqual(self.menu._handle(event), "a")

    def test_rows_are_one_based_from_the_title(self):
        event = MouseEvent(button=0, column=1, row=4, pressed=True)
        self.assertEqual(self.menu._handle(event), "c")

    def test_a_click_above_the_list_does_nothing(self):
        # The title row is not an option.
        event = MouseEvent(button=0, column=1, row=1, pressed=True)
        self.assertIsNone(self.menu._handle(event))

    def test_a_click_below_the_list_does_nothing(self):
        event = MouseEvent(button=0, column=1, row=99, pressed=True)
        self.assertIsNone(self.menu._handle(event))

    def test_a_release_does_not_select(self):
        # Dragging reports the release at a different row than the
        # press; only the press is a choice.
        event = MouseEvent(button=0, column=1, row=2, pressed=False)
        self.assertIsNone(self.menu._handle(event))

    def test_other_buttons_do_not_select(self):
        event = MouseEvent(button=2, column=1, row=2, pressed=True)
        self.assertIsNone(self.menu._handle(event))

    def test_a_scroll_report_is_not_a_choice(self):
        event = MouseEvent(button=64, column=1, row=2, pressed=True)
        self.assertIsNone(self.menu._handle(event))

    def test_clicks_follow_the_visible_rows_after_filtering(self):
        self.menu._handle("c")
        event = MouseEvent(button=0, column=1, row=2, pressed=True)
        self.assertEqual(self.menu._handle(event), "c")


class KeyReadingTest(unittest.TestCase):
    """Raw bytes in, menu keys out.

    These run the POSIX reader against a string, because that is the
    branch where escape sequences actually have to be parsed.
    """

    def _read(self, feed: str, menu: Menu | None = None):
        """Feed a byte string through the POSIX reader.

        "Is more input pending" is answered by how much of the feed is
        left, because that is what distinguishes a lone Escape from the
        start of an arrow key or a mouse report.
        """
        menu = menu or _menu(["a"])
        fake = io.StringIO(feed)
        pending = lambda: fake.tell() < len(feed)
        with mock.patch.object(sys, "stdin", fake), \
             mock.patch("mantra.core.menu.os.name", "posix"), \
             mock.patch.object(menu, "_input_pending", pending):
            return menu._read_key()

    def test_enter_arrives_as_carriage_return(self):
        self.assertEqual(self._read("\r"), KEY_ENTER)

    def test_escape_is_a_cancel(self):
        self.assertEqual(self._read("\x1b"), KEY_CANCEL)

    def test_backspace_arrives_as_delete(self):
        self.assertEqual(self._read("\x7f"), KEY_BACKSPACE)

    def test_arrow_keys_are_decoded(self):
        self.assertEqual(self._read("\x1b[A"), KEY_UP)
        self.assertEqual(self._read("\x1b[B"), KEY_DOWN)

    def test_a_letter_is_itself(self):
        self.assertEqual(self._read("g"), "g")

    def test_ctrl_c_raises_so_the_run_can_be_aborted(self):
        with self.assertRaises(KeyboardInterrupt):
            self._read("\x03")

    def test_a_mouse_report_is_parsed(self):
        key = self._read("\x1b[<0;12;4M")
        self.assertIsInstance(key, MouseEvent)
        self.assertEqual((key.button, key.column, key.row), (0, 12, 4))
        self.assertTrue(key.pressed)

    def test_a_mouse_release_is_flagged(self):
        key = self._read("\x1b[<0;12;4m")
        self.assertFalse(key.pressed)

    def test_space_pages_forward(self):
        menu = _menu([f"m{i}" for i in range(50)])
        with mock.patch.object(menu, "_input_pending", lambda: True):
            fake = io.StringIO(" ")
            with mock.patch.object(sys, "stdin", fake), \
                 mock.patch("mantra.core.menu.os.name", "posix"):
                self.assertIsNone(menu._read_key())
        # 12 is the default page size (max_rows).
        self.assertEqual(menu.cursor, 12)


class PickTest(unittest.TestCase):
    """The whole loop, with the terminal stubbed out."""

    def _pick(self, options, keys, **kwargs) -> str | None:
        menu = Menu(_Off(), "pick one", options_from(options), **kwargs)
        out = io.StringIO()
        fake_in = mock.MagicMock()
        fake_in.isatty.return_value = True
        fake_out = mock.MagicMock()
        fake_out.isatty.return_value = True
        fake_out.write = out.write
        fake_out.flush = lambda: None
        # Fake terminal plus scripted keys; os.system is stubbed to skip
        # the screen-clear side effect of pick().
        with mock.patch.object(sys, "stdin", fake_in), \
             mock.patch.object(sys, "stdout", fake_out), \
             mock.patch.object(Menu, "_read_key", side_effect=list(keys)), \
             mock.patch("mantra.core.menu.os.system"):
            return menu.pick()

    def test_enter_picks_the_highlighted_row(self):
        self.assertEqual(self._pick(["a", "b"], [KEY_DOWN, KEY_ENTER]), "b")

    def test_escape_returns_empty(self):
        self.assertEqual(self._pick(["a"], [KEY_CANCEL]), "")

    def test_a_click_picks_without_moving_the_cursor(self):
        self.menu_row = 2
        self.assertEqual(self._pick(["a", "b"], [MouseEvent(0, 1, 3, True)]), "b")

    def test_mouse_reporting_is_switched_on_and_off(self):
        # Left on, it would report clicks as garbage input in the
        # editor once the menu closed.
        out = io.StringIO()
        menu = Menu(_Off(), "t", options_from(["a"]))
        fake_in = mock.MagicMock()
        fake_in.isatty.return_value = True
        fake_out = mock.MagicMock()
        fake_out.isatty.return_value = True
        fake_out.write = out.write
        fake_out.flush = lambda: None
        with mock.patch.object(sys, "stdin", fake_in), \
             mock.patch.object(sys, "stdout", fake_out), \
             mock.patch.object(Menu, "_read_key", side_effect=[KEY_CANCEL]), \
             mock.patch("mantra.core.menu.os.system"):
            menu.pick()
        text = out.getvalue()
        self.assertIn("\033[?1000h", text)
        self.assertIn("\033[?1000l", text)

    def test_no_options_means_nothing_to_pick(self):
        self.assertIsNone(self._pick([], [KEY_ENTER]))

    def test_no_terminal_returns_none_without_reading(self):
        # A piped run must not block waiting for a cursor nobody can
        # move, nor eat the next line of the script.
        fake_in = mock.MagicMock()
        fake_in.isatty.return_value = False
        with mock.patch.object(sys, "stdin", fake_in), \
             mock.patch.object(Menu, "_read_key") as read:
            self.assertIsNone(_menu(["a"]).pick())
        read.assert_not_called()

    def test_the_wrapper_accepts_plain_strings(self):
        fake_in = mock.MagicMock()
        fake_in.isatty.return_value = False
        with mock.patch.object(sys, "stdin", fake_in):
            self.assertIsNone(choose(_Off(), "t", ["a"]))


if __name__ == "__main__":
    unittest.main()
