"""Tests for the full-screen diff review surface (renderer + state)."""

import re
import unittest

from core.diffparse import parse_diff
from core.tui.review import ReviewState, render_review

DIFF = """diff --git a/src/app.py b/src/app.py
--- a/src/app.py
+++ b/src/app.py
@@ -10,4 +10,5 @@ def main():
     return run()
 
-old line one
-old line two
+new line one
+new line two
+extra line
@@ -40,2 +41,2 @@ def other():
-removed
+added
 end context
diff --git a/docs/guide.md b/docs/guide.md
--- /dev/null
+++ b/docs/guide.md
@@ -0,0 +1,2 @@
+# Guide
+intro
"""


def _plain(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


def _files():
    return parse_diff(DIFF)


class ReviewStateTest(unittest.TestCase):
    def test_sidebar_marks_the_selected_file(self):
        state = ReviewState(files=_files(), index=1)
        frame = render_review(state, 120, 20)
        texts = [_plain(r.text) for r in frame.sidebar]
        self.assertTrue(any(t.startswith("> ") and "docs/guide.md" in t for t in texts))
        self.assertTrue(any(t.startswith("  ") and "src/app.py" in t for t in texts))
        self.assertEqual(frame.header.count("2/2"), 1)

    def test_stacked_rows_show_gutters_and_hunk_separators(self):
        state = ReviewState(files=_files(), split=False)
        frame = render_review(state, 100, 30)
        texts = [_plain(r.text) for r in frame.body]
        self.assertIn("@@ -10,4 +10,5 @@", texts)
        self.assertTrue(any(t.startswith("  10   10 ") for t in texts))  # context line
        self.assertTrue(any("extra line" in t for t in texts))  # added line present
        self.assertTrue(any("old line one" in t for t in texts))  # removed line present
        self.assertFalse(state.split)

    def test_split_pairs_removed_and_added_side_by_side(self):
        state = ReviewState(files=_files(), split=True)
        frame = render_review(state, 140, 30)
        self.assertTrue(frame.split)
        # removed on the left, added on the right, on the same row
        pairs = [_plain(r.text) for r in frame.body]
        joined = "\n".join(pairs)
        self.assertIn("old line one", joined)
        self.assertIn("new line one", joined)

    def test_next_file_wraps_and_resets_offset(self):
        state = ReviewState(files=_files())
        state.offset = 5
        state.next_file(1)
        self.assertEqual(state.index, 1)
        self.assertEqual(state.offset, 0)
        state.next_file(-1)
        self.assertEqual(state.index, 0)


class ReviewAppIntegrationTest(unittest.TestCase):
    """The app opens the review via the layout bridge and navigates."""

    def test_bridge_opens_and_escape_closes(self):
        from core.tui.app import Key
        from tests.test_tui import FakeBackend, _make_app

        app, session, backend = _make_app([])
        self.assertIsNone(app.review)
        session.layout.open_review(DIFF)
        self.assertIsNotNone(app.review)
        self.assertEqual(app.review.index, 0)
        app.render_frame()
        app.handle_event(Key("down"))
        app.handle_event(Key("down"))
        app.handle_event(Key("tab"))
        self.assertEqual(app.review.index, 1)
        app.handle_event(Key("h"))
        self.assertEqual(app.review.index, 0)
        app.handle_event(Key("q"))
        self.assertIsNone(app.review)


if __name__ == "__main__":
    unittest.main()
