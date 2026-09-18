"""Mouse selection model: click, drag, small-motion suppression."""

from __future__ import annotations

import os
import sys
import unittest

from core.tui.selection import Selection

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "."))
_tests_dir = os.path.dirname(os.path.abspath(__file__))
if _tests_dir not in sys.path:
    sys.path.insert(0, _tests_dir)


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




if __name__ == "__main__":  # pragma: no cover
    unittest.main()
