"""End-to-end tests: the real console application driven through a
pseudoconsole (see ``conpty_harness.py``).

The pure-widget and app-level suites live in the topical
``test_tui_*.py`` modules; they share helpers from ``tui_harness.py``.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "."))
_tests_dir = os.path.dirname(os.path.abspath(__file__))
if _tests_dir not in sys.path:
    sys.path.insert(0, _tests_dir)


class EndToEndConsoleTest(unittest.TestCase):
    """The real console application, spawned and driven like a terminal.

    The harness owns the transport (pseudoconsole, or the headless conhost
    fallback on hosts whose ConPTY sessions never wire up). The child gets
    a scratch settings file and a dummy key so first-run setup never
    blocks, a scratch workspace, and a dead endpoint: everything here
    exercises the UI surface, never the network.
    """

    def test_help_roundtrip_and_input_wiring(self):
        if os.name != "nt":
            self.skipTest("ConPTY is Windows-only")
        import ctypes
        import json as jsonlib
        import shutil as shutillib
        from conpty_harness import k32, make_console

        work = tempfile.mkdtemp(prefix="mantra-e2e-")
        self.addCleanup(shutillib.rmtree, work, True)
        settings = os.path.join(work, "settings.json")
        with open(settings, "w", encoding="utf-8") as fh:
            jsonlib.dump(
                {
                    "active": {"endpoint": "local", "model": "test-model"},
                    "endpoints": {
                        "local": {
                            "name": "local",
                            "base_url": "http://localhost:9/v1",
                            "api_key_env": "MODEL_API_KEY",
                        }
                    },
                    "approvals": "default",
                },
                fh,
            )
        ws = os.path.join(work, "ws")
        os.makedirs(ws, exist_ok=True)
        repo = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

        saved = {name: os.environ.get(name) for name in ("MANTRA_SETTINGS", "MODEL_API_KEY", "PYTHONPATH")}
        os.environ["MANTRA_SETTINGS"] = settings
        os.environ["MODEL_API_KEY"] = "dummy-key-for-e2e"
        os.environ["PYTHONPATH"] = repo + os.pathsep + os.environ.get("PYTHONPATH", "")

        def restore_env():
            for name, value in saved.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value

        self.addCleanup(restore_env)

        pty = make_console(
            cols=110, rows=32,
            args=[sys.executable, "-m", "core.console", "--workspace", ws],
            cwd=repo,
        )
        self.addCleanup(pty.close)

        # 1. Boot: banner, chrome and composer all render, startup settles.
        pty.wait_composer_ready(timeout=60)

        # 2. Keys reach the composer and echo (wired input path).
        base = pty.text()
        seen = pty.type_and_wait_echo("z")
        self.assertIn("z", seen[len(base):], "typed key never echoed by the app")

        # 3. Paced /help round-trip. The app reads stdin in chunks, so a
        # newline in the same burst as the text can be delivered early;
        # pacing text and Enter is what a fast human types anyway.
        # Typing "/help" opens the completion popup (whose description
        # also contains "show help"), so the first Enter accepts the
        # completion and only the second one submits the command. The
        # assertion token is a help row the popup can never render.
        pty.type_text("\x7f" * 8)  # clear the probe character
        pty.type_and_wait_echo("/help")
        pty.enter()  # accept the completion
        # Acceptance repaints identical cells (no new text to wait for);
        # the echo above already proves the text was consumed, so a
        # short settle for the popup close is enough before submitting.
        time.sleep(0.3)
        pty.enter()  # submit
        # The viewport holds only the tail of the long help screen, so
        # anchor both the wait and the assertions at its last lines:
        # by the time they appear, the screen finished painting.
        seen = pty.wait_for("Ctrl+C once stops the current run", timeout=20)
        self.assertIn("Reference files with @ in any message", seen, "help text incomplete")

        # 4. The child is still healthy after the round-trip.
        code = ctypes.c_ulong(0)
        k32.GetExitCodeProcess(pty._hprocess, ctypes.byref(code))
        self.assertEqual(code.value, 259, "console exited during the help round-trip")  # STILL_ACTIVE

    def test_tool_call_and_approval_card(self):
        """One compact agent turn that reaches a tool-call gate.

        The scripted LLM yields a single mutating tool call
        (write_file) instead of a final answer, so the interactive
        session routes the call to the approval policy and the TUI
        presents the ``allow?`` QuestionCard. The test types ``y``
        to confirm, then watches for two side channels of the same
        event: the approval banner in the transcript and the tool
        result streamed as the operator sees it.

        The exact wording of the approval prompt is policy output
        (it changes when the tool set changes), so the assertions
        anchor on the card title and the affirmative key hints,
        which are stable and cannot be produced by the composer.
        """
        if os.name != "nt":
            self.skipTest("ConPTY is Windows-only")
        import ctypes
        import json as jsonlib
        import shutil as shutillib
        from conpty_harness import k32, make_console

        work = tempfile.mkdtemp(prefix="mantra-e2e-tool-")
        self.addCleanup(shutillib.rmtree, work, True)
        settings = os.path.join(work, "settings.json")
        with open(settings, "w", encoding="utf-8") as fh:
            jsonlib.dump(
                {
                    "active": {"endpoint": "local", "model": "test-model"},
                    "endpoints": {
                        "local": {
                            "name": "local",
                            "base_url": "http://localhost:9/v1",
                            "api_key_env": "MODEL_API_KEY",
                        }
                    },
                    "approvals": "default",
                },
                fh,
            )
        ws = os.path.join(work, "ws")
        os.makedirs(ws, exist_ok=True)
        repo = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

        # The scripted conversation: one mutating tool call, then the
        # final answer. MANTRA_SCRIPT makes build_llm hand the live
        # console this script, so the turn never touches the network.
        script = os.path.join(work, "script.json")
        with open(script, "w", encoding="utf-8") as fh:
            jsonlib.dump(
                [
                    {"tool_calls": [
                        {"name": "write_file",
                         "arguments": {"path": "greeting.txt", "content": "hello from the approval test"}}
                    ]},
                    {"content": "wrote the greeting file"},
                ],
                fh,
            )

        saved = {name: os.environ.get(name) for name in
                 ("MANTRA_SETTINGS", "MANTRA_SCRIPT", "MODEL_API_KEY", "PYTHONPATH")}
        os.environ["MANTRA_SETTINGS"] = settings
        os.environ["MANTRA_SCRIPT"] = script
        os.environ["MODEL_API_KEY"] = "dummy-key-for-e2e"
        os.environ["PYTHONPATH"] = repo + os.pathsep + os.environ.get("PYTHONPATH", "")

        def restore_env():
            for name, value in saved.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value

        self.addCleanup(restore_env)

        pty = make_console(
            cols=110, rows=32,
            args=[sys.executable, "-m", "core.console",
                  "--workspace", ws, "--approve", "default"],
            cwd=repo,
        )
        self.addCleanup(pty.close)

        # 1. Boot and clear whatever the banner placed on the composer.
        pty.wait_composer_ready(timeout=60)
        pty.type_text("\x7f" * 8)
        time.sleep(0.3)  # settle: idle probes cannot fire while a turn repaints

        # 2. Submit the scripted prompt. With approvals=default the
        # mutating tool call gates on the interactive card: the card
        # title and the affirmative key hints are stable anchors that
        # the composer or banner can never produce.
        pty.type_and_wait_echo("write a greeting file")
        pty.enter()

        seen_card = pty.wait_for("[y]es   [n]o   [a]lways for this session", timeout=60)
        self.assertIn("allow?", seen_card, "approval card title missing")

        # 3. Confirm the tool call by typing the answer on the card. The
        # card is modal (no composer echo) and the turn's live counter
        # repaints continuously, so pacing here is a bounded settle.
        time.sleep(0.4)  # the card finished painting
        pty.type_text("y")
        time.sleep(0.4)
        pty.enter()

        # 4. The turn finishes with the scripted final reply and the
        # ENCHANTER banner - all hermetic, no network involved.
        result = pty.wait_for("wrote the greeting file", timeout=60)
        self.assertIn("ENCHANTER", result, "final reply banner missing")

        # 5. The child is still healthy after the full approval turn.
        code = ctypes.c_ulong(0)
        k32.GetExitCodeProcess(pty._hprocess, ctypes.byref(code))
        self.assertEqual(code.value, 259, "console exited during the tool approval test")  # STILL_ACTIVE

    def test_mouse_wheel_scrolls_transcript_end_to_end(self):
        """Real wheel reports through a pseudoconsole scroll the transcript.

        The harness injects the exact SGR byte stream a terminal sends
        for scroll-wheel motion; the child must repaint the scrolled
        transcript (the `` ^N`` detached marker on the composer border)
        and never type anything into the prompt box.
        """
        if os.name != "nt":
            self.skipTest("ConPTY is Windows-only")
        import ctypes
        import json as jsonlib
        import shutil as shutillib
        from conpty_harness import k32, make_console

        # The headless-conhost fallback transport swallows SGR mouse
        # bytes entirely (no MOUSE_EVENT records, no key decomposition),
        # so the wheel chain can only be exercised on a host whose
        # pseudoconsole sessions deliver output.
        from conpty_harness import _ConsoleSession

        if _ConsoleSession._transport != "pseudoconsole":
            self.skipTest("host pseudoconsole unusable; mouse bytes are dropped by the conhost fallback")

        work = tempfile.mkdtemp(prefix="mantra-e2e-wheel-")
        self.addCleanup(shutillib.rmtree, work, True)
        settings = os.path.join(work, "settings.json")
        with open(settings, "w", encoding="utf-8") as fh:
            jsonlib.dump(
                {
                    "active": {"endpoint": "local", "model": "test-model"},
                    "endpoints": {
                        "local": {
                            "name": "local",
                            "base_url": "http://localhost:9/v1",
                            "api_key_env": "MODEL_API_KEY",
                        }
                    },
                    "approvals": "default",
                },
                fh,
            )
        ws = os.path.join(work, "ws")
        os.makedirs(ws, exist_ok=True)
        repo = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

        saved = {name: os.environ.get(name) for name in
                 ("MANTRA_SETTINGS", "MANTRA_SCRIPT", "MODEL_API_KEY", "PYTHONPATH")}
        os.environ["MANTRA_SETTINGS"] = settings
        os.environ.pop("MANTRA_SCRIPT", None)  # live console, scripted nothing
        os.environ["MODEL_API_KEY"] = "dummy-key-for-e2e"
        os.environ["PYTHONPATH"] = repo + os.pathsep + os.environ.get("PYTHONPATH", "")

        def restore_env():
            for name, value in saved.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value

        self.addCleanup(restore_env)

        # Produce more transcript rows than a 30-row viewport holds:
        # /help prints a long, static, offline-safe screen.
        pty = make_console(
            cols=110, rows=30,
            args=[sys.executable, "-m", "core.console", "--workspace", ws],
            cwd=repo,
        )
        self.addCleanup(pty.close)

        pty.wait_composer_ready(timeout=60)
        pty.type_and_wait_echo("/help")
        pty.enter()  # accept completion
        # Same reasoning as the help round-trip: no repaint text marks
        # acceptance; the echo pacing plus a short settle is enough.
        time.sleep(0.3)
        pty.enter()  # submit
        pty.wait_for("Ctrl+C once stops the current run", timeout=20)
        pty.wait_for_output_idle()  # the final frame settles

        # Ten wheel-up reports at the same cell, paced like a real wheel.
        for _ in range(10):
            pty.wheel(64, 55, 15)
            time.sleep(0.08)  # pace the stimulus like a real wheel
        pty.wait_for_output_idle()  # the scrolled repaint settles

        # The transcript detached from the tail: the border must show the
        # scroll marker. A composer that swallowed the wheel would instead
        # show recalled history lines in the prompt box.
        tail = pty.text()[-6000:]
        self.assertRegex(tail, r"\^\d+", "no scroll marker after wheel-up: the transcript never detached")

        # Wheel back down to the tail and confirm the marker clears.
        for _ in range(14):
            pty.wheel(65, 55, 15)
            time.sleep(0.08)  # pace the stimulus like a real wheel
        pty.wait_for_output_idle()  # the re-follow repaint settles
        tail = pty.text()[-4000:]
        # Repaints continue after re-follow; the last border row painted
        # must not carry a marker.
        border_rows = [ln for ln in tail.splitlines() if ln.startswith("\u256d")]
        self.assertTrue(border_rows, "no composer border painted after wheel-down")
        self.assertNotIn("^", border_rows[-1], "scroll marker still shown after scrolling back to the tail")

        # The child is still healthy, and the composer stayed empty.
        code = ctypes.c_ulong(0)
        k32.GetExitCodeProcess(pty._hprocess, ctypes.byref(code))
        self.assertEqual(code.value, 259, "console exited during the wheel test")  # STILL_ACTIVE
        self.assertNotIn("/help", pty.text()[pty.text().rfind("Ctrl+C once stops the current run"):],
                         "wheel reports leaked into the composer as text")

    def _boot(self, work, cols, rows, extra_env=None, args_extra=()):
        """Shared boot for the cramped-frame E2E tests: settings on a dead
        endpoint, scratch workspace, live console, no network."""
        import json as jsonlib
        settings = os.path.join(work, "settings.json")
        with open(settings, "w", encoding="utf-8") as fh:
            jsonlib.dump(
                {
                    "active": {"endpoint": "local", "model": "test-model"},
                    "endpoints": {
                        "local": {
                            "name": "local",
                            "base_url": "http://localhost:9/v1",
                            "api_key_env": "MODEL_API_KEY",
                        }
                    },
                    "approvals": "default",
                },
                fh,
            )
        ws = os.path.join(work, "ws")
        os.makedirs(ws, exist_ok=True)
        # A file the @-completer can offer.
        with open(os.path.join(ws, "note.txt"), "w", encoding="utf-8") as fh:
            fh.write("x\n")
        repo = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

        names = ("MANTRA_SETTINGS", "MODEL_API_KEY", "PYTHONPATH")
        saved = {name: os.environ.get(name) for name in names}
        os.environ["MANTRA_SETTINGS"] = settings
        os.environ["MODEL_API_KEY"] = "dummy-key-for-e2e"
        os.environ["PYTHONPATH"] = repo + os.pathsep + os.environ.get("PYTHONPATH", "")
        for name, value in (extra_env or {}).items():
            saved[name] = os.environ.get(name)
            os.environ[name] = value

        def restore_env():
            for name, value in saved.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value

        self.addCleanup(restore_env)

        from conpty_harness import make_console
        pty = make_console(
            cols=cols, rows=rows,
            args=[sys.executable, "-m", "core.console", "--workspace", ws, *args_extra],
            cwd=repo,
        )
        self.addCleanup(pty.close)
        return pty

    def test_popup_and_chrome_survive_a_cramped_terminal(self):
        """The '/' dropdown inside a 60x12 console.

        Small-terminal regressions the probe suite covers only in
        isolation: the popup must open, stay inside the frame, stop
        clear of the composer box (the typed '/' stays visible), and
        dismiss cleanly with backspace so typing continues.
        """
        if os.name != "nt":
            self.skipTest("ConPTY is Windows-only")
        import ctypes
        from conpty_harness import k32

        work = tempfile.mkdtemp(prefix="mantra-e2e-cramped-")
        self.addCleanup(shutil.rmtree, work, True)
        pty = self._boot(work, cols=60, rows=12)
        pty.wait_composer_ready(timeout=60)

        # Open the command popup. The dropdown paints immediately (its
        # first item is the real /approve command, the head of the
        # alphabetical catalogue), and the prompt row must still show the
        # typed token.
        pty.type_and_wait_echo("/")
        tail = pty.text()[-4000:]
        self.assertIn("/approve", tail, "completion dropdown never rendered")

        # The composer box painted below the dropdown (the popup's
        # geometry guarantees live in SmallTerminalTest; here we verify
        # the real console renders both surfaces without dying).
        self.assertIn("MANTRA >", tail, "composer box never painted")

        # Backspace dismisses the popup: the dropdown is a live window,
        # so opening it again must re-paint its rows. Counting the
        # popup's distinctive overflow marker is observable in the raw
        # VT stream where screen erasures are not.
        marker = "more (click or keep pressing down)"
        first_paints = pty.text().count(marker)
        self.assertGreaterEqual(first_paints, 1, "dropdown marker never painted")

        pty.type_text("")  # dismiss
        pty.wait_for_output_idle()  # the dismissal repaint settles
        pty.type_and_wait_echo("/")     # re-open
        second_paints = pty.wait_for_count(marker, first_paints + 1)
        self.assertGreater(second_paints, first_paints, "popup never re-opened after backspace dismissed it")

        # Dismiss again and submit a plain message: the input pipeline
        # still works end-to-end (the dead endpoint just spins).
        pty.type_text("")
        time.sleep(0.3)  # the dismissal repaint settles
        pty.type_and_wait_echo("hi")
        pty.enter()
        # The turn against the dead endpoint spins a live counter that
        # repaints continuously, so only the transcript echo is a signal.
        pty.wait_for_count("hi", 2, timeout=20)
        self.assertIn("hi", pty.text()[-3000:], "keys after dismissing the popup were dropped")

        # The child is still healthy.
        code = ctypes.c_ulong(0)
        k32.GetExitCodeProcess(pty._hprocess, ctypes.byref(code))
        self.assertEqual(code.value, 259, "console exited during the cramped-popup test")  # STILL_ACTIVE

    def test_approval_card_is_answerable_on_a_tiny_console(self):
        """A 60x12 console still paints and answers the approval card.

        The minimum-frame gate lives in the TUI, so a modal that only a
        real resize below MIN_ROWS can surface is exercised end-to-end
        here: a scripted mutating tool call opens the ``allow?`` card in
        a cramped console, and typing ``y`` answers it.
        """
        if os.name != "nt":
            self.skipTest("ConPTY is Windows-only")
        import ctypes
        import json as jsonlib
        from conpty_harness import k32

        work = tempfile.mkdtemp(prefix="mantra-e2e-cramped-card-")
        self.addCleanup(shutil.rmtree, work, True)

        # Scripted turn: one mutating tool call, then the final answer.
        script = os.path.join(work, "script.json")
        with open(script, "w", encoding="utf-8") as fh:
            jsonlib.dump(
                [
                    {"tool_calls": [
                        {"name": "write_file",
                         "arguments": {"path": "greeting.txt", "content": "hello cramped"}}
                    ]},
                    {"content": "wrote the greeting file"},
                ],
                fh,
            )

        # Interactive gate: the settings key is not the CLI override, and
        # the default mode on this surface is auto (mutating writes
        # allowed), so pin "default" to make the card appear.
        pty = self._boot(
            work, cols=60, rows=12,
            extra_env={"MANTRA_SCRIPT": script},
            args_extra=("--approve", "default"),
        )
        pty.wait_composer_ready(timeout=60)
        pty.type_text("" * 8)  # clear any banner probe characters
        time.sleep(0.3)  # settle: the scripted turn repaints while active

        pty.type_and_wait_echo("write a greeting file")
        pty.enter()

        # The approval card renders even in the cramped frame, and the
        # y-key answers it through the same path as the full-size UI.
        pty.wait_for("[y]es", timeout=60)
        # Modal card (no composer echo) with the turn's counter live:
        # bounded settles are the only reliable pacing here.
        time.sleep(0.4)  # the card finished painting
        pty.type_text("y")
        time.sleep(0.4)
        pty.enter()

        seen = pty.wait_for("wrote the greeting file", timeout=60)
        self.assertIn("greeting", seen.lower(), "turn never completed after approval")

        code = ctypes.c_ulong(0)
        k32.GetExitCodeProcess(pty._hprocess, ctypes.byref(code))
        self.assertEqual(code.value, 259, "console exited during the cramped-card test")  # STILL_ACTIVE


class ConPtyHarnessTest(unittest.TestCase):
    """The ConPTY harness: spawn a real child console process, reap it.

    The payload script writes a marker file, so the test asserts the
    child actually executed without depending on the console output pipe
    round-trip. Windows-only: the pseudoconsole API does not exist on
    POSIX.
    """

    def test_harness_spawns_and_reaps_a_child(self):
        if os.name != "nt":
            self.skipTest("ConPTY is Windows-only")
        from conpty_harness import make_console

        work = tempfile.mkdtemp(prefix="mantra-conpty-")
        self.addCleanup(shutil.rmtree, work, True)
        marker = os.path.join(work, "child-ran.txt")
        payload = os.path.join(work, "payload.py")
        with open(payload, "w", encoding="utf-8") as fh:
            fh.write(f"open({marker!r}, 'w').write('ran')\n")
        pty = make_console(cols=40, rows=10, args=[sys.executable, payload])
        self.assertGreater(pty.pid, 0)
        try:
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline and not os.path.isfile(marker):
                time.sleep(0.1)
            self.assertTrue(os.path.isfile(marker), "ConPTY child never ran")
        finally:
            pty.close()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
