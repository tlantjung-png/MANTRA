"""Regression tests for the core/tui remediation pass (defects D1..D26).

Each test pins one fixed defect so a future refactor cannot silently
reintroduce it. Helpers are reused from tests/test_tui.py (FakeBackend,
_make_app, grid_rows, wait_until); the backend is faked, so no real
terminal is involved.
"""

from __future__ import annotations

import queue
import unittest

import core.tui.app as app_module
from core.tui.backend import Key, Paste
from core.tui.buffer import Renderer
from core.tui.loop import Presenter
from core.tui.overlays import LinePrompt, QuestionCard
from core.tui.sessionpanel import SessionEntry, SessionPanelState, render_session_panel
from core.tui.transcript import Transcript, sanitize_ingest

from tests.tui_harness import _make_app, grid_rows, wait_until


class RecordingWriter:
    def __init__(self):
        self.chunks: list[str] = []

    def write(self, text: str) -> None:
        self.chunks.append(text)


class D1TabSanitizationTest(unittest.TestCase):
    """Raw tabs must never reach the terminal."""

    def test_sanitize_ingest_expands_tabs_to_spaces(self):
        self.assertEqual(sanitize_ingest("a\tb"), "a    b")

    def test_flush_emits_no_raw_tab_or_control_bytes(self):
        # A literal tab placed straight into a cell must be skipped, and
        # only ESC (the renderer's own escapes) may be a control byte.
        rec = RecordingWriter()
        renderer = Renderer(rec, 30, 3)
        renderer.buffer.set_str(0, 0, "a\tb\x07\x01")
        renderer.flush()
        out = "".join(rec.chunks)
        self.assertNotIn("\t", out)
        self.assertNotIn("\x07", out)
        for ch in out:
            if ord(ch) < 32 and ch != "\x1b":
                self.fail(f"raw control byte {ord(ch):#x} in flush output")

    def test_tab_in_content_never_reaches_the_terminal(self):
        app, session, backend = _make_app([])
        with app.lock:
            app.transcript.append("code\twith\ttabs")
        app.render_frame()
        out = "".join(backend.writes)
        self.assertNotIn("\t", out)


class D2FrameRecoveryTest(unittest.TestCase):
    """A failing render_frame must keep the repaint request armed."""

    def test_loop_recovers_after_a_frame_failure(self):
        class _LoopApp:
            running = True
            dirty = True

            def __init__(self):
                self.fail_first_frame = True
                self.frames = 0
                self.repaints = 0
                self._polls = 0

            def next_event(self, timeout):
                # Safety valve: end the loop (like a Ctrl+C) once the
                # recovery path has had its chance, so a regression
                # cannot hang the suite.
                self._polls += 1
                if self._polls > 5:
                    raise KeyboardInterrupt
                raise queue.Empty

            def needs_animation(self):
                return False

            def tick_animation(self):
                pass

            def mark_dirty(self):
                self.dirty = True

            def apply_pending_resize(self):
                return False

            def render_frame(self):
                self.frames += 1
                if self.fail_first_frame:
                    self.fail_first_frame = False
                    raise RuntimeError("frame boom")
                self.running = False

            def force_full_repaint(self):
                self.repaints += 1
                self.mark_dirty()

        app = _LoopApp()
        Presenter(app, min_draw_interval=0.0).run()
        self.assertEqual(app.frames, 2)   # the failed frame is retried
        self.assertEqual(app.repaints, 1)  # full repaint was requested
        self.assertFalse(app.dirty)        # cleared only after a good draw


class D3SessionPanelUnitsTest(unittest.TestCase):
    """The session panel must slice entries by the row budget."""

    def test_render_slices_entries_by_row_budget(self):
        state = SessionPanelState(
            entries=[SessionEntry(name=f"s{i}") for i in range(40)]
        )
        frame = render_session_panel(state, 80, 30)
        # Body area is 28 rows and each entry renders 2 rows.
        self.assertEqual(len(frame.rows), 28)
        self.assertEqual(len(frame.rows) // 2, 14)

    def test_scroll_thresholds_keep_the_selection_visible(self):
        app, session, backend = _make_app([])
        app.cols, app.rows = 80, 30
        app.session_panel = SessionPanelState(
            entries=[SessionEntry(name=f"s{i}") for i in range(40)]
        )
        for _ in range(20):
            app.handle_event(Key("j"))
        panel = app.session_panel
        # j moves the cursor down and scrolls the offset to keep it
        # visible. After 20 j's, the cursor sits at entry 20 (clamped
        # before total) and the offset has rolled forward so the
        # selected entry is on screen.
        self.assertEqual(panel.index, 20)
        self.assertEqual(panel.offset, 20 - (app.rows - 2) // 2 + 1)
        app.render_frame()
        grid = grid_rows(app.renderer.buffer)
        self.assertTrue(
            any("> s20" in row for row in grid),
            "selected entry scrolled off-screen",
        )


class D4OffsetClampTest(unittest.TestCase):
    """Offset must be clamped after rewrap so the viewport is not blank."""

    def test_offset_is_clamped_after_rewrap(self):
        t = Transcript()
        t.set_width(20)
        t.viewport_height = 10
        for _ in range(20):
            t.append("x" * 100)
        t.scroll_up(10**6)
        self.assertEqual(t.offset, 90)  # 100 display rows minus viewport
        t.set_width(50)
        # 40 display rows now; the offset cannot exceed the pool.
        self.assertLessEqual(t.offset, max(0, len(t.display) - 10))
        rows = t.row_source(10)
        self.assertEqual(len(rows), 10)


class D5PasteRoutingTest(unittest.TestCase):
    """A paste while a modal overlay is open must not reach the composer."""

    def test_paste_is_dropped_while_a_modal_overlay_is_open(self):
        app, session, backend = _make_app([])
        app.overlay = QuestionCard("allow?", "may I write files?")
        app.handle_event(Paste("pasted\nline"))
        self.assertEqual(app.composer.buffer, "")

    def test_line_prompt_still_receives_pastes(self):
        app, session, backend = _make_app([])
        app.overlay = LinePrompt("api key?", secret=True)
        app.handle_event(Paste("sekret-value"))
        self.assertEqual(app.overlay.buffer, "sekret-value")


class Def03ModelKeyRedactionTest(unittest.TestCase):
    """Plaintext API keys in /model lines must not echo or enter history."""

    def test_model_line_echo_and_history_redact_keys(self):
        original = app_module.dispatch_session
        calls: list[str] = []
        app_module.dispatch_session = lambda s, line: calls.append(line) or True
        app, session, backend = _make_app([])
        try:
            app.submit("/model https://api.openai.com/v1 sk-secret-123 gpt-4o")
            self.assertTrue(wait_until(lambda: bool(calls), 5))
            self.assertNotIn("sk-secret-123", app.transcript.raw[0])
            self.assertIn("****", app.transcript.raw[0])
            self.assertNotIn("sk-secret-123", " ".join(app._history))
            # The executed command still carries the real key.
            self.assertEqual(calls[0], "/model https://api.openai.com/v1 sk-secret-123 gpt-4o")

            app.submit("/model key groq gsk-other-secret")
            joined = " ".join(app.transcript.raw)
            self.assertNotIn("gsk-other-secret", joined)
            self.assertNotIn("gsk-other-secret", " ".join(app._history))
        finally:
            app_module.dispatch_session = original


if __name__ == "__main__":
    unittest.main()
