"""Resize and reflow: the frame rebuilt after a window size change,
and the welcome card re-centering with it."""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "."))
_tests_dir = os.path.dirname(os.path.abspath(__file__))
if _tests_dir not in sys.path:
    sys.path.insert(0, _tests_dir)
from core.tui.backend import Resize  # noqa: E402

from tui_harness import _make_app, app_module, grid_rows, wait_until  # noqa: E402


class ResizeReflowTest(unittest.TestCase):
    def tearDown(self):
        from tui_harness import _cleanup_temp_workspaces

        _cleanup_temp_workspaces()

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

class WelcomeCardResizeTest(unittest.TestCase):
    def tearDown(self):
        from tui_harness import _cleanup_temp_workspaces

        _cleanup_temp_workspaces()

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


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
