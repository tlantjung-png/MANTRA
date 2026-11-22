"""Headless harness tests: the full TUI renders into inspectable grids."""

from __future__ import annotations

import os
import tempfile
import unittest

from core.tui.harness import build, chat_state, grid_ansi, grid_text, main, render_page


class HarnessRenderTest(unittest.TestCase):
    def setUp(self):
        self.app, self.session, self.backend = build()

    def test_chat_frame_renders_full_ui(self):
        chat_state(self.app)
        self.app.render_frame()
        grid = grid_text(self.app)
        # The whole screen is painted: top bar, transcript, chip, prompt.
        self.assertIn("WORKSPACE:", grid)
        self.assertIn("MANTRA >", grid)
        self.assertIn("Enchanting", grid)
        self.assertIn("1234 tok", grid)
        self.assertNotIn("ENCHANTER", grid)

    def test_ansi_grid_carries_style_escapes(self):
        chat_state(self.app)
        self.app.render_frame()
        ansi = grid_ansi(self.app)
        self.assertIn("\033[", ansi)
        self.assertIn("Enchanting", ansi)

    def test_render_page_produces_browser_html(self):
        chat_state(self.app)
        self.app.render_frame()
        page = render_page(
            grid_ansi(self.app),
            "MANTRA TUI — headless render",
            "note",
        )
        self.assertIn("<!doctype html>", page)
        self.assertIn("Enchanting", page)
        self.assertIn('<div class="screen">', page)

    def test_cli_writes_preview_and_grid(self):
        with tempfile.TemporaryDirectory() as tmp:
            code = main(["--out", tmp, "--cols", "100", "--rows", "28"])
            self.assertEqual(code, 0)
            preview = os.path.join(tmp, "preview.html")
            grid = os.path.join(tmp, "grid.txt")
            self.assertTrue(os.path.isfile(preview))
            self.assertTrue(os.path.isfile(grid))
            with open(preview, encoding="utf-8") as handle:
                self.assertIn("Enchanting", handle.read())
            with open(grid, encoding="utf-8") as handle:
                self.assertIn("MANTRA >", handle.read())

    def test_cli_rejects_unknown_option(self):
        self.assertEqual(main(["--bogus"]), 2)


if __name__ == "__main__":
    unittest.main()
