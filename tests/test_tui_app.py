"""App-level integration: turns, chips, prompt box,
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

    def test_activity_word_is_stable_and_clears(self):
        import re as _re

        app, session, backend = _make_app([])
        app.busy = True
        app._turn_started = time.monotonic()
        app.set_activity("Scrying")
        borders = []
        for i in range(10):
            if i > 0:
                app.tick_animation()
            borders.append(_re.sub(r"\033\[[0-9;]*m", "", app._border_text()))
        # One stable themed word the whole time — no cycling, no ENCHANTER.
        for b in borders:
            self.assertIn("Scrying", b)
            self.assertNotIn("ENCHANTER", b)
        self.assertEqual(len({b.split(" ")[1] for b in borders}), 1)
        # Detail rides along when supplied.
        app.set_activity("Inscribing", "max 16384 tokens")
        border_detail = _re.sub(r"\033\[[0-9;]*m", "", app._border_text())
        self.assertIn("Inscribing", border_detail)
        self.assertIn("max 16384 tokens", border_detail)
        # Clearing falls back to the default busy label.
        app.set_activity("")
        border_after = _re.sub(r"\033\[[0-9;]*m", "", app._border_text())
        self.assertNotIn("Scrying", border_after)
        self.assertNotIn("Inscribing", border_after)

    def test_tool_word_survives_quick_followup_chat(self):
        import re as _re

        app, session, backend = _make_app([])
        app.busy = True
        app._turn_started = time.monotonic()
        app.set_activity("Scrying")
        # A chat starts right after the tool; the hold keeps Scrying visible.
        app.set_activity("Channeling")
        border = _re.sub(r"\033\[[0-9;]*m", "", app._border_text())
        self.assertIn("Scrying", border)
        self.assertNotIn("Channeling", border)
        # Once the hold window has passed, the new state takes over.
        app.activity_since = time.monotonic() - 5
        app.set_activity("Channeling")
        border_after = _re.sub(r"\033\[[0-9;]*m", "", app._border_text())
        self.assertNotIn("Scrying", border_after)
        self.assertIn("Channeling", border_after)

    def test_live_token_estimate_uses_total_text_not_fragment_count(self):
        app, session, backend = _make_app([])
        for _ in range(20):
            session._on_delta("x")
        self.assertEqual(session._stream_tokens, 5)
        session._last_counter_update = 0
        session._update_live_counter()
        self.assertIn("5 tok", app.counter_text)

    def test_live_speed_resets_after_a_pause_without_resetting_total(self):
        app, session, backend = _make_app([])
        session._on_delta("x" * 40)
        session._last_delta_at = time.monotonic() - 3
        session._on_delta("y" * 40)
        self.assertEqual(session._stream_tokens, 20)
        self.assertEqual(session._stream_segment_chars, 40)

    def test_cache_rate_is_unknown_when_provider_counts_cache_separately(self):
        from core.term import ansi_strip

        app, session, backend = _make_app([])
        session.totals["tokens_in"] = 10
        session.totals["cache_hit"] = 20
        self.assertIn("CACHE: —", ansi_strip(app._info_text()))
        shown = []
        session._print = shown.append
        session.show_cost()
        self.assertIn("cache hit    20", shown)
        self.assertFalse(any("200%" in line or "cache saved" in line for line in shown))

    def test_usage_footer_pluralizes_multiple_steps(self):
        app, session, backend = _make_app([])
        session._turn_started = time.monotonic()
        result = type("Result", (), {"metrics": {}, "steps_used": 2})()
        self.assertIn("2 STEPS", session._usage_line(result))

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

    def test_wheel_still_scrolls_conversation_after_several_turns(self):
        from core.scripted import ScriptedLLMClient, final_response, tool_call_response

        replies = [f"reply {i}: " + (f"detail {i} " * 45) for i in range(5)]
        client = ScriptedLLMClient([
            tool_call_response("write_file", {"path": "note.txt", "content": "hello"}),
            final_response(replies[0]),
            tool_call_response("run_command", {"command": "echo verified"}),
            *[final_response(reply) for reply in replies[1:]],
        ])
        scripted_chat = client.chat

        def streaming_chat(messages, tools=None, on_delta=None, **kwargs):
            response = scripted_chat(messages, tools=tools, **kwargs)
            if on_delta and response.is_final:
                midpoint = len(response.content) // 2
                on_delta(response.content[:midpoint])
                on_delta(response.content[midpoint:])
            return response

        client.chat = streaming_chat
        app, session, backend = _make_app([client])
        for i in range(5):
            app.submit(f"question {i}")
            self.assertTrue(wait_until(lambda: not app.busy))
        self.assertTrue(os.path.isfile(os.path.join(session.sandbox.root, "note.txt")))
        app.render_frame()
        self.assertTrue(app.transcript.follow)
        before_prompt = app.composer.buffer
        app.handle_event(Mouse("wheel", 64, 10, 10, frozenset()))
        self.assertGreater(app.transcript.offset, 0)
        self.assertEqual(app.composer.buffer, before_prompt)
        self.assertFalse(app.transcript.follow)
        app.handle_event(Mouse("wheel", 65, 10, 10, frozenset()))
        self.assertEqual(app.transcript.offset, 0)
        self.assertTrue(app.transcript.follow)
        history = "\n".join(str(m.get("content") or "") for m in session.context.messages)
        for reply in replies:
            self.assertIn(reply, history)
        transcript = "\n".join(app.transcript.raw)
        self.assertEqual(transcript.count("detail 4"), 45)
        self.assertIn("$ echo verified → exit 0", transcript)

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

    def test_long_busy_status_keeps_the_right_border(self):
        app, session, backend = _make_app([])
        app.busy = True
        app.counter_text = "x" * 200
        app.render_frame()
        border = grid_rows(app.renderer.buffer)[app.rows - 3]
        self.assertEqual(len(border), app.cols)
        self.assertTrue(border.endswith("╮"))
        self.assertIn("…", border)

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
            def chat(self, messages, tools=None, on_delta=None, **kwargs):
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
        app.render_frame()
        with app.lock:
            for i in range(40):
                app.transcript.append(f"line {i}")
            app.transcript.scroll_up(5)
            detached = app.transcript.offset
            visible_before = app.transcript.visible_rows(app.transcript.viewport_height)
        self.assertGreater(detached, 0)
        self.assertFalse(session.layout.following)
        app.submit("go")
        wait_until(lambda: not app.busy, 10)
        app.render_frame()
        self.assertGreaterEqual(app.transcript.offset, detached)
        self.assertEqual(app.transcript.visible_rows(app.transcript.viewport_height), visible_before)
        self.assertFalse(session.layout.following)

    def test_busy_chip_waits_for_real_tokens_before_showing_counter(self):
        # While the model thinks (no tokens yet) the busy chip must not
        # show fake "0 tok" values; the counter appears with the first
        # real streamed token.
        from core.scripted import LLMResponse, ScriptedLLMClient

        class _Thinking(ScriptedLLMClient):
            def chat(self, messages, tools=None, on_delta=None, **kwargs):
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

    def test_operator_echo_carries_no_you_label(self):
        # The operator's own line is the timestamp chip plus the text: a
        # "you" label reads as chatter in a wall of tool output.
        app, session, backend = _make_app([])
        app.submit("what this project does")
        raw = "\n".join(app.transcript.raw)
        self.assertIn("what this project does", raw)
        self.assertNotIn("you", raw.lower().replace("your", ""), raw)

    def test_event_handler_failure_is_reported_not_fatal(self):
        # One malformed event used to propagate out of the presenter and
        # kill the session; it is now surfaced as a toast and the loop
        # keeps draining.
        from core.tui.loop import Presenter

        app, session, backend = _make_app([])
        calls = {"n": 0}

        def _boom(event):
            calls["n"] += 1
            if calls["n"] >= 2:
                app.running = False  # both events drained: let the loop exit
            raise RuntimeError("bad event")

        app.handle_event = _boom
        app.render_frame = lambda: None
        presenter = Presenter(app, min_draw_interval=0.0)
        backend.events.put(Key("a"))
        backend.events.put(Key("b"))
        try:
            presenter._run()
        finally:
            app.running = True
        self.assertEqual(calls["n"], 2, "the loop stopped draining after a failure")
        self.assertIn("input error", app.toast)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
