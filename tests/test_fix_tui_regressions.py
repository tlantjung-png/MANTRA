"""Regressions for the terminal-interface audit fixes.

Self-contained: the app tests use the scripted harness (fake backend, temp
workspace); the backend tests drive the parsers and pipes directly. No real
terminal, user home, or project log is touched.
"""

from __future__ import annotations

import base64
import os
import queue
import sys
import threading
import time
import unittest
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
_TESTS = os.path.dirname(os.path.abspath(__file__))
if _TESTS not in sys.path:
    sys.path.insert(0, _TESTS)

import core.tui.app as app_module  # noqa: E402
from core.tui.backend import Backend, Key, Paste, _PASTE_CAP, _PASTE_END, _PASTE_START  # noqa: E402
from core.tui.buffer import Buffer, Renderer, StyleTable, parse_ansi_spans  # noqa: E402
from core.tui.clipboard import copy_text  # noqa: E402
from core.tui.sessionpanel import SessionEntry, SessionPanelState  # noqa: E402
from core.tui.transcript import Transcript, _PARTIAL_CAP, sanitize_ingest  # noqa: E402

from tui_harness import _make_app, grid_rows, wait_until  # noqa: E402


def _drain(events) -> list:
    out = []
    while events.qsize():
        out.append(events.get_nowait())
    return out


