"""Surface behaviour tests: popup, modal cards, wheel and clicks.

These exercise the *interaction surfaces* of the terminal application — the
completion dropdown, the menu/approval/key cards, the scrollbar, and how a
cramped frame degrades — rather than the core widgets (buffer, transcript,
composer, backend decoding) that ``test_tui.py`` covers. They share its
fakes, so the harness stays in one place.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "."))
_tests_dir = os.path.dirname(os.path.abspath(__file__))
if _tests_dir not in sys.path:
    sys.path.insert(0, _tests_dir)

from core.tui.app import TuiApp  # noqa: E402
from core.tui.backend import Key, Mouse  # noqa: E402
from core.tui.composer import Composer  # noqa: E402
from core.tui.overlays import LinePrompt, MenuOverlay, Option, QuestionCard  # noqa: E402

from _helpers import make_session  # noqa: E402
from tests.tui_harness import FakeBackend, _TEMP_WORKSPACES, _make_app, grid_rows, wait_until  # noqa: E402


def _thumb_row_at(app) -> int:
    """Row of the scrollbar thumb's middle (a drag grab point)."""
    _track_x, top, _height, thumb_top, thumb_span = app._scrollbar
    return top + thumb_top + thumb_span // 2


class _FakeCompleter:
    """A completer with a long list, so the popup always overflows."""

    def __init__(self, count: int = 60) -> None:
        self.count = count

    def complete(self, buffer: str, cursor: int):
        from core.tui.composer import Completion

        if buffer.startswith("/"):
            return Completion(
                items=[f"/cmd-{i}" for i in range(self.count)], start=0, end=cursor
            )
        return None


class _SurfaceTest(unittest.TestCase):
    """Shared cleanup for the temp workspaces ``_make_app`` hands out."""

    def tearDown(self):
        for ws in list(_TEMP_WORKSPACES):
            shutil.rmtree(ws, ignore_errors=True)
        _TEMP_WORKSPACES.clear()


def _workspace(tag: str) -> str:
    """A fresh temp workspace, registered for the shared atexit cleanup."""
    path = os.path.join(
        os.environ.get("TEMP", tempfile.gettempdir()),
        f"mantra-{tag}-{os.getpid()}-{threading.get_ident()}",
    )
    os.makedirs(path, exist_ok=True)
    _TEMP_WORKSPACES.append(path)
    return path


