"""Frame-buffer primitives: cell grid, styling, ANSI span parsing."""

from __future__ import annotations

import os
import sys
import unittest

from core.tui.buffer import Buffer, Renderer, parse_ansi_spans

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "."))
_tests_dir = os.path.dirname(os.path.abspath(__file__))
if _tests_dir not in sys.path:
    sys.path.insert(0, _tests_dir)


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




if __name__ == "__main__":  # pragma: no cover
    unittest.main()