class SpyLock:
    """An RLock that records depth and acquisitions for lock-scope asserts."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.depth = 0
        self.acquires = 0

    def __enter__(self):
        self._lock.acquire()
        self.depth += 1
        self.acquires += 1
        return self

    def __exit__(self, *exc) -> None:
        self.depth -= 1
        self._lock.release()


class High3ChunkProgressTest(unittest.TestCase):
    """High-3: an escape at offset zero may never stall the chunk loop."""

    def test_cap_straddling_escape_at_offset_zero_terminates(self):
        t = Transcript()
        t.set_width(80)
        done: list[object] = []
        # A complete CSI with a long parameter run: the match starts at
        # zero and ends just past the cap, which used to set cut to zero.
        payload = "\x1b[" + "1" * _PARTIAL_CAP + " m"

        def feed() -> None:
            try:
                t.append_partial(payload)
                done.append(True)
            except Exception as exc:  # pragma: no cover - surfaced below
                done.append(exc)

        thread = threading.Thread(target=feed, daemon=True)
        thread.start()
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive(), "chunking loop never consumed input")
        self.assertEqual(done, [True])
        self.assertTrue(t.raw, "no bounded chunk was committed")
        self.assertLessEqual(len(t.partial), _PARTIAL_CAP)

    def test_escape_starting_before_the_cap_is_not_split(self):
        t = Transcript()
        t.set_width(80)
        prefix = "a" * (_PARTIAL_CAP - 4)
        t.append_partial(prefix + "\x1b[31m" + "b" * 100)
        self.assertTrue(t.raw)
        self.assertEqual(t.raw[0], prefix)
        self.assertTrue(t.partial.startswith("\x1b[31m"))


class Medium10OscSanitizerTest(unittest.TestCase):
    """Medium-10: OSC bodies must not survive as transcript text."""

    def test_osc_payloads_are_dropped_whole(self):
        self.assertEqual(sanitize_ingest("a\x1b]0;secret-title\x07b"), "ab")
        self.assertEqual(sanitize_ingest("a\x1b]52;c;c2VjcmV0\x1b\\b"), "ab")
        # A payload ending in "m" must not be mistaken for an SGR code.
        self.assertEqual(sanitize_ingest("a\x1b]0;title m\x07b"), "ab")

    def test_sgr_is_still_kept_verbatim(self):
        self.assertEqual(
            sanitize_ingest("a\x1b[1mb\x1b[0m"), "a\x1b[1mb\x1b[0m"
        )


class Medium9IdlePasteTest(unittest.TestCase):
    """Medium-9: a paste split across an idle gap is not replayed as keys."""

    def test_idle_flush_keeps_a_partial_paste_pending(self):
        backend = Backend()
        backend._pending_at = time.monotonic() - 1.0
        pending = _PASTE_START + "ab\ncd"
        self.assertEqual(backend._idle_replay_posix(pending), pending)
        self.assertEqual(backend.events.qsize(), 0)

        # Completed later: the buffered body becomes one Paste event.
        remainder, _ = backend._parse_stream(pending + _PASTE_END, 0)
        self.assertEqual(remainder, "")
        self.assertEqual(_drain(backend.events), [Paste("ab\ncd")])

    def test_idle_flush_still_replays_incomplete_ordinary_escapes(self):
        backend = Backend()
        backend._pending_at = time.monotonic() - 1.0
        self.assertEqual(backend._idle_replay_posix("\x1b[1;5"), "")
        self.assertEqual(
            [e.key for e in _drain(backend.events)], ["esc", "[", "1", ";", "5"]
        )

    def test_split_paste_with_a_real_idle_gap_is_one_paste_event(self):
        backend = Backend()
        read_fd, write_fd = os.pipe()
        backend._fd = read_fd
        backend._winch_r = -1
        thread = threading.Thread(
            target=backend._read_loop_posix, name="tui-split-paste", daemon=True
        )
        thread.start()
        try:
            os.write(write_fd, (_PASTE_START + "first\n").encode())
            time.sleep(0.12)  # longer than the 0.05 idle-replay window
            os.write(write_fd, ("second" + _PASTE_END).encode())
            self.assertTrue(wait_until(lambda: backend.events.qsize() >= 1, 3))
            time.sleep(0.05)
            events = _drain(backend.events)
            self.assertEqual(len(events), 1, events)
            self.assertIsInstance(events[0], Paste)
            self.assertEqual(events[0].text, "first\nsecond")
        finally:
            backend._stop.set()
            try:
                os.write(write_fd, b" ")  # unblock a pending select/read
            except OSError:
                pass
            thread.join(timeout=2)
            os.close(write_fd)


class Medium11HistoryLockTest(unittest.TestCase):
    """Medium-11: history and recall index mutate under the submit lock."""

    @staticmethod
    def _spy_app():
        app, _, backend = _make_app([])
        spy = SpyLock()
        app.lock = spy
        app._history = _HistorySpy(spy)
        return app, backend, spy

    def test_history_append_and_index_update_happen_under_lock(self):
        app, _, spy = self._spy_app()
        original = app_module.dispatch_session
        app_module.dispatch_session = lambda s, line: True
        try:
            app.submit("first prompt")
            app.submit("second prompt")
        finally:
            app_module.dispatch_session = original
        self.assertEqual(list(app._history), ["first prompt", "second prompt"])
        self.assertEqual(app._history_idx, 2)
        self.assertTrue(app._history.append_depths)
        self.assertTrue(all(d > 0 for d in app._history.append_depths))
        self.assertGreaterEqual(spy.acquires, 2)

    def test_busy_submission_still_queues_and_records_history(self):
        app, _, _ = self._spy_app()
        app.busy = True
        app.submit("queued prompt")
        self.assertEqual(app.queued, "queued prompt")
        self.assertEqual(list(app._history), ["queued prompt"])


class _HistorySpy(list):
    def __init__(self, lock: SpyLock) -> None:
        super().__init__()
        self.lock = lock
        self.append_depths: list[int] = []

    def append(self, item) -> None:
        self.append_depths.append(self.lock.depth)
        super().append(item)


class Low6OverlayAndPagerLockTest(unittest.TestCase):
    """Low-6: overlay reply and pager truth checks use their locks."""

    def test_finish_and_stop_take_the_overlay_lock(self):
        app, _, _ = _make_app([])
        spy = SpyLock()
        app._overlay_lock = spy

        before = spy.acquires
        app._finish_overlay("answer")
        self.assertGreater(spy.acquires, before)
        self.assertIsNone(app._overlay_reply)

        before = spy.acquires
        app.stop()
        self.assertGreater(spy.acquires, before)

    def test_concurrent_prompts_stay_serialized_and_answerable(self):
        app, _, _ = _make_app([])
        answers: dict[int, str] = {}

        def first() -> None:
            answers[1] = app.ask_line("one")

        def second() -> None:
            answers[2] = app.ask_line("two")

        t1 = threading.Thread(target=first, daemon=True)
        t1.start()
        self.assertTrue(wait_until(lambda: app.overlay is not None, 2))
        overlay = app.overlay
        t2 = threading.Thread(target=second, daemon=True)
        t2.start()
        time.sleep(0.05)
        self.assertIs(app.overlay, overlay)  # gate holds the second prompt

        app._finish_overlay("a")
        self.assertTrue(wait_until(lambda: answers.get(1) == "a", 2))
        self.assertTrue(
            wait_until(lambda: app.overlay is not None and app.overlay is not overlay, 2)
        )
        app._finish_overlay("b")
        t1.join(timeout=2)
        t2.join(timeout=2)
        self.assertEqual(answers, {1: "a", 2: "b"})

    def test_empty_enter_checks_the_pager_under_its_lock(self):
        app, session, _ = _make_app([])
        spy = SpyLock()
        session._pager_lock = spy
        pages = _BoolSpy(spy)
        pages.append(("title", ["line"]))
        session._pending_pages = pages
        called: list[bool] = []
        session.page_next = lambda: called.append(True)  # type: ignore[assignment]

        app.handle_event(Key("enter"))
        self.assertEqual(called, [True])
        self.assertTrue(pages.bool_depths)
        self.assertTrue(all(d > 0 for d in pages.bool_depths))


class _BoolSpy(list):
    def __init__(self, lock: SpyLock) -> None:
        super().__init__()
        self.lock = lock
        self.bool_depths: list[int] = []

    def __bool__(self) -> bool:
        self.bool_depths.append(self.lock.depth)
        return len(self) > 0


class Low7ToolTextReaderTest(unittest.TestCase):
    """Low-7: the cross-thread collector has a reader under the app lock."""

    def test_take_turn_tool_text_drains_under_lock(self):
        app, _, _ = _make_app([])
        spy = SpyLock()
        app.lock = spy
        collector = _IterSpy(spy)
        collector.append("read_file: ok")
        app._turn_tool_text = collector

        self.assertEqual(app.take_turn_tool_text(), ["read_file: ok"])
        self.assertEqual(app._turn_tool_text, [])
        self.assertTrue(collector.iter_depths)
        self.assertTrue(all(d > 0 for d in collector.iter_depths))

    def test_turn_teardown_consumes_worker_collected_text(self):
        app, _, _ = _make_app([])
        original = app_module.dispatch_session

        def dispatch(session, line):
            session.ui._turn_tool_text.append("run_command: ok")
            return True

        app_module.dispatch_session = dispatch
        try:
            app.submit("go")
            self.assertTrue(
                wait_until(lambda: bool(app._last_turn_tool_text), 3)
            )
        finally:
            app_module.dispatch_session = original
        self.assertEqual(app._last_turn_tool_text, ["run_command: ok"])
        self.assertEqual(app._turn_tool_text, [])


class _IterSpy(list):
    def __init__(self, lock: SpyLock) -> None:
        super().__init__()
        self.lock = lock
        self.iter_depths: list[int] = []

    def __iter__(self):
        self.iter_depths.append(self.lock.depth)
        return super().__iter__()


class Low11OscStyleTest(unittest.TestCase):
    """Low-11: an OSC match must not reset the active style."""

    def test_osc_does_not_clear_bold(self):
        table = StyleTable()
        spans = parse_ansi_spans("\x1b[1mbold\x1b]0;title\x07plain", table)
        self.assertEqual("".join(s[0] for s in spans), "boldplain")
        self.assertTrue(all(table.params_for(s[1]) == ("1",) for s in spans))

    def test_osc_with_st_terminator_does_not_clear_bold(self):
        table = StyleTable()
        spans = parse_ansi_spans("\x1b[1ma\x1b]0;t\x1b\\b", table)
        self.assertEqual("".join(s[0] for s in spans), "ab")
        self.assertTrue(all(table.params_for(s[1]) == ("1",) for s in spans))


class Low12WinchHandlerTest(unittest.TestCase):
    """Low-12: teardown restores the previous resize handler."""

    @unittest.skipUnless(os.name != "nt", "POSIX signal API")
    def test_previous_sigwinch_handler_is_restored(self):
        import signal

        backend = Backend()

        def custom(signum, frame):
            pass

        previous = signal.getsignal(signal.SIGWINCH)
        signal.signal(signal.SIGWINCH, custom)
        try:
            backend._enter_posix()
            self.assertEqual(signal.getsignal(signal.SIGWINCH), backend._on_winch)
            backend._leave_posix()
            self.assertEqual(signal.getsignal(signal.SIGWINCH), custom)
        finally:
            backend._leave_posix()
            signal.signal(signal.SIGWINCH, previous)


class Low13PasteMarkerTest(unittest.TestCase):
    """Low-13: full marker match and no silent loss past the cap."""

    def test_csi_starting_with_200_is_not_a_paste(self):
        backend = Backend()
        backend._parse_stream("\x1b[200A", 0)
        self.assertEqual(_drain(backend.events), [Key("up")])

    def test_bytes_after_the_cap_are_decoded(self):
        backend = Backend()
        seq = _PASTE_START + "x" * (_PASTE_CAP + 50) + "Z"
        backend._parse_stream(seq, 0)
        events = _drain(backend.events)
        self.assertIsInstance(events[0], Paste)
        self.assertEqual(events[0].text, "x" * _PASTE_CAP)
        self.assertEqual(sum(e == Key("x") for e in events[1:]), 50)
        self.assertEqual(events[-1], Key("Z"))


class Low14CursorFrameTest(unittest.TestCase):
    """Low-14: a cursor placed by a normal frame is cleared for modals."""

    def test_reset_cursor_hides_it_on_flush(self):
        class Rec:
            def __init__(self):
                self.chunks: list[str] = []

            def write(self, text):
                self.chunks.append(text)

        rec = Rec()
        renderer = Renderer(rec, 20, 3)
        renderer.set_cursor(4, 2)
        renderer.reset_cursor()
        renderer.flush()
        out = "".join(rec.chunks)
        self.assertIn("\033[?25l", out)
        self.assertNotIn("\033[3;5H", out)

    def test_modal_frame_drops_the_previous_cursor(self):
        app, _, backend = _make_app([])
        app.render_frame()
        self.assertIsNotNone(app.renderer._cursor)
        backend.writes.clear()

        app.session_panel = SessionPanelState(
            entries=[SessionEntry(name="one"), SessionEntry(name="two")]
        )
        app.render_frame()
        self.assertIsNone(app.renderer._cursor)
        self.assertIn("\033[?25l", "".join(backend.writes))


class Low15WidthBoundTest(unittest.TestCase):
    """Low-15: max_width is an absolute column bound everywhere."""

    def test_set_str_stops_at_the_absolute_bound(self):
        buf = Buffer(10, 1, StyleTable())
        self.assertEqual(buf.set_str(3, 0, "abcdefgh", max_width=6), 6)
        self.assertEqual("".join(buf.chars), "   abc    ")

    def test_set_spans_stops_at_the_absolute_bound(self):
        buf = Buffer(10, 1, StyleTable())
        spans = parse_ansi_spans("abcdef", StyleTable())
        self.assertEqual(buf.set_spans(2, 0, spans, max_width=5), 5)
        self.assertEqual("".join(buf.chars), "  abc     ")


class Low16SessionPanelClampTest(unittest.TestCase):
    """Low-16: the session clamp counts entries, not screen rows."""

    def test_clamp_uses_the_entry_viewport(self):
        app, _, _ = _make_app([])
        app.session_panel = SessionPanelState(
            entries=[SessionEntry(name=f"s{i}") for i in range(40)]
        )
        app.session_panel.offset = 35
        app.render_frame()
        entry_viewport = max(1, max(2, app.renderer.buffer.rows - 2) // 2)
        self.assertEqual(app.session_panel.offset, 40 - entry_viewport)
        rows = grid_rows(app.renderer.buffer)
        self.assertIn("s26", rows[1])
        self.assertNotIn("s12", " | ".join(rows))


class Low17ClipboardWriterTest(unittest.TestCase):
    """Low-17: the OSC 52 fallback goes through the backend's writer."""

    def test_copy_text_routes_osc52_through_supplied_writer(self):
        written: list[str] = []
        with mock.patch("core.tui.clipboard._copy_windows", return_value=False), \
             mock.patch("core.tui.clipboard._copy_posix", return_value=False):
            copy_text("hello", written.append)
        self.assertEqual(len(written), 1)
        payload = base64.b64encode(b"hello").decode("ascii")
        self.assertEqual(written[0], f"\033]52;c;{payload}\x07")

    def test_app_copy_uses_the_backend_writer(self):
        from core.tui.selection import Anchor

        app, _, backend = _make_app([])
        with app.lock:
            app.transcript.append("hello world")
        app.render_frame()
        original = app_module.copy_text
        app.selection.anchor = Anchor(0, 0)
        app.selection.head = Anchor(0, 5)
        app.selection.active = True
        seen: dict[str, object] = {}

        def spy(text, writer=None):
            seen["writer"] = writer
            return original(text, writer) if writer is not None else original(text)

        app_module.copy_text = spy
        try:
            with mock.patch("core.tui.clipboard._copy_windows", return_value=False), \
                 mock.patch("core.tui.clipboard._copy_posix", return_value=False):
                app.handle_event(Key("ctrl+y"))
        finally:
            app_module.copy_text = original
        self.assertEqual(seen.get("writer"), backend.write)
        self.assertTrue(any("\033]52;c;" in chunk for chunk in backend.writes))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
