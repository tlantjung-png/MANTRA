"""Tests for the full-screen diff review surface (renderer + state)."""

import os
import re
import tempfile
import time
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
        from tests.tui_harness import FakeBackend, _make_app

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

    def test_wheel_scrolls_the_review(self):
        # The review owns the whole screen while it is open; mouse wheels
        # used to be dropped on the floor there, leaving the diff
        # unscrollable with the mouse.
        from core.tui.app import Mouse
        from core.tui.backend import _WHEEL_DOWN, _WHEEL_UP
        from tests.tui_harness import _make_app

        added = "".join(f"+line {i:03d}\n" for i in range(80))
        big = (
            "diff --git a/big.py b/big.py\n--- a/big.py\n+++ b/big.py\n"
            f"@@ -1,1 +1,80 @@\n{added}"
        )
        app, session, _ = _make_app([])
        session.layout.open_review(big)
        app.render_frame()
        review = app.review
        self.assertEqual(review.offset, 0)

        app.handle_event(Mouse("wheel", _WHEEL_DOWN, 5, 5, frozenset()))
        scrolled = review.offset
        self.assertGreater(scrolled, 0, "wheel-down did not scroll the review")
        app.handle_event(Mouse("wheel", _WHEEL_UP, 5, 5, frozenset()))
        self.assertLess(review.offset, scrolled, "wheel-up did not scroll back")
        self.assertEqual(review.offset, 0)


class SessionPanelTest(unittest.TestCase):
    """The session manager panel lists, renders, navigates and resumes."""

    def _isolate(self):
        store = tempfile.mkdtemp(prefix="mantra-sessions-")
        prior = os.environ.get("MANTRA_SESSIONS")
        os.environ["MANTRA_SESSIONS"] = store
        # addCleanup is LIFO: register the setdefault BEFORE the pop so the
        # pop runs first and the prior value is restored while the key is absent.
        if prior is not None:
            self.addCleanup(os.environ.setdefault, "MANTRA_SESSIONS", prior)
        self.addCleanup(os.environ.pop, "MANTRA_SESSIONS", None)
        return store

    def test_bridge_opens_and_lists_saved_sessions(self):
        from core.agent import sessions
        from tests.tui_harness import _make_app

        self._isolate()
        sessions.save("alpha", {"workspace": "C:/x/a", "model": "gpt-4o",
                                "messages": [{"role": "user", "content": "hi"}]})
        app, session, _ = _make_app([])
        session.layout.open_sessions()
        self.assertIsNotNone(app.session_panel)
        self.assertTrue(any(e.name == "alpha" for e in app.session_panel.entries))

    def test_panel_renders_rows_and_closes_on_escape(self):
        import re

        from core.agent import sessions
        from core.tui.app import Key
        from tests.tui_harness import _make_app

        self._isolate()
        sessions.save("alpha", {"workspace": "C:/x/a", "model": "gpt-4o",
                                "messages": [{"role": "user", "content": "fix the build"}]})
        app, session, backend = _make_app([])
        session.layout.open_sessions()
        app.render_frame()
        plain = re.sub(r"\x1b\[[0-9;]*m", "", "".join(backend.writes))
        self.assertIn("sessions (1)", plain)
        self.assertIn("alpha", plain)
        self.assertIn("Enter resume", plain)
        app.handle_event(Key("esc"))
        self.assertIsNone(app.session_panel)

    def test_enter_resumes_the_selected_session(self):
        from core.agent import sessions
        from core.tui.app import Key
        from tests.tui_harness import _make_app

        self._isolate()
        sessions.save("alpha", {"workspace": "C:/x/a", "model": "gpt-4o",
                                "messages": [{"role": "user", "content": "hi"}]})
        app, session, _ = _make_app([])
        session.layout.open_sessions()
        captured = []
        app.session_panel_on_enter = lambda name: captured.append(name)
        app.handle_event(Key("enter"))
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and not captured:
            time.sleep(0.02)
        self.assertEqual(captured, ["alpha"])
        self.assertIsNone(app.session_panel)

    def test_wheel_moves_the_panel_selection(self):
        # Mouse events reaching the app while the panel owns the screen
        # used to be dropped, so the list could not be wheeled.
        from core.agent import sessions
        from core.tui.app import Mouse
        from core.tui.backend import _WHEEL_DOWN, _WHEEL_UP
        from tests.tui_harness import _make_app

        self._isolate()
        for name in ("alpha", "beta", "gamma"):
            sessions.save(name, {"workspace": f"C:/x/{name}", "model": "gpt-4o",
                                 "messages": [{"role": "user", "content": "hi"}]})
        app, session, _ = _make_app([])
        session.layout.open_sessions()
        app.render_frame()
        panel = app.session_panel
        self.assertIsNotNone(panel)
        self.assertEqual(panel.index, 0)
        app.handle_event(Mouse("wheel", _WHEEL_DOWN, 5, 5, frozenset()))
        self.assertEqual(panel.index, 1, "wheel-down did not move the selection")
        app.handle_event(Mouse("wheel", _WHEEL_UP, 5, 5, frozenset()))
        self.assertEqual(panel.index, 0, "wheel-up did not move the selection back")


if __name__ == "__main__":
    unittest.main()
