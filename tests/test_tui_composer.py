"""Composer widget: editing keys, completion popup, mouse mapping."""

from __future__ import annotations

import os
import sys
import unittest

from core.tui.backend import Backend, Key
from core.tui.composer import Composer
from core.tui.overlays import MenuOverlay, Option

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "."))
_tests_dir = os.path.dirname(os.path.abspath(__file__))
if _tests_dir not in sys.path:
    sys.path.insert(0, _tests_dir)


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

    def test_typing_replaces_the_selection(self):
        c = Composer()
        c.consume_paste("hello world")
        c.sel_anchor, c.sel_head, c.cursor = 6, 11, 11
        c.consume_key("x")
        self.assertEqual(c.buffer, "hello x")
        self.assertFalse(c.has_selection())

    def test_backspace_deletes_the_selection(self):
        c = Composer()
        c.consume_paste("hello world")
        c.sel_anchor, c.sel_head, c.cursor = 0, 6, 6
        c.consume_key("backspace")
        self.assertEqual(c.buffer, "world")
        self.assertEqual(c.cursor, 0)

    def test_caret_move_clears_the_selection(self):
        c = Composer()
        c.consume_paste("hello")
        c.sel_anchor, c.sel_head = 0, 5
        c.consume_key("left")
        self.assertFalse(c.has_selection())

    def test_mouse_offset_and_highlight_span(self):
        # Single-line box of height 2: content sits on rows_total - 2,
        # after the "│ MANTRA > " label (11 cells).
        c = Composer()
        c.consume_paste("hello")
        self.assertEqual(c.offset_at(22, 11, 40, 24, 2), 0)
        self.assertEqual(c.offset_at(22, 16, 40, 24, 2), 5)
        self.assertIsNone(c.offset_at(10, 11, 40, 24, 2))
        c.sel_anchor, c.sel_head = 1, 4
        spans = c.selection_spans(40, 24, 2)
        self.assertEqual(spans, [(22, 12, 15)])

    def test_mouse_offset_multiline(self):
        c = Composer()
        c.consume_paste("ab\ncde")
        rows = c.row_map(40, 24, 4)
        self.assertEqual([sy for sy, _, _, _ in rows], [21, 22])
        self.assertEqual(c.offset_at(21, 11, 40, 24, 4), 0)
        self.assertEqual(c.offset_at(22, 12, 40, 24, 4), 4)

    def test_pasted_enter_inserts_instead_of_submitting(self):
        # A burst of key-downs is a paste: its newlines insert, and the
        # prompt only submits on a lone, typed Enter.
        from types import SimpleNamespace

        def decode(uchar, vk, pasted, mods=frozenset()):
            key = SimpleNamespace(wVirtualKeyCode=vk, uChar=uchar, dwControlKeyState=0)
            return Backend()._win_key_events(key, mods, pasted)

        self.assertEqual(decode("\r", 0x0D, False), [Key("enter")])
        self.assertEqual(decode("\r", 0x0D, True), [Key("newline")])
        self.assertEqual(
            decode("\r", 0x0D, True, frozenset({"shift"})), [Key("newline", frozenset({"shift"}))]
        )
        self.assertEqual(decode("a", 0x41, True), [Key("a")])
        # End to end: the pasted newline lands in the buffer, nothing submits.
        c = Composer()
        for ev in decode("a", 0x41, True) + decode("\r", 0x0D, True) + decode("b", 0x42, True):
            c.consume_key(ev.key, ev.mods)
        self.assertEqual(c.buffer, "a\nb")
        self.assertIsNone(c.submitted)


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





if __name__ == "__main__":  # pragma: no cover
    unittest.main()
