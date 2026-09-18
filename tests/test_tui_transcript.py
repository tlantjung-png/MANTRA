"""Transcript pool: ingest, wrapping cache, follow/scroll semantics."""

from __future__ import annotations

import os
import sys
import unittest

from core.tui.transcript import Transcript, wrap_ansi

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "."))
_tests_dir = os.path.dirname(os.path.abspath(__file__))
if _tests_dir not in sys.path:
    sys.path.insert(0, _tests_dir)


class TranscriptTest(unittest.TestCase):
    def test_sanitize_ingest_keeps_sgr_esc_byte(self):
        # Regression: the control-char strip used to eat the ESC byte of
        # kept SGR sequences, leaving literal "[2m" codes that rendered
        # as text and never coloured the line.
        from core.tui.transcript import sanitize_ingest

        line = "\x1b[2m18:13\x1b[0m  Hello\n\x1b[1;38;5;167mENCHANTER\x1b[0m Hi\n"
        kept = sanitize_ingest(line)
        self.assertIn("\x1b[2m", kept)
        self.assertIn("\x1b[1;38;5;167m", kept)
        self.assertIn("\x1b[0m", kept)
        self.assertNotIn("\n[2m", kept)  # no ESC-less code text survives
        # Non-SGR escapes are still dropped entirely.
        self.assertEqual(sanitize_ingest("a\x1b[2Jb"), "ab")

    def test_partial_lines_commit_on_newline(self):
        t = Transcript()
        t.set_width(40)
        t.append_partial("hel")
        self.assertEqual(t.raw, [])
        t.append_partial("lo\nworld\n")
        self.assertEqual(t.raw, ["hello", "world"])
        self.assertEqual(t.partial, "")

    def test_wrapping_is_cached_per_width(self):
        t = Transcript()
        t.set_width(10)
        t.append("a" * 25)
        self.assertEqual(len(t.display), 3)
        t.set_width(30)
        self.assertEqual(len(t.display), 1)

    def test_follow_and_scroll_semantics(self):
        t = Transcript()
        t.set_width(40)
        t.viewport_height = 10
        for i in range(20):
            t.append(f"line {i}")
        self.assertEqual(t.offset, 0)
        t.scroll_up(3)
        self.assertEqual(t.offset, 3)
        self.assertFalse(t.follow)
        t.append("new line")
        self.assertEqual(t.offset, 3)  # detached: the tail does not drag
        # Scrolling past the end clamps instead of showing blank rows.
        t.scroll_up(500)
        self.assertEqual(t.offset, len(t.display) + (1 if t.partial else 0) - 10)
        t.scroll_down(20)
        self.assertTrue(t.follow)
        self.assertEqual(t.offset, 0)

    def test_wrap_ansi_keeps_styling_across_rows(self):
        lines = wrap_ansi("\033[1m" + "x" * 15 + "\033[0m", 10)
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[0].startswith("\033[1m"))
        self.assertTrue(lines[1].startswith("\033[1m"))




if __name__ == "__main__":  # pragma: no cover
    unittest.main()
