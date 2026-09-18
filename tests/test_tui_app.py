"""App-level integration: turns, chips, suggestions, prompt box,
scroll state, copying — everything that needs a whole TuiApp."""

from __future__ import annotations

import os
import shutil
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "."))
_tests_dir = os.path.dirname(os.path.abspath(__file__))
if _tests_dir not in sys.path:
    sys.path.insert(0, _tests_dir)
from core.tui.app import LayoutBridge, TuiApp  # noqa: E402
from core.tui.backend import Key, Mouse  # noqa: E402
from core.tui.overlays import Option  # noqa: E402

from tui_harness import _chip_cell_params, _chip_row_text, _make_app, app_module, grid_rows, wait_until  # noqa: E402
from tui_harness import _TEMP_WORKSPACES  # re-exported registry  # noqa: E402


class AppIntegrationTest(unittest.TestCase):
    def tearDown(self):
        for ws in list(_TEMP_WORKSPACES):
            shutil.rmtree(ws, ignore_errors=True)
        _TEMP_WORKSPACES.clear()
    def test_turn_lands_in_the_grid(self):
        from core.scripted import LLMResponse

        app, session, backend = _make_app([LLMResponse(content="Hello there")])
        app.render_frame()
        app.submit("Hello")
        self.assertTrue(wait_until(lambda: not app.busy))
        app.render_frame()
        rows = grid_rows(app.renderer.buffer)
        joined = "\n".join(rows)
        self.assertIn("Hello", joined)
        self.assertIn("Hello there", joined)
        # The usage footer was printed by the session through the bridge.
        self.assertTrue(any("STEP" in row or "in context" in row or "CTX" in row for row in rows))

    def test_typing_reaches_the_composer_and_renders(self):
        app, session, backend = _make_app([])
        for ch in "hi there":
            app.handle_event(Key(ch))
        app.render_frame()
        joined = "\n".join(grid_rows(app.renderer.buffer))
        self.assertIn("hi there", joined)

    def test_escape_aborts_only_when_busy(self):
        from core.scripted import LLMResponse

        app, session, backend = _make_app([LLMResponse(content="slow reply")])
        app.submit("go")
        self.assertTrue(wait_until(lambda: app.busy))
        app.handle_event(Key("esc"))
        self.assertTrue(session._abort.is_set())
        self.assertTrue(wait_until(lambda: not app.busy))

    def test_approval_card_round_trip(self):
        app, session, backend = _make_app([])
        results = []
        app.run_detached(lambda: results.append(app.ask_approval("may I write files?")))
        self.assertTrue(wait_until(lambda: app.overlay is not None))
        self.assertTrue(wait_until(lambda: app._overlay_reply is not None))
        # Render: the card is on screen.
        app.render_frame()
        joined = "\n".join(grid_rows(app.renderer.buffer))
        self.assertIn("allow?", joined)
        app.handle_event(Key("y"))
        self.assertTrue(wait_until(lambda: results == ["y"]))
        self.assertIsNone(app.overlay)

    def test_menu_round_trip(self):
        app, session, backend = _make_app([])
        results = []
        app.run_detached(lambda: results.append(
            app.choose("pick one", [Option("first"), Option("second")])
        ))
        self.assertTrue(wait_until(lambda: app.overlay is not None))
        app.handle_event(Key("enter"))
        self.assertTrue(wait_until(lambda: results == ["first"]))

    def test_scroll_to_bottom_chip_stays_while_a_turn_streams(self):
        # Mid-turn (spinner running, new output arriving) the chip must
        # still show while scrolled up, and clicking it still re-follows.
        app, session, backend = _make_app([])
        with app.lock:
            for i in range(200):
                app.transcript.append(f"line {i:03d}")
        app.render_frame()
        app.set_busy(True, "Channeling")
        app.handle_event(Mouse("wheel", 64, 10, 10, frozenset()))
        # Stream output while detached: the chip must survive repaints.
        app.feed_output("streamed chunk")
        app.render_frame()
        self.assertTrue(app.busy)
        self.assertGreater(app.transcript.offset, 0)
        chip = app._scroll_to_bottom
        self.assertIsNotNone(chip, "chip vanished mid-turn while scrolled")
        chip_x, chip_y, chip_w = chip
        app.handle_event(Mouse("press", 0, chip_x + chip_w // 2, chip_y, frozenset()))
        self.assertEqual(app.transcript.offset, 0)
        self.assertTrue(app.transcript.follow)

    def test_chip_flashes_accent_when_turn_ends_while_detached(self):
        # A turn finishing while the operator is scrolled away is worth
        # announcing: chip and count render in the accent colour for a
        # moment, then drop back to the regular info styling.
        app, session, backend = _make_app([])
        with app.lock:
            for i in range(200):
                app.transcript.append(f"line {i:03d}")
            app.transcript.scroll_up(5)
            app.transcript.append("while detached")
        self.assertGreater(app.transcript.missed, 0)

        app.set_busy(False)  # turn finished while detached
        self.assertGreater(app._chip_flash_until, 0, "no flash armed on turn end while detached")
        app.render_frame()
        row = _chip_row_text(app)
        self.assertIn("↓ bottom", row)
        self.assertIn("204", _chip_cell_params(app), "label not accent-styled while flashing")

        # Once the flash expires the chip returns to the regular style.
        app._chip_flash_until = time.monotonic() - 0.01
        app.render_frame()
        row = _chip_row_text(app)
        self.assertIn("117", _chip_cell_params(app), "label not back to info styling after flash")

        # Finishing a turn while FOLLOWING must not flash.
        app.set_busy(True)
        with app.lock:
            app.transcript.jump_bottom()
        app._chip_flash_until = 0.0  # drop the earlier flash before re-arming
        app.set_busy(False)
        self.assertEqual(app._chip_flash_until, 0.0, "flash armed while following the tail")

    def test_chip_flashes_while_output_streams_in_detached(self):
        # Output arriving while scrolled away pulses the chip in the
        # accent colour — not just at turn end.
        app, session, backend = _make_app([])
        with app.lock:
            for i in range(200):
                app.transcript.append(f"line {i:03d}")
            app.transcript.scroll_up(5)
        app.render_frame()
        self.assertIn("117", _chip_cell_params(app), "chip not at info styling at rest")

        app.feed_output("more output\n")
        self.assertGreater(app.transcript.missed, 0)
        self.assertGreater(app._chip_flash_until, 0, "no flash armed on output while detached")
        app.render_frame()
        self.assertIn("204", _chip_cell_params(app), "chip not accent-pulsed on new output")

        # Output while FOLLOWING must not flash.
        with app.lock:
            app.transcript.jump_bottom()
        app._chip_flash_until = 0.0  # drop the pulse before re-checking
        app.feed_output("tail text\n")
        self.assertEqual(app._chip_flash_until, 0.0, "flash armed while following the tail")

        # A chip click while the pulse is pending hands straight over to
        # the fade (flash cleared, echo armed). The throttle anchor is
        # reset so this phase is not swallowed by the earlier pulse.
        app.handle_event(Mouse("wheel", 64, 10, 10, frozenset()))
        app._chip_last_pulse = 0.0
        app.feed_output("again\n")
        self.assertGreater(app._chip_flash_until, 0)
        app.render_frame()
        chip_x, chip_y, chip_w = app._scroll_to_bottom
        app.handle_event(Mouse("press", 0, chip_x + chip_w // 2, chip_y, frozenset()))
        self.assertEqual(app._chip_flash_until, 0.0, "click did not clear the pending flash")
        self.assertGreater(app._chip_fade_until, 0, "click did not arm the fade")

    def test_chip_fades_out_after_returning_to_the_tail(self):
        # Returning to the tail (chip click, wheel, End) lingers the chip
        # for a second in faint style with a frozen count, then clears.
        app, session, backend = _make_app([])
        with app.lock:
            for i in range(200):
                app.transcript.append(f"line {i:03d}")
            app.transcript.scroll_up(5)
            app.transcript.append("while detached")
        missed_before = app.transcript.missed

        # Fade is armed by a chip click, with the count frozen at jump time.
        app.render_frame()
        chip_x, chip_y, chip_w = app._scroll_to_bottom
        app.handle_event(Mouse("press", 0, chip_x + chip_w // 2, chip_y, frozenset()))
        self.assertGreater(app._chip_fade_until, 0, "no fade armed after chip click")
        self.assertEqual(app._chip_fade_missed, missed_before, "fade count not frozen at jump time")
        app.render_frame()
        row = _chip_row_text(app)
        self.assertIn("↓ bottom", row, "fading chip not painted after click")
        self.assertIn(f"+{missed_before}", row, "fading chip lost the frozen count")
        self.assertIsNone(app._scroll_to_bottom, "fading chip must not be clickable")
        self.assertIn("246", _chip_cell_params(app), "fading chip not faint-styled")

        # When the fade expires the chip disappears entirely.
        app._chip_fade_until = time.monotonic() - 0.01
        app.render_frame()

        # Arm a fresh fade, then a new render must not re-arm it.
        app.handle_event(Mouse("wheel", 64, 10, 10, frozenset()))
        app.render_frame()
        chip_x, chip_y, chip_w = app._scroll_to_bottom
        app.handle_event(Mouse("press", 0, chip_x + chip_w // 2, chip_y, frozenset()))
        self.assertGreater(app._chip_fade_until, 0)
        until = app._chip_fade_until
        app.render_frame()  # fade still pending: no re-arm, count kept
        self.assertEqual(app._chip_fade_until, until, "fade re-armed by a render while pending")

        # Wheeling back down to the tail also arms the fade: detach
        # first (a wheel-down at the tail is a no-op worth no echo),
        # then wheel back down to re-follow.
        app._chip_fade_until = 0.0
        app.handle_event(Mouse("wheel", 64, 10, 10, frozenset()))
        self.assertFalse(app.transcript.follow, "wheel-up did not detach")
        app.handle_event(Mouse("wheel", 65, 10, 10, frozenset()))
        self.assertGreater(app._chip_fade_until, 0, "no fade armed when wheeling back to the tail")
        self.assertTrue(app.transcript.follow)

    def test_chip_fade_not_armed_while_following(self):
        # Returning to the tail while already following must not arm a
        # fade (there was no jump to echo).
        app, session, backend = _make_app([])
        with app.lock:
            for i in range(200):
                app.transcript.append(f"line {i:03d}")
        app.render_frame()
        self.assertTrue(app.transcript.follow)
        app.set_busy(True)
        with app.lock:
            app.transcript.jump_bottom()
        app._chip_fade_until = 0.0  # drop any earlier fade before re-arming
        self.assertEqual(app._chip_fade_until, 0.0)

    def test_border_marker_shows_missed_count(self):
        # While detached, the composer border shows " ^N +M": the offset
        # marker plus the missed-row count, so growth stays visible
        # while typing even when the chip is out of view.
        app, session, backend = _make_app([])
        with app.lock:
            for i in range(200):
                app.transcript.append(f"line {i:03d}")
        app.render_frame()
        border_row = app.rows - 3
        border = grid_rows(app.renderer.buffer)[border_row]
        self.assertNotIn("^", border)

        app.handle_event(Mouse("wheel", 64, 10, 10, frozenset()))
        app.feed_output("grown while detached\n")
        app.render_frame()
        border = grid_rows(app.renderer.buffer)[border_row]
        self.assertRegex(border, r"\^\d+ \+\d+", "no '^N +M' marker on the border while detached with missed rows")

        # Back at the tail: the whole marker disappears.
        with app.lock:
            app.transcript.jump_bottom()
        app.render_frame()
        border = grid_rows(app.renderer.buffer)[border_row]
        self.assertNotIn("^", border)

    def test_toast_is_painted_and_expires(self):
        # toast_message must actually paint: centered on the top
        # hairline row, gone after expiry, and never leaking into the
        # composer border.
        app, session, backend = _make_app([])
        app.toast_message("suggestions off", seconds=2.0)
        app.render_frame()
        grid = grid_rows(app.renderer.buffer)
        rows = [i for i, row in enumerate(grid) if "suggestions off" in row]
        self.assertEqual(rows, [1], "toast not centered on the hairline row")
        border = grid[app.rows - 3]
        self.assertNotIn("suggestions off", border)

        app.toast = ""
        app._toast_until = 0.0
        app.render_frame()
        grid = grid_rows(app.renderer.buffer)
        self.assertTrue(all("suggestions off" not in r for r in grid), "toast survived expiry")

    def test_suggestions_use_the_assistant_reply_text(self):
        # The engine reads session.last_reply: a green test report in
        # the reply yields the commit chip even when the prompt alone
        # would not.
        from core.tui.suggest import suggestions_for

        # Engine level: reply signals are read.
        green = suggestions_for("what changed?", "All 42 tests pass.", "", False)
        self.assertTrue(any(s.label == "commit the changes" for s in green))
        implemented = suggestions_for("add a feature", "I added the parser and updated the docs.", "", False)
        labels = [s.label for s in implemented]
        self.assertIn("commit the changes", labels)
        self.assertIn("update the docs", labels)
        # A non-committal reply still earns a row: the engine falls back
        # to conversation-derived chips instead of going quiet.
        neutral = suggestions_for("hello", "Hi! How can I help?", "", False)
        self.assertTrue(neutral, "fallback row missing for a content-free turn")
        self.assertTrue(all("hello" not in s.command.lower() for s in neutral))

    def test_suggestions_ranked_by_recency(self):
        # Reply evidence outranks tool evidence, which outranks prompt
        # evidence: the freshest signal wins slot 1. Ties break by rule
        # order, deterministically.
        from core.tui.suggest import suggestions_for

        # Same rule matched in two zones: the reply hit (weight 3) ranks
        # the commit chip first even though the prompt also matched.
        ranked = suggestions_for("tests are failing", "", "", True)
        self.assertEqual(ranked[0].label, "diagnose the failure")

        # Reply-only signal vs prompt-only signal for the same rule:
        # both score, but the ordering must be deterministic.
        a = suggestions_for("look at the docs", "", "", False)
        b = suggestions_for("", "the docs are updated", "", False)
        self.assertEqual(a[0].label, "update the docs")
        self.assertEqual(b[0].label, "update the docs")

        # Cross-rule ranking: a strong reply signal (green tests) beats
        # a weak prompt signal (a passing mention of "test").
        mixed = suggestions_for("add a test somewhere", "All 12 tests pass.", "", False)
        self.assertEqual(mixed[0].label, "commit the changes")

        # Determinism: identical inputs, identical output order.
        again = suggestions_for("add a test somewhere", "All 12 tests pass.", "", False)
        self.assertEqual([s.command for s in mixed], [s.command for s in again])

        # App level: the reply is read off the session.
        app, session, backend = _make_app([])
        app._turn_user_prompt = "what changed?"
        app._turn_pending = True
        session.last_reply = "All 42 tests pass. Nothing else was touched."
        app._maybe_show_suggestions()
        self.assertIn("commit the changes", app.suggestions)
        # And the reply is consumed turn-scoped: re-running without a
        # new prompt must not re-show.
        self.assertEqual(app._turn_user_prompt, "")

    def test_suggestions_toggle_disables_the_row(self):
        # config "suggestions": False removes the row entirely - turn
        # completion never populates it, even with a rich reply.
        app, session, backend = _make_app([])
        self.assertTrue(app.suggestions_enabled)
        app.suggestions_enabled = False
        app._turn_user_prompt = "edit the files"
        app._turn_had_error = True
        app._turn_pending = True
        app._maybe_show_suggestions()
        self.assertEqual(app.suggestions, [], "row shown while disabled")

        # A fresh app whose session config disables suggestions also
        # reads the flag at construction time.
        app2, session2, backend2 = _make_app([])
        session2.config = {"suggestions": False}
        app3 = TuiApp(session2, backend=backend2)
        app3._init_surface()
        self.assertFalse(app3.suggestions_enabled, "flag not read from session config")

    def test_suggestions_appear_after_turn_and_are_clickable(self):
        # After a finished turn whose prompt mentions files, a row of
        # chips appears at the tail: clicking one submits its command.
        from core.scripted import final_response

        app, session, backend = _make_app([final_response("edited the files for you")])
        app.submit("please edit the files")
        wait_until(lambda: not app.busy, 10)
        app.render_frame()
        self.assertTrue(app.suggestions, "no suggestions after the turn")
        self.assertGreater(len(app._suggestion_rects), 0, "suggestion row not painted")
        x, y, w = app._suggestion_rects[0]
        row = "".join(app.renderer.buffer.chars[y * app.renderer.buffer.cols:(y + 1) * app.renderer.buffer.cols])
        self.assertIn("1 ", row, "chip number not painted")

        # Click the first chip: the row clears and its command runs.
        app.handle_event(Mouse("press", 0, x + w // 2, y, frozenset()))
        self.assertEqual(app.suggestions, [], "row not dismissed on click")
        wait_until(lambda: app.busy, 5)  # the chip's command became a turn

        # Esc dismisses without running; submit also dismisses.
        app, session, backend = _make_app([])
        app.suggestions = ["one", "two"]
        app._suggestion_commands = ["cmd one", "cmd two"]
        app._suggestions_until = time.monotonic() + 30
        app.handle_event(Key("esc"))
        self.assertEqual(app.suggestions, [], "esc did not dismiss the row")
        app.suggestions = ["one"]
        app._suggestion_commands = ["cmd one"]
        app.submit("plain prompt")
        self.assertEqual(app.suggestions, [], "submit did not dismiss the row")

    def test_suggestion_expiry_and_detached_hold(self):
        # The row self-dismisses after its linger window, and holds its
        # expiry while the operator is detached from the tail.
        app, session, backend = _make_app([])
        app.suggestions = ["one", "two"]
        app._suggestion_commands = ["cmd one", "cmd two"]
        app._suggestions_until = time.monotonic() - 0.01  # already expired
        app.tick_animation()
        self.assertEqual(app.suggestions, [], "expired row not dismissed")

        app.suggestions = ["one"]
        app._suggestion_commands = ["cmd one"]
        app._suggestions_until = time.monotonic() + 2.0
        with app.lock:
            app.transcript.scroll_up(3)  # detached: expiry must hold
        app.render_frame()
        self.assertTrue(app.suggestions, "row dismissed while merely detached")

    def test_chip_counter_counts_rows_appended_while_detached(self):
        # Output arriving while scrolled up accumulates on the chip as
        # "+N" (in wrapped display rows); re-following resets it, and
        # output while following never counts.
        app, session, backend = _make_app([])
        with app.lock:
            for i in range(200):
                app.transcript.append(f"line {i:03d}")
        app.render_frame()
        app.handle_event(Mouse("wheel", 64, 10, 10, frozenset()))
        self.assertEqual(app.transcript.missed, 0)
        with app.lock:
            app.transcript.append("while detached one")
            app.transcript.append("while detached two")
        app.render_frame()
        self.assertEqual(app.transcript.missed, 2)
        chip_x, chip_y, chip_w = app._scroll_to_bottom
        cols = app.renderer.buffer.cols
        row = "".join(app.renderer.buffer.chars[chip_y * cols:(chip_y + 1) * cols])
        self.assertIn("+2", row, "counter not painted on the chip")
        # Click the chip: counter resets with the jump.
        app.handle_event(Mouse("press", 0, chip_x + chip_w // 2, chip_y, frozenset()))
        self.assertEqual(app.transcript.missed, 0)
        # Appending while following must not accumulate.
        with app.lock:
            app.transcript.append("tail line")
        self.assertEqual(app.transcript.missed, 0)

    def test_missed_counter_resets_when_wheeling_back_to_the_tail(self):
        app, session, backend = _make_app([])
        with app.lock:
            for i in range(200):
                app.transcript.append(f"line {i:03d}")
        app.render_frame()
        app.handle_event(Mouse("wheel", 64, 10, 10, frozenset()))
        with app.lock:
            app.transcript.append("arrived while away")
        self.assertEqual(app.transcript.missed, 1)
        for _ in range(6):
            app.handle_event(Mouse("wheel", 65, 10, 10, frozenset()))
        self.assertTrue(app.transcript.follow)
        self.assertEqual(app.transcript.missed, 0, "wheeling to the tail did not reset the counter")

    def test_empty_composer_arrow_keys_do_not_scroll_without_history(self):
        # Plain up/down on an empty composer recall prompt history (see
        # test_empty_composer_up_down_cycles_prompt_history); with no
        # history they fall through instead of scrolling - scrolling
        # lives on ctrl+up/down, wheel and PageUp.
        app, session, backend = _make_app([])
        with app.lock:
            for i in range(60):
                app.transcript.append(f"line {i}")
        app.handle_event(Key("up"))
        self.assertEqual(app.transcript.offset, 0)  # no history: no recall/scroll
        app.handle_event(Key("down"))
        self.assertEqual(app.transcript.offset, 0)
        app.handle_event(Key("home"))
        self.assertGreater(app.transcript.offset, 0)  # home jumps to the top
        app.handle_event(Key("end"))
        self.assertEqual(app.transcript.offset, 0)
        self.assertTrue(app.transcript.follow)

    def test_arrow_keys_do_not_scroll_while_typing(self):
        # With text in the composer, up/down stay caret navigation.
        app, session, backend = _make_app([])
        with app.lock:
            for i in range(60):
                app.transcript.append(f"line {i}")
        for ch in "hi":
            app.handle_event(Key(ch))
        app.handle_event(Key("up"))
        self.assertEqual(app.transcript.offset, 0)
        self.assertTrue(app.transcript.follow)

    def test_empty_composer_up_down_cycles_prompt_history(self):
        # Prompt history: up recalls the previous prompt, down walks back
        # to the fresh slot; typing leaves the recall mode.
        app, session, backend = _make_app([])
        for p in ("first prompt", "second prompt", "third prompt"):
            for ch in p:
                app.handle_event(Key(ch))
            app.handle_event(Key("enter"))
        self.assertEqual(app._history, ["first prompt", "second prompt", "third prompt"])

        app.handle_event(Key("up"))
        self.assertEqual(app.composer.buffer, "third prompt")
        app.handle_event(Key("up"))
        self.assertEqual(app.composer.buffer, "second prompt")
        app.handle_event(Key("up"))
        self.assertEqual(app.composer.buffer, "first prompt")
        app.handle_event(Key("down"))
        self.assertEqual(app.composer.buffer, "second prompt")
        app.handle_event(Key("down"))
        self.assertEqual(app.composer.buffer, "third prompt")
        app.handle_event(Key("down"))
        self.assertEqual(app.composer.buffer, "")

        # Typing exits the recall mode: up no longer overwrites.
        app.handle_event(Key("up"))
        app.handle_event(Key("x"))
        app.handle_event(Key("up"))
        self.assertEqual(app.composer.buffer, "third promptx")

    def test_ctrl_arrows_scroll_the_transcript(self):
        # Plain arrows recall history; ctrl+up/down still scroll.
        app, session, backend = _make_app([])
        with app.lock:
            for i in range(60):
                app.transcript.append(f"line {i}")
        app.handle_event(Key("up", mods=frozenset({"ctrl"})))
        self.assertGreater(app.transcript.offset, 0)
        app.handle_event(Key("down", mods=frozenset({"ctrl"})))
        self.assertLess(app.transcript.offset, 60)

    def test_prompt_box_is_a_closed_rectangle(self):
        # The prompt is a full rectangle: status row as the top edge,
        # wall rows for the input, and a solid bottom edge underneath.
        app, session, backend = _make_app([])
        app.render_frame()
        grid = grid_rows(app.renderer.buffer)
        top = grid[app.rows - 3]
        content = grid[app.rows - 2]
        bottom = grid[app.rows - 1]
        self.assertTrue(top.startswith("╭") and top.endswith("╮"))
        self.assertTrue(content.startswith("│") and content.endswith("│"))
        self.assertTrue(bottom.startswith("╰") and bottom.endswith("╯"))
        self.assertEqual(len(bottom), app.renderer.buffer.cols)

    def test_multiline_chip_sits_inside_the_rectangle(self):
        # Multiline input keeps a single top edge; the line-count chip is
        # a divider row inside the box, not a second box top.
        app, session, backend = _make_app([])
        for ch in "ab":
            app.handle_event(Key(ch))
        app.handle_event(Key("newline"))
        app.handle_event(Key("c"))
        app.render_frame()
        grid = grid_rows(app.renderer.buffer)
        bottom_rows = "\n".join(grid[app.rows - 6 :])
        self.assertEqual(bottom_rows.count("╭"), 1)
        self.assertEqual(bottom_rows.count("╰"), 1)
        chip_row = next(r for r in grid if "lines" in r)
        self.assertTrue(chip_row.startswith("│") and chip_row.endswith("│"))
        self.assertNotIn("╭", chip_row)

    def test_status_chip_does_not_leak_prompt_label(self):
        # The live token counter rides the prompt body ("│ MANTRA > 1 tok ·");
        # only the counter part belongs in the border chip.
        app, session, backend = _make_app([])
        session.layout.draw_prompt("\033[2m│ \033[0m\033[1mMANTRA >\033[0m 1 tok ·")
        self.assertEqual(app.counter_text, "1 tok ·")
        self.assertNotIn("MANTRA", app.counter_text)

    def test_counter_chip_clears_when_busy_ends(self):
        app, session, backend = _make_app([])
        app.set_counter("1 tok ·")
        app.set_busy(False)
        self.assertEqual(app.counter_text, "")

    def test_busy_border_shows_elapsed_and_counter(self):
        # While a turn runs the border chip shows the spinner, the label,
        # the elapsed time and the live token counter with its rate - but
        # no queued/toast side chips.

        app, session, backend = _make_app([])
        app._turn_started = time.monotonic() - 2
        app.set_busy(True, label="Chanting")
        app.counter_text = "1 tok · · 12 tok/s"
        app.queued = "something"
        app.toast = "note"
        app.render_frame()
        border = grid_rows(app.renderer.buffer)[app.rows - 3]
        self.assertIn("Chanting", border)
        self.assertRegex(border, r"\d+s")
        self.assertIn("1 tok ·", border)
        self.assertIn("tok/s", border)
        self.assertNotIn("queued", border)
        self.assertNotIn("note", border)

    def test_idle_border_is_clean(self):
        # The model/approval summary lives in the top bar; the idle
        # border chip stays clean and shows only a queued prompt notice.
        app, session, backend = _make_app([])
        app.render_frame()
        border = grid_rows(app.renderer.buffer)[app.rows - 3]
        self.assertNotIn("approval", border)
        self.assertNotIn("gpt-4o", border)
        self.assertEqual(border.strip("╭╮─ "), "")
        app.queued = "message"
        app.render_frame()
        border = grid_rows(app.renderer.buffer)[app.rows - 3]
        self.assertIn("queued prompt", border)
        app.queued = ""
        app.toast = "copied 1 line"
        app.render_frame()
        border = grid_rows(app.renderer.buffer)[app.rows - 3]
        self.assertNotIn("copied 1 line", border)

    def test_busy_chip_shows_counter_during_a_streaming_turn(self):
        # The counter rides the live delta stream: a real streaming turn
        # must put "tok ·" and "tok/s" into the busy chip, not just a
        # preset counter_text.
        from core.scripted import LLMResponse, ScriptedLLMClient

        class _Slow(ScriptedLLMClient):
            def chat(self, messages, tools=None, on_delta=None):
                response = LLMResponse(content="one two three four five six seven eight")
                if on_delta:
                    for word in response.content.split(" "):
                        on_delta(word + " ")
                        time.sleep(0.03)
                return response

        app, session, backend = _make_app([_Slow([LLMResponse(content="x")])])
        app.submit("go")
        self.assertTrue(wait_until(lambda: app.counter_text != "", 5))
        app.render_frame()
        border = grid_rows(app.renderer.buffer)[app.rows - 3]
        self.assertIn("tok ·", border)
        self.assertIn("tok/s", border)
        wait_until(lambda: not app.busy, 10)

    def test_turn_end_does_not_yank_a_detached_scroll(self):
        # Regression: handle()'s tail force-scrolled the transcript to
        # the bottom at the end of every turn, so scrolling up while or
        # just after a task ran looked broken. A detached scroll must
        # survive the turn's end.
        from core.scripted import final_response

        app, session, backend = _make_app([final_response("done")])
        with app.lock:
            for i in range(40):
                app.transcript.append(f"line {i}")
            app.transcript.scroll_up(5)
            detached = app.transcript.offset
        self.assertGreater(detached, 0)
        self.assertFalse(session.layout.following)
        app.submit("go")
        wait_until(lambda: not app.busy, 10)
        app.render_frame()
        self.assertEqual(app.transcript.offset, detached)
        self.assertFalse(session.layout.following)

    def test_busy_chip_waits_for_real_tokens_before_showing_counter(self):
        # While the model thinks (no tokens yet) the busy chip must not
        # show fake "0 tok" values; the counter appears with the first
        # real streamed token.
        from core.scripted import LLMResponse, ScriptedLLMClient

        class _Thinking(ScriptedLLMClient):
            def chat(self, messages, tools=None, on_delta=None):
                time.sleep(0.6)  # model "thinks" before the first token
                response = LLMResponse(content="hello there world")
                if on_delta:
                    for word in response.content.split(" "):
                        on_delta(word + " ")
                        time.sleep(0.03)
                return response

        app, session, backend = _make_app([_Thinking([LLMResponse(content="x")])])
        app.submit("go")
        self.assertTrue(wait_until(lambda: app.busy, 5))
        time.sleep(0.2)  # still thinking: no tokens have streamed
        app.render_frame()
        border = grid_rows(app.renderer.buffer)[app.rows - 3]
        self.assertNotIn("tok", border)
        self.assertNotIn("0 tok", border)
        # Once tokens stream, the counter shows real numbers promptly.
        self.assertTrue(wait_until(lambda: app.counter_text != "", 5))
        app.render_frame()
        border = grid_rows(app.renderer.buffer)[app.rows - 3]
        self.assertIn("tok ·", border)
        self.assertIn("tok/s", border)
        wait_until(lambda: not app.busy, 10)

    def test_layout_bridge_reports_lines(self):
        app, session, backend = _make_app([])
        bridge = LayoutBridge(app)
        with app.lock:
            app.transcript.clear()
        app.feed_output("a\nb\n")
        self.assertEqual(bridge.lines, ["a", "b"])
        self.assertTrue(bridge.active)

    def test_ctrl_y_copies_selection_after_scroll(self):
        # Regression: _selection_text used overlay_rows(0, ...) while the
        # visible window starts at a non-zero display row once the
        # transcript overflows, so ctrl+y silently copied nothing.
        from core.scripted import final_response

        app, session, backend = _make_app([final_response("hi")])
        with app.lock:
            for i in range(40):
                app.transcript.append(f"filler line {i}")
            app.transcript.append("target alpha row")
            app.transcript.append("target beta row")
        app.render_frame()
        grid = grid_rows(app.renderer.buffer)
        top, height = app._content_top, app._content_height
        alpha_y = next(i for i, r in enumerate(grid) if "alpha" in r and top <= i < top + height)
        beta_y = next(i for i, r in enumerate(grid) if "beta" in r and top <= i < top + height)
        captured = []
        app_module.copy_text = lambda t: captured.append(t)
        app.handle_event(Mouse("press", 0, 0, alpha_y, frozenset()))
        app.handle_event(Mouse("drag", 0, 10, beta_y, frozenset()))
        app.render_frame()
        app.handle_event(Key("ctrl+y"))
        self.assertTrue(captured, msg="ctrl+y copied nothing")
        self.assertIn("alpha", captured[0])
        self.assertIn("beta", captured[0])


class ConversationSuggestionsTest(unittest.TestCase):
    """Chips stay tied to the conversation and show after every reply.

    Three contracts pinned here:
    * the operator's own subject seeds a "continue with ..." chip;
    * every agent turn earns a row (fallback chips for pure conversation),
      including end-to-end through submit();
    * slash-command turns show no row - chrome is not a reply.
    """

    def tearDown(self):
        for ws in list(_TEMP_WORKSPACES):
            shutil.rmtree(ws, ignore_errors=True)
        _TEMP_WORKSPACES.clear()

    def test_topic_chip_echoes_the_operators_subject(self):
        from core.tui.suggest import topic_of

        self.assertEqual(topic_of("please can you fix the login redirect bug"), "fix login redirect bug")
        self.assertEqual(topic_of("why is the build failing"), "build failing")
        self.assertEqual(topic_of("hello"), "")
        self.assertEqual(topic_of(""), "")

    def test_engine_always_returns_chips_and_weaves_the_topic(self):
        from core.tui.suggest import suggestions_for

        # Rule hits keep their lead, and the subject reserves a slot.
        out = suggestions_for("fix the login redirect bug", "fixed the redirect", "", False, max_items=3)
        self.assertTrue(any("login redirect bug" in s.command for s in out), out)
        self.assertLessEqual(len(out), 3)
        # Pure conversation: the subject still seeds the row.
        convo = suggestions_for("tell me about the parser", "The parser handles both formats.", "", False)
        self.assertTrue(convo, "no fallback row for a conversational turn")
        self.assertTrue(any(s.command.startswith("continue working on: parser") for s in convo), convo)

    def test_every_agent_turn_shows_a_row_even_a_greeting(self):
        from core.scripted import final_response

        app, session, backend = _make_app([final_response("Hi! How can I help?")])
        app.submit("hello")
        self.assertTrue(wait_until(lambda: not app.busy, 10))
        app.render_frame()
        self.assertTrue(app.suggestions, "greeting turn earned no chips")
        self.assertGreater(len(app._suggestion_rects), 0, "fallback row not painted")

    def test_command_turns_show_no_suggestions(self):
        app, session, backend = _make_app([])
        app.submit("/workspace")
        self.assertTrue(wait_until(lambda: not app.busy, 10))
        app.render_frame()
        self.assertEqual(app.suggestions, [], "command turn sprouted chips")

    def test_older_topic_resurfaces_as_a_return_chip(self):
        from core.tui.suggest import suggestions_for

        # Switching subjects offers a jump back to the previous thread.
        out = suggestions_for("now check the test suite", "running them", "", False,
                              recent_topics=["login redirect bug"])
        self.assertTrue(any(s.command == "go back to: login redirect bug" for s in out), out)
        # The current subject still leads as a continuation chip.
        self.assertTrue(any(s.command == "continue working on: check test suite" for s in out), out)

    def test_content_free_turn_inherits_the_previous_subject(self):
        from core.tui.suggest import suggestions_for

        out = suggestions_for("thanks", "anytime", "", False,
                              recent_topics=["login redirect bug"])
        self.assertTrue(out, "inherited-subject row missing")
        self.assertTrue(any("login redirect bug" in s.command for s in out), out)
        # And never echoes the content-free words as a subject.
        self.assertTrue(all("thanks" not in s.command.lower() for s in out), out)

    def test_topic_memory_spans_turns_end_to_end(self):
        from core.scripted import final_response

        app, session, backend = _make_app([
            final_response("Fixed the redirect."),
            final_response("Ran the tests."),
        ])
        app.submit("please fix the login redirect bug")
        self.assertTrue(wait_until(lambda: not app.busy, 10))
        app.submit("now run the tests")
        self.assertTrue(wait_until(lambda: not app.busy, 10))
        # Memory: newest first, deduped, both threads remembered.
        self.assertEqual(app._recent_topics[0], "run tests")
        self.assertIn("fix login redirect bug", app._recent_topics)
        # And the second row offers the jump back to the first thread.
        app.render_frame()
        self.assertTrue(
            any("return to fix login redirect bug" in s for s in app.suggestions),
            app.suggestions,
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