class CompletionPopupTest(_SurfaceTest):
    """The ``/`` and ``@`` dropdown: geometry, dismissal and the wheel."""

    def test_esc_dismisses_popup_and_wheel_scrolls_afterwards(self):
        # Regression: the app-level ESC handler returned before the
        # composer ever saw the key, so a '/'-completion popup could not
        # be dismissed and its rect kept swallowing wheel events over
        # the conversation. ESC must close the popup, and wheeling must
        # scroll the transcript again afterwards.
        from core.console import ConsoleCompleter
        from core.scripted import final_response

        session = make_session(_workspace("escpopup"), [final_response("hi")])
        app = TuiApp(session, backend=FakeBackend())
        app._init_surface()
        app._show_welcome()
        app.composer.completer = ConsoleCompleter(session)
        with app.lock:
            for i in range(60):
                app.transcript.append(f"line {i:02d}")
        app.render_frame()

        app.handle_event(Key("/"))
        self.assertTrue(app.composer.popup_open, "popup did not open on '/'")

        app.handle_event(Key("esc"))
        self.assertFalse(app.composer.popup_open, "ESC did not dismiss the popup")
        self.assertIsNone(app._popup_rect)

        before = app.transcript.scrolled
        app.handle_event(Mouse("wheel", 64, 5, 5, frozenset()))
        self.assertGreater(
            app.transcript.scrolled,
            before,
            "wheel did not scroll the transcript after dismissing the popup",
        )

    def test_wheel_scrolls_the_transcript_while_a_popup_is_open(self):
        # Regression: the completion dropdown is a tall floating box over
        # the lower half of the conversation, and it used to claim every
        # wheel event landing inside its rect. Typing "/" or "@" then left
        # the transcript unscrollable near the prompt - where the mouse
        # usually sits. The wheel belongs to the conversation; the list is
        # paged by keyboard or by clicking it.
        from core.console import ConsoleCompleter
        from core.scripted import final_response

        workspace = _workspace("wheelpopup")
        with open(os.path.join(workspace, "note.txt"), "w", encoding="utf-8") as fh:
            fh.write("x\n")
        session = make_session(workspace, [final_response("hi")])
        app = TuiApp(session, backend=FakeBackend(cols=100, rows=24))
        app._init_surface()
        app._show_welcome()
        app.composer.completer = ConsoleCompleter(session)
        with app.lock:
            for i in range(120):
                app.transcript.append(f"line {i:03d}")
        for ch in "@no":
            app.handle_event(Key(ch))
        app.render_frame()
        self.assertTrue(app.composer.popup_open, "popup did not open on '@'")
        rect = app._popup_rect
        self.assertIsNotNone(rect, "no popup rect recorded")
        x, y, _w, h = rect
        composer_top = app.rows - app._composer_height(app.rows)
        for probe_y in (y + 1, (2 * y + h) // 2, composer_top, app.rows - 1):
            before = app.transcript.scrolled
            app.handle_event(Mouse("wheel", 64, x + 2, probe_y, frozenset()))
            self.assertGreater(
                app.transcript.scrolled,
                before,
                f"wheel at y={probe_y} (popup rows {y}..{y + h - 1}) did not scroll",
            )

    def test_popup_floats_clear_of_the_status_line_and_composer(self):
        # Regression: the dropdown anchored its bottom border on the
        # composer's input row and pushed its last item row onto the
        # status line, so the text being typed was hidden behind it.
        from core.console import ConsoleCompleter
        from core.scripted import final_response

        workspace = _workspace("popupfloat")
        for name in ("note.txt", "notes.md"):
            with open(os.path.join(workspace, name), "w", encoding="utf-8") as fh:
                fh.write("x\n")
        session = make_session(workspace, [final_response("hi")])
        app = TuiApp(session, backend=FakeBackend(cols=100, rows=24))
        app._init_surface()
        app._show_welcome()
        app.composer.completer = ConsoleCompleter(session)
        for ch in "@no":
            app.handle_event(Key(ch))
        app.render_frame()

        rect = app._popup_rect
        self.assertIsNotNone(rect, "no popup rect recorded")
        x, y, _w, h = rect
        composer_top = app.rows - app._composer_height(app.rows)
        border_row = composer_top - 1
        self.assertLessEqual(
            y + h,
            border_row,
            "popup overlaps the status line or the composer box",
        )
        rows = grid_rows(app.renderer.buffer)
        self.assertIn("@no", rows[composer_top], "the typed prompt is hidden by the popup")

        # The hit map must follow the box: clicking the second item row
        # selects the second completion (item rows start one below the
        # box's top border).
        second_row = y + 2
        self.assertIn(second_row, app._popup_hits)
        app.handle_event(Mouse("press", 0, x + 2, second_row, frozenset()))
        self.assertEqual(app.composer.buffer, "@notes.md")

    def test_completion_popup_grows_with_the_terminal_and_hangs_off_the_token(self):
        # The dropdown used to be a fixed-size box centred on the screen.
        # It now anchors on the token being completed and its window
        # adapts to the viewport (a taller terminal shows more of the
        # list), while still stopping clear of the status line.
        from core.scripted import final_response

        heights = {}
        for rows in (15, 45):
            session = make_session(_workspace(f"popupadapt{rows}"), [final_response("hi")])
            app = TuiApp(session, backend=FakeBackend(cols=120, rows=rows))
            app._init_surface()
            app._show_welcome()
            app.composer.completer = _FakeCompleter()
            app.handle_event(Key("/"))
            app.render_frame()

            rect = app._popup_rect
            self.assertIsNotNone(rect, f"no popup rect at {rows} rows")
            x, y, _w, h = rect
            self.assertEqual(
                x,
                app.composer.column_for(app.composer.completion.start, 120),
                "popup is not anchored on the token being completed",
            )
            self.assertEqual(
                y + h - 1,
                app.rows - 3 - 1,
                "popup does not stop one row above the status line",
            )
            self.assertGreaterEqual(y, app._content_top, "popup escapes the content area")
            heights[rows] = h
        self.assertGreater(
            heights[45],
            heights[15],
            "popup window did not adapt to a taller terminal",
        )


    def test_completion_popup_navigates_with_arrows(self):
        # The @ / completion popup must be navigable with the arrow keys
        # and accepted with Enter - the keyboard path an operator uses.
        from core.console import ConsoleCompleter
        from core.scripted import final_response

        workspace = os.path.join(
            os.environ.get("TEMP", tempfile.gettempdir()),
            f"mantra-popup-{os.getpid()}-{threading.get_ident()}",
        )
        os.makedirs(workspace, exist_ok=True)
        _TEMP_WORKSPACES.append(workspace)
        for name in ("note.txt", "notes.md"):
            with open(os.path.join(workspace, name), "w", encoding="utf-8") as fh:
                fh.write("x\n")
        session = make_session(workspace, [final_response("hi")])
        app = TuiApp(session, backend=FakeBackend(cols=100, rows=24))
        app._init_surface()
        app._show_welcome()
        app.composer.completer = ConsoleCompleter(session)
        for ch in "@no":
            app.handle_event(Key(ch))
        self.assertTrue(app.composer.popup_open)
        self.assertEqual(app.composer.selected, 0)
        app.handle_event(Key("down"))
        self.assertEqual(app.composer.selected, 1)
        app.handle_event(Key("up"))
        self.assertEqual(app.composer.selected, 0)
        app.handle_event(Key("enter"))
        self.assertEqual(app.composer.buffer, "@note.txt")


class WheelRoutingTest(_SurfaceTest):
    """Where a wheel notch goes: conversation, prompt, card, or scrollbar."""

    def test_wheel_over_single_line_composer_scrolls_transcript(self):
        # Regression: the composer row swallowed wheel events, and for a
        # single-line prompt scroll_by is a no-op - so wheeling near the
        # bottom of the window (where the prompt box sits) did nothing.
        # The composer only claims the wheel while it is multi-line.
        app, session, backend = _make_app([])
        with app.lock:
            for i in range(60):
                app.transcript.append(f"line {i:02d}")
        app.render_frame()
        composer_top = app.rows - app._composer_height(app.rows)
        self.assertFalse(app.composer.is_multiline)
        for y in (composer_top, app.rows - 1):
            before = app.transcript.scrolled
            app.handle_event(Mouse("wheel", 64, 5, y, frozenset()))
            self.assertGreater(
                app.transcript.scrolled,
                before,
                f"wheel at y={y} over a single-line composer did not scroll the transcript",
            )

    def test_wheel_over_multiline_composer_scrolls_transcript(self):
        # The wheel always scrolls the conversation, even over a multi-line
        # prompt box: routing notches into the composer made the box
        # swallow the wheel, so the operator could not scroll the
        # transcript from the lower half of the window.
        app, session, backend = _make_app([])
        with app.lock:
            for i in range(60):
                app.transcript.append(f"line {i:02d}")
        app.composer.buffer = "one\ntwo\nthree"
        app.render_frame()
        composer_top = app.rows - app._composer_height(app.rows)
        self.assertTrue(app.composer.is_multiline)
        for y in (composer_top, app.rows - 1):
            before = app.transcript.scrolled
            app.handle_event(Mouse("wheel", 64, 5, y, frozenset()))
            self.assertGreater(
                app.transcript.scrolled,
                before,
                f"wheel at y={y} over a multi-line composer did not scroll the transcript",
            )
            self.assertIsNone(
                app.composer._view_first,
                "the composer must not claim the wheel over its own box",
            )

    def test_wheel_walks_a_menu_overlay_but_still_scrolls_outside_it(self):
        # Mouse support across surfaces: a wheel notch over the menu walks
        # the highlighted option, while a notch outside the box keeps
        # scrolling the conversation behind it.
        app, session, backend = _make_app([])
        with app.lock:
            for i in range(80):
                app.transcript.append(f"line {i:03d}")
        app.render_frame()
        app.overlay = MenuOverlay("pick", [Option(f"opt{i:02d}") for i in range(20)])
        app.render_frame()
        menu = app.overlay
        x, y, _w, _h = menu.rect
        for _ in range(3):
            app.handle_event(Mouse("wheel", 65, x + 2, y + 2, frozenset()))
        self.assertEqual(menu.cursor, 3, "wheel-down over the menu did not walk it")
        app.handle_event(Mouse("wheel", 64, x + 2, y + 2, frozenset()))
        self.assertEqual(menu.cursor, 2, "wheel-up over the menu did not walk it back")
        app.render_frame()
        self.assertIn("› opt02", "\n".join(grid_rows(app.renderer.buffer)))

        before = app.transcript.scrolled
        app.handle_event(Mouse("wheel", 64, 1, app._content_top, frozenset()))
        self.assertGreater(
            app.transcript.scrolled,
            before,
            "wheel outside the menu must still scroll the conversation",
        )
        self.assertEqual(menu.cursor, 2, "scrolling outside the menu moved its selection")

    def test_wheel_scrolls_a_long_question_card_body(self):
        app, session, backend = _make_app([])
        body = "\n".join(f"diff line {i:03d}" for i in range(40))
        app.overlay = QuestionCard("allow?", body, choices="yna")
        app.render_frame()
        card = app.overlay
        x, y, _w, _h = card.rect
        self.assertEqual(card.scroll, 0)
        app.handle_event(Mouse("wheel", 65, x + 2, y + 2, frozenset()))
        app.render_frame()
        self.assertGreater(card.scroll, 0, "wheel did not scroll the card body")
        rows = grid_rows(app.renderer.buffer)
        self.assertIn("more above", "\n".join(rows))
        for _ in range(200):
            app.handle_event(Mouse("wheel", 64, x + 2, y + 2, frozenset()))
        app.render_frame()
        self.assertEqual(card.scroll, 0, "card body did not clamp back to the top")

    def test_wheel_slides_a_long_line_prompt_value(self):
        # A pasted key or URL is wider than the prompt card, and the caret
        # window alone can never show the part behind the caret: the wheel
        # slides the value, and any edit re-anchors the view to the caret.
        app, session, backend = _make_app([])
        app.overlay = LinePrompt("api key", default="k" * 90)
        app.render_frame()
        card = app.overlay
        x, y, _w, _h = card.rect

        app.handle_event(Mouse("wheel", 65, x + 3, y + 1, frozenset()))
        self.assertIsNotNone(card._view_col)
        self.assertGreater(card._view_col, 0, "wheel did not slide the value")
        for _ in range(100):
            app.handle_event(Mouse("wheel", 65, x + 3, y + 1, frozenset()))
        self.assertEqual(
            card._view_col,
            len(card.buffer) - card._text_cols,
            "not clamped at the end",
        )
        for _ in range(200):
            app.handle_event(Mouse("wheel", 64, x + 3, y + 1, frozenset()))
        self.assertEqual(card._view_col, 0, "not clamped back at the start")

        app.handle_event(Key("x"))
        self.assertIsNone(card._view_col, "typing did not re-anchor the view")
        self.assertTrue(card.buffer.endswith("x"))

        # The card swallows clicks (no highlight on the page behind it) and
        # stays open: it has no buttons to press.
        app.handle_event(Mouse("press", 0, x + 3, y + 1, frozenset()))
        self.assertIs(app.overlay, card)
        self.assertFalse(app.selection.active)

    def test_wheel_on_the_scrollbar_drags_the_thumb_one_step(self):
        # The bar is a drag target, not just a readout: a notch on its own
        # column moves the view by exactly one thumb-step (the inverse of
        # the render mapping), while a notch one column to the left keeps
        # the conversation's page-sized step.
        app, session, backend = _make_app([])
        with app.lock:
            for i in range(2000):
                app.transcript.append(f"line {i:04d}")
        app.render_frame()
        app.handle_event(Mouse("wheel", 64, 5, 5, frozenset()))
        app.render_frame()
        bar = app._scrollbar
        self.assertIsNotNone(bar, "no scrollbar while scrolled")
        track_x, top, height, _thumb_top, thumb_span = bar
        total = app.transcript.total_rows()
        height_v = app.transcript.viewport_height
        step = max(1, max(1, total - height_v) // max(1, height - thumb_span))

        before = app.transcript.scrolled
        app.handle_event(Mouse("wheel", 64, track_x, top + 1, frozenset()))
        self.assertEqual(app.transcript.scrolled - before, step, "not one thumb-step")
        app.handle_event(Mouse("wheel", 65, track_x, top + 1, frozenset()))
        self.assertEqual(app.transcript.scrolled, before, "wheel-down did not come back")

        page_before = app.transcript.scrolled
        app.handle_event(Mouse("wheel", 64, track_x - 1, top + 1, frozenset()))
        page = app.transcript.scrolled - page_before
        self.assertGreaterEqual(page, 3)
        self.assertNotEqual(page, step, "the conversation got the bar's step")

        # A held button owns the bar: a notch mid-drag must not move the
        # thumb out from under the cursor.
        app.handle_event(Mouse("press", 0, track_x, _thumb_row_at(app), frozenset()))
        self.assertTrue(app._scrollbar_drag, "press on the thumb did not start a drag")
        held = app.transcript.scrolled
        app.handle_event(Mouse("wheel", 64, track_x, top + 1, frozenset()))
        self.assertEqual(app.transcript.scrolled, held, "the wheel snatched the thumb mid-drag")


    def test_wheel_scrolls_and_shows_the_marker(self):
        app, session, backend = _make_app([])
        with app.lock:
            for i in range(60):
                app.transcript.append(f"line {i}")
        app.render_frame()
        app.handle_event(Mouse("wheel", 64, 5, 5, frozenset()))
        self.assertGreater(app.transcript.scrolled, 0)
        app.render_frame()
        joined = "\n".join(grid_rows(app.renderer.buffer))
        self.assertIn("^", joined)

    def test_scrollbar_appears_while_scrolled_and_clears_at_the_tail(self):
        # While the view is detached from the tail, the right edge shows
        # a track with a thumb proportional to the viewport; back at the
        # tail the scrollbar is gone and no row text is overwritten.
        app, session, backend = _make_app([])
        with app.lock:
            for i in range(60):
                app.transcript.append(f"line {i:02d}")
        app.render_frame()
        buf = app.renderer.buffer

        def content_edge(buf_):
            # Only the transcript rows (the composer walls also use │
            # on the same column, below the content area).
            top, h = app._content_top, app._content_height
            return "".join(buf_.chars[(top + y) * buf_.cols + buf_.cols - 1] for y in range(h))

        self.assertNotIn("│", content_edge(buf), "track painted while following the tail")

        app.handle_event(Mouse("wheel", 64, 5, 5, frozenset()))
        app.render_frame()
        edge_while_scrolled = content_edge(buf)
        self.assertIn("│", edge_while_scrolled, "no track while scrolled")
        self.assertIn("█", edge_while_scrolled, "no thumb while scrolled")
        # The thumb must not span the whole track.
        self.assertLess(edge_while_scrolled.count("█"), app._content_height)

        # Content rows must not be clobbered by the scrollbar: the edge
        # column may only hold track, thumb, or untouched-space glyphs.
        self.assertTrue(set(edge_while_scrolled) <= {"│", "█", " "})

        # Scrolling back to the tail clears it again.
        for _ in range(5):
            app.handle_event(Mouse("wheel", 65, 5, 5, frozenset()))
        app.render_frame()
        self.assertNotIn("│", content_edge(buf), "track still painted after re-follow")

    def test_scroll_to_bottom_chip_appears_and_jumps_to_tail(self):
        # While scrolled, a "↓ bottom" chip renders near the bottom-right
        # of the content area (clear of the scrollbar track column);
        # clicking it re-follows the tail. Clicking elsewhere does not.
        app, session, backend = _make_app([])
        with app.lock:
            for i in range(200):
                app.transcript.append(f"line {i:03d}")
        app.render_frame()
        self.assertIsNone(app._scroll_to_bottom, "chip shown while following the tail")

        app.handle_event(Mouse("wheel", 64, 10, 10, frozenset()))
        app.render_frame()
        chip = app._scroll_to_bottom
        self.assertIsNotNone(chip, "no chip while scrolled")
        chip_x, chip_y, chip_w = chip
        self.assertGreater(chip_w, 0)
        # Inside the content area, and not under the track column.
        self.assertGreaterEqual(chip_y, app._content_top)
        self.assertLess(chip_y, app._content_top + app._content_height)
        self.assertLess(chip_x + chip_w, app.renderer.buffer.cols - 1)
        # Chip text is actually painted at its rect.
        row = "".join(app.renderer.buffer.chars[chip_y * app.renderer.buffer.cols:(chip_y + 1) * app.renderer.buffer.cols])
        self.assertIn("bottom", row)

        # Click on the chip jumps to the live tail.
        app.handle_event(Mouse("press", 0, chip_x + chip_w // 2, chip_y, frozenset()))
        self.assertEqual(app.transcript.offset, 0)
        self.assertTrue(app.transcript.follow)
        self.assertEqual(app.transcript.missed, 0, "missed counter not reset by the jump")
        app.render_frame()
        self.assertIsNone(app._scroll_to_bottom, "chip still shown after jump")

        # A press on ordinary text never triggers the chip path.
        app.handle_event(Mouse("wheel", 64, 10, 10, frozenset()))
        app.render_frame()
        chip_x2, chip_y2, _ = app._scroll_to_bottom
        app.handle_event(Mouse("press", 0, 0, chip_y2, frozenset()))
        self.assertGreater(app.transcript.offset, 0, "click far from the chip jumped to the tail")

    def test_scrollbar_thumb_pulses_accent_with_chip(self):
        # While detached, the thumb renders accent during the flash
        # window (matching the chip) and bone at rest.
        app, session, backend = _make_app([])
        with app.lock:
            for i in range(400):
                app.transcript.append(f"line {i:04d}")
        app.handle_event(Mouse("wheel", 64, 10, 10, frozenset()))
        app.render_frame()
        track_x, top, h, thumb_top, span = app._scrollbar
        buf = app.renderer.buffer

        def thumb_params():
            style_id = buf.styles[(top + thumb_top) * buf.cols + track_x]
            return ";".join(buf.styles_table.params_for(style_id))

        app._chip_flash_until = 0.0
        app.render_frame()
        self.assertIn("255", thumb_params(), "thumb not bone at rest")
        app._chip_flash_until = time.monotonic() + 2.0
        app.render_frame()
        self.assertIn("204", thumb_params(), "thumb not accent during the pulse")

    def test_wheel_over_composer_scrolls_the_transcript(self):
        # The wheel always scrolls the conversation, even over a long
        # multi-line prompt box: routing notches into the composer made
        # the box swallow the wheel, so the operator could not scroll the
        # transcript from the lower half of the window.
        app, session, backend = _make_app([])
        with app.lock:
            for i in range(200):
                app.transcript.append(f"line {i:03d}")
        app.composer.set_text("\n".join(f"prompt {i}" for i in range(40)))
        app.render_frame()
        rows = app.rows
        transcript_before = app.transcript.scrolled

        # Wheel up over the box: transcript scrolls, composer stays anchored.
        app.handle_event(Mouse("wheel", 64, 10, rows - 2, frozenset()))
        self.assertGreater(
            app.transcript.scrolled, transcript_before, "box wheel did not scroll the transcript"
        )
        self.assertIsNone(app.composer._view_first, "composer claimed the wheel over its own box")

        # Wheel up over the conversation: transcript keeps scrolling.
        app.handle_event(Mouse("wheel", 64, 10, 5, frozenset()))
        self.assertGreater(app.transcript.scrolled, transcript_before)

        # Typing still re-anchors the composer to the caret.
        app.composer.consume_key("a")
        self.assertIsNone(app.composer._view_first, "typing did not re-anchor the view")

    def test_scrollbar_drag_moves_view_and_keeps_grab_point(self):
        # Pressing the thumb starts a drag; moving the pointer updates
        # the transcript offset through the exact inverse of the render
        # mapping, so the grab point stays under the cursor.
        app, session, backend = _make_app([])
        with app.lock:
            for i in range(200):
                app.transcript.append(f"line {i:03d}")
        app.render_frame()
        app.handle_event(Mouse("wheel", 64, 98, 10, frozenset()))
        app.render_frame()
        bar = app._scrollbar
        self.assertIsNotNone(bar)
        track_x, top, height, thumb_top, thumb_span = bar
        self.assertGreater(height, 4)

        grab_y = top + thumb_top + thumb_span // 2
        app.handle_event(Mouse("press", 0, track_x, grab_y, frozenset()))
        self.assertTrue(app._scrollbar_drag)

        # Drag to the top of the track: the view must land pinned at the
        # oldest content (offset == max), thumb at the track top.
        app.handle_event(Mouse("drag", 0, track_x, top, frozenset()))
        app.render_frame()
        bar2 = app._scrollbar
        self.assertEqual(bar2[3], 0, "thumb not at track top after dragging there")
        total = app.transcript.total_rows()
        max_offset = total - app.transcript.viewport_height
        self.assertEqual(app.transcript.offset, max_offset, "drag to track top did not pin the oldest content")

        # Drag to the bottom of the track: near the tail.
        app.handle_event(Mouse("drag", 0, track_x, top + height - 1, frozenset()))
        app.render_frame()
        self.assertEqual(app.transcript.offset, 0)
        self.assertTrue(app.transcript.follow)

        # Release ends the drag; later pointer motion must not scroll.
        app.handle_event(Mouse("release", 0, track_x, top + height - 1, frozenset()))
        self.assertFalse(app._scrollbar_drag)
        offset_before = app.transcript.offset
        app.handle_event(Mouse("drag", 0, track_x, top, frozenset()))
        self.assertEqual(app.transcript.offset, offset_before, "drag without press still scrolled")

    def test_scrollbar_track_click_pages_toward_the_press(self):
        # Clicking the bare track pages one viewport toward the click,
        # clamped at the ends; clicking the thumb itself does not page.
        # Enough content that one page (~viewport rows) cannot reach
        # either end of the pool.
        app, session, backend = _make_app([])
        with app.lock:
            for i in range(600):
                app.transcript.append(f"line {i:03d}")
        app.render_frame()
        for _ in range(4):  # deep enough that one page cannot reach an end
            app.handle_event(Mouse("wheel", 64, 98, 10, frozenset()))
        app.render_frame()
        track_x, top, height, thumb_top, thumb_span = app._scrollbar

        # Click just above the thumb: pages toward the tail by ~a page.
        before = app.transcript.offset
        app.handle_event(Mouse("press", 0, track_x, top + max(0, thumb_top - 1), frozenset()))
        self.assertLess(app.transcript.offset, before, "track click above the thumb did not page toward the tail")
        self.assertTrue(app._scrollbar_drag)  # press also arms a drag
        app.handle_event(Mouse("release", 0, track_x, top + max(0, thumb_top - 1), frozenset()))

        # Click just below the thumb: pages away from the tail again.
        app.render_frame()
        _, _, _, thumb_top2, _ = app._scrollbar
        before = app.transcript.offset
        app.handle_event(Mouse("press", 0, track_x, top + thumb_top2 + thumb_span, frozenset()))
        app.handle_event(Mouse("release", 0, track_x, top + thumb_top2 + thumb_span, frozenset()))
        self.assertGreater(app.transcript.offset, before, "track click below the thumb did not page up")

        # Press exactly on the thumb: no paging, just drag arming.
        app.render_frame()
        before = app.transcript.offset
        _, _, _, tt, _ = app._scrollbar
        app.handle_event(Mouse("press", 0, track_x, top + tt, frozenset()))
        self.assertEqual(app.transcript.offset, before, "thumb press paged the view")
        app.handle_event(Mouse("release", 0, track_x, top + tt, frozenset()))

    def test_selection_still_works_when_press_misses_the_scrollbar(self):
        # The scrollbar handlers take priority only on the bar's column;
        # a press on ordinary text must still start a selection.
        app, session, backend = _make_app([])
        with app.lock:
            for i in range(60):
                app.transcript.append(f"line {i:02d}")
        app.render_frame()
        app.handle_event(Mouse("wheel", 64, 98, 10, frozenset()))
        app.render_frame()
        y = app._content_top + 2
        app.handle_event(Mouse("press", 0, 0, y, frozenset()))
        self.assertIsNotNone(app.selection._press_cell, "press on text did not begin a selection")
        self.assertFalse(app._scrollbar_drag)

    def test_wheel_down_refollows_after_scrolling_up(self):
        # Scrolling back to the tail must reattach follow so new output
        # keeps streaming into view; stopping short of the bottom keeps
        # the viewport detached.
        app, session, backend = _make_app([])
        with app.lock:
            for i in range(60):
                app.transcript.append(f"line {i}")
        app.render_frame()
        app.handle_event(Mouse("wheel", 64, 5, 5, frozenset()))
        self.assertGreater(app.transcript.scrolled, 0)
        self.assertFalse(app.transcript.follow)
        app.handle_event(Mouse("wheel", 65, 5, 5, frozenset()))
        app.handle_event(Mouse("wheel", 65, 5, 5, frozenset()))
        app.handle_event(Mouse("wheel", 65, 5, 5, frozenset()))
        self.assertEqual(app.transcript.scrolled, 0)
        self.assertTrue(app.transcript.follow)


class OverlayClickTest(_SurfaceTest):
    """Clicks on a modal card: choose, answer, or stay out of the way."""

    def test_click_highlights_a_menu_option_then_accepts_it(self):
        # A click on the menu box walks the highlight to that row; only a
        # second click on the already highlighted row accepts it, so the
        # pointer can never fire a choice on its own.
        app, session, backend = _make_app([])
        results = []
        app.run_detached(lambda: results.append(
            app.choose("pick one", [Option("first"), Option("second"), Option("third")])
        ))
        self.assertTrue(wait_until(lambda: app.overlay is not None))
        self.assertTrue(wait_until(lambda: app._overlay_reply is not None))
        app.render_frame()
        menu = app.overlay
        x, y, _w, _h = menu.rect

        # Row 3 of the box is the second option (item rows start one below
        # the title): a click there only walks the highlight.
        app.handle_event(Mouse("press", 0, x + 3, y + 2, frozenset()))
        self.assertEqual(menu.cursor, 1, "click did not highlight the row under it")
        self.assertIsNone(menu.result, "a single click must not accept an option")
        self.assertIs(app.overlay, menu)

        # The third option is not highlighted either: still just a highlight.
        app.handle_event(Mouse("press", 0, x + 3, y + 3, frozenset()))
        self.assertEqual(menu.cursor, 2)
        self.assertIsNone(menu.result)

        # Clicking the row that is already highlighted accepts it.
        app.handle_event(Mouse("press", 0, x + 3, y + 3, frozenset()))
        self.assertTrue(wait_until(lambda: results == ["third"]))
        self.assertIsNone(app.overlay)

    def test_click_outside_a_menu_still_reaches_the_conversation(self):
        app, session, backend = _make_app([])
        with app.lock:
            for i in range(60):
                app.transcript.append(f"line {i:03d}")
        app.render_frame()
        app.overlay = MenuOverlay("pick", [Option(f"opt{i:02d}") for i in range(20)])
        app.render_frame()
        menu = app.overlay
        x, y, _w, h = menu.rect
        self.assertGreater(y, app._content_top, "menu does not leave a row free above it")
        app.handle_event(Mouse("press", 0, 1, app._content_top, frozenset()))
        app.handle_event(Mouse("drag", 0, 20, app._content_top + 1, frozenset()))
        self.assertEqual(menu.cursor, 0, "click outside the box moved its selection")
        self.assertTrue(app.selection.active, "click outside the box was swallowed")
        self.assertIs(app.overlay, menu)

    def test_click_on_a_question_card_button_answers_it(self):
        app, session, backend = _make_app([])
        results = []
        app.run_detached(lambda: results.append(app.ask_approval("may I write files?")))
        self.assertTrue(wait_until(lambda: app.overlay is not None))
        self.assertTrue(wait_until(lambda: app._overlay_reply is not None))
        app.render_frame()
        card = app.overlay
        x, y, _w, h = card.rect
        hint_row = y + h - 2
        no_zone = next(z for z in card._hint_zones if z[2] == "n")

        # A click on the body (not a button) is swallowed by the card but
        # does not answer it.
        app.handle_event(Mouse("press", 0, x + 2, y + 1, frozenset()))
        self.assertIs(app.overlay, card)
        self.assertEqual(results, [])
        self.assertFalse(app.selection.active, "a modal let a highlight through")

        app.handle_event(Mouse("press", 0, no_zone[0] + 1, hint_row, frozenset()))
        self.assertTrue(wait_until(lambda: results == ["n"]))
        self.assertIsNone(app.overlay)


class SmallTerminalTest(_SurfaceTest):
    """Splash, completion popup and modal cards in a cramped frame.

    Every surface must stay inside the frame and stop clear of the status
    row and the prompt box, and the boxes must be opaque: a terminal cell
    has no transparency, so anything that shows through the gaps reads as
    the overlay colliding with the page under it.
    """

    def test_tiny_terminal_shows_a_notice_instead_of_a_colliding_chrome(self):
        for cols, rows in ((60, 6), (24, 30), (40, 7)):
            app, session, backend = _make_app([], FakeBackend(cols=cols, rows=rows))
            app.render_frame()
            first = grid_rows(app.renderer.buffer)[0]
            self.assertIn(
                "terminal too small",
                first,
                f"no size notice at {cols}x{rows}: {first!r}",
            )
            # The chrome is skipped entirely: no prompt box on the last row.
            self.assertNotIn(
                "MANTRA >",
                "\n".join(grid_rows(app.renderer.buffer)),
                f"prompt box still drawn at {cols}x{rows}",
            )
            # A waiting modal is still drawn (clamped), so an approval is
            # never lost behind the notice.
            app.overlay = QuestionCard("allow?", "may I write files?")
            app.render_frame()
            card = app.overlay.rect
            self.assertIsNotNone(card)
            cx, cy, cw, ch = card
            self.assertTrue(
                0 <= cx and 0 <= cy and cx + cw <= cols and cy + ch <= rows,
                f"card {card} escapes the {cols}x{rows} frame",
            )

    def test_popup_menu_and_splash_stay_in_their_own_rows_when_cramped(self):
        for cols, rows in ((30, 8), (40, 10), (60, 12)):
            app, session, backend = _make_app([], FakeBackend(cols=cols, rows=rows))
            # Splash card: it must not reach the status row or the prompt.
            app.render_frame()
            splash_rows = [
                i for i, row in enumerate(grid_rows(app.renderer.buffer)) if "M A N T R A" in row
            ]
            if splash_rows:
                content_bottom = app._content_top + max(0, app._content_height) - 1
                self.assertTrue(
                    app._content_top <= splash_rows[0] <= content_bottom,
                    f"splash at row {splash_rows[0]} escapes the content area at {cols}x{rows}",
                )

            with app.lock:
                for i in range(60):
                    app.transcript.append(f"line {i:03d}")
            app.composer.completer = _FakeCompleter()
            app.handle_event(Key("/"))
            app.render_frame()
            border_row = app.rows - 1 - app._composer_height(app.rows)
            rect = app._popup_rect
            if rect is not None:
                px, py, pw, ph = rect
                self.assertTrue(0 <= px and px + pw <= cols, f"popup {rect} escapes {cols}x{rows}")
                self.assertLess(
                    py + ph - 1,
                    border_row,
                    f"popup overlaps the status row at {cols}x{rows}",
                )
                self.assertGreaterEqual(
                    py, app._content_top, f"popup over the info bar at {cols}x{rows}"
                )

            app.overlay = MenuOverlay("pick", [Option(f"opt{i:02d}") for i in range(20)])
            app.render_frame()
            mx, my, mw, mh = app.overlay.rect
            self.assertTrue(
                0 <= mx and 0 <= my and mx + mw <= cols and my + mh <= rows,
                f"menu {app.overlay.rect} escapes {cols}x{rows}",
            )
            app.overlay = QuestionCard("allow?", "\n".join(f"body {i}" for i in range(40)))
            app.render_frame()
            cx, cy, cw, ch = app.overlay.rect
            self.assertTrue(
                0 <= cx and 0 <= cy and cx + cw <= cols and cy + ch <= rows,
                f"card {app.overlay.rect} escapes {cols}x{rows}",
            )

    def test_overlay_boxes_do_not_let_the_page_show_through(self):
        app, session, backend = _make_app([], FakeBackend(cols=80, rows=24))
        with app.lock:
            for i in range(40):
                app.transcript.append(f"secret line {i:03d} " + "z" * 40)
        app.render_frame()

        def interior_rows(rect, rows, *, skip_last: int = 1):
            """The box's inside cells: borders stripped, outside text kept."""
            x, y, w, h = rect
            return [row[x + 1 : x + w - 1] for row in rows[y + 1 : y + h - skip_last]]

        app.overlay = MenuOverlay("pick", [Option(f"opt{i:02d}") for i in range(12)])
        app.render_frame()
        rows = grid_rows(app.renderer.buffer)
        for row in interior_rows(app.overlay.rect, rows):
            self.assertNotIn("secret", row, f"page shows through the menu box: {row!r}")
            self.assertNotIn("zzzz", row, f"page shows through the menu box: {row!r}")

        app.overlay = QuestionCard("allow?", "\n".join(f"secret body {i}" for i in range(20)))
        app.render_frame()
        rows = grid_rows(app.renderer.buffer)
        for row in interior_rows(app.overlay.rect, rows):
            self.assertNotIn("zzzz", row, f"page shows through the card: {row!r}")

        app.overlay = LinePrompt("key", default="k" * 60)
        app.render_frame()
        rows = grid_rows(app.renderer.buffer)
        for row in interior_rows(app.overlay.rect, rows):
            self.assertNotIn("zzzz", row, f"page shows through the prompt card: {row!r}")

        app.overlay = None
        app.composer.completer = _FakeCompleter()
        app.handle_event(Key("/"))
        app.render_frame()
        rows = grid_rows(app.renderer.buffer)
        for row in interior_rows(app._popup_rect, rows):
            self.assertNotIn("secret", row, f"page shows through the popup: {row!r}")


class ComposerColumnTest(unittest.TestCase):
    def test_column_for_agrees_with_the_rendered_caret(self):
        # ``column_for`` is what anchors the completion popup on the token
        # being completed, so it must map a buffer offset onto exactly the
        # cell ``render`` puts it in - across short buffers, buffers that
        # slide sideways, wide characters and multi-line prompts.
        samples = [
            ("", 0),
            ("@fi", 3),
            ("@fi", 0),
            ("x" * 60 + " @tail", 66),
            ("x" * 60 + " @tail", 62),
            ("line one\nline two\n@here", 23),
            ("a\n" + "y" * 50 + "\nz", 52),
            ("漢字 @path", 8),
        ]
        for cols in (24, 40, 100):
            for buffer, cursor in samples:
                c = Composer()
                c.set_text(buffer)
                c.cursor = cursor
                self.assertEqual(
                    c.column_for(c.cursor, cols),
                    c._caret_col(cols),
                    f"caret column mismatch for {buffer!r} at {cols} cols",
                )
                # Every offset before the caret must stay left of it.
                for index in range(0, cursor + 1, max(1, cursor // 7)):
                    self.assertLessEqual(
                        c.column_for(index, cols),
                        c.column_for(cursor, cols),
                        f"offset {index} anchored right of the caret for {buffer!r}",
                    )


if __name__ == "__main__":
    unittest.main()
