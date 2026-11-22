"""Headless snapshot tests: the busy-chip HTML preview renders correctly."""

from __future__ import annotations

import os
import tempfile
import unittest

from core.tui.snapshot import ansi_to_html, chip_rows, render_page


class AnsiToHtmlTest(unittest.TestCase):
    def test_bold_and_dim_become_spans(self):
        html = ansi_to_html("\033[1mE\033[0m")
        self.assertIn('font-weight:bold', html)
        self.assertIn(">E<", html)
        html_dim = ansi_to_html("\033[2m x \033[0m")
        self.assertIn('color:#8a8f98', html_dim)

    def test_rgb_style_is_preserved(self):
        html = ansi_to_html("\033[38;5;196mred\033[0m")
        self.assertIn("color:#ff0000", html)
        self.assertIn(">red<", html)

    def test_html_is_escaped(self):
        html = ansi_to_html("a <b> & \"c\"")
        self.assertIn("&lt;b&gt;", html)


class ChipSnapshotTest(unittest.TestCase):
    def test_chip_rows_cover_state_variants(self):
        labels = [label for label, _ in chip_rows()]
        self.assertEqual(len(labels), 6)
        self.assertIn("idle", labels[0])
        self.assertIn("busy", labels[1])
        self.assertIn("thinking", labels[2])
        self.assertIn("reading", labels[3])
        self.assertIn("writing", labels[4])
        self.assertIn("presenting", labels[5])

    def test_render_page_contains_themed_activity_chips(self):
        page = render_page()
        for word in ("Channeling", "Scrying", "Inscribing", "Manifesting"):
            self.assertIn(word, page)
        self.assertNotIn("ENCHANTER", page)
        self.assertIn("tok ·", page)        # live token counter survives
        self.assertIn('class="chip"', page)
        self.assertIn("<style>", page)      # styling shell present
        self.assertNotIn("LongCat-2.5", page)  # model is not in the chip label

    def test_cli_writes_html_file(self):
        from core.tui.snapshot import main

        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "chip.html")
            self.assertEqual(main(["--out", out]), 0)
            self.assertTrue(os.path.isfile(out))
            with open(out, encoding="utf-8") as handle:
                self.assertIn("MANTRA busy-chip snapshot", handle.read())

    def test_cli_requires_out_argument(self):
        from core.tui.snapshot import main

        self.assertEqual(main(["--out"]), 2)


if __name__ == "__main__":
    unittest.main()
