"""Tests for /fix: last-failure capture, the fix prompt, the indicator,
and the attention bell."""

import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

os.environ.setdefault("MANTRA_SETTINGS", tempfile.mkdtemp())

from core.config import merge_defaults  # noqa: E402
from core.console import ConsoleSession, dispatch  # noqa: E402
from core.scripted import ScriptedLLMClient  # noqa: E402


def _session(workspace: str | None = None):
    ws = workspace or tempfile.mkdtemp(prefix="mantra-fix-")
    return ConsoleSession(
        config=merge_defaults({}),
        workspace=ws,
        style=__import__("core.console", fromlist=["Style"]).Style(enabled=False),
        llm=ScriptedLLMClient([]),
        ask=lambda p: "y",
    )


class CaptureLastErrorTest(unittest.TestCase):
    def test_error_observation_is_kept(self):
        s = _session()
        s._capture_last_error("read_file", "ERROR: no such file: x.py")
        self.assertEqual(s.last_error, "[read_file] ERROR: no such file: x.py")

    def test_nonzero_exit_is_kept(self):
        s = _session()
        s._capture_last_error("run_command", "exit_code: 2\nfail\n")
        self.assertIn("[run_command]", s.last_error)
        self.assertIn("exit_code: 2", s.last_error)

    def test_zero_exit_is_not_a_failure(self):
        s = _session()
        s._capture_last_error("run_command", "exit_code: 0\nok\n")
        self.assertIsNone(s.last_error)

    def test_grep_no_matches_is_not_a_failure(self):
        s = _session()
        s._capture_last_error("run_command", "exit_code: 1 (grep: no matches — not an error)\n")
        self.assertIsNone(s.last_error)

    def test_capture_via_the_observation_hook(self):
        s = _session()
        s._on_tool_observation("run_command", "ERROR: command too long", 1)
        self.assertEqual(s.last_error, "[run_command] ERROR: command too long")


class FixPromptTest(unittest.TestCase):
    def test_prompt_carries_the_failure_and_hint(self):
        s = _session()
        s.last_error = "[run_command] exit_code: 2\nboom"
        prompt = s._fix_prompt("path looks wrong")
        self.assertIn("exit_code: 2", prompt)
        self.assertIn("boom", prompt)
        self.assertIn("path looks wrong", prompt)
        self.assertIn("Do NOT run any", prompt)

    def test_no_failure_means_no_prompt(self):
        s = _session()
        self.assertIsNone(s._fix_prompt())

    def test_a_fresh_turn_clears_the_error(self):
        s = _session()
        s.last_error = "old failure"
        fake_loop = mock.MagicMock()
        fake_loop.run.return_value = None
        with mock.patch("core.console.AgentLoop", return_value=fake_loop), \
             mock.patch.object(s, "_record_memory"), mock.patch.object(s, "autosave"):
            s.handle("hello")
        self.assertIsNone(s.last_error)


class FixDispatchTest(unittest.TestCase):
    def test_fix_runs_the_agent_with_the_prompt(self):
        s = _session()
        s.last_error = "[run_command] exit_code: 1\nnope"
        with mock.patch.object(s, "handle", return_value=None) as fake, redirect_stdout(io.StringIO()):
            dispatch(s, "/fix")
        self.assertTrue(fake.called)
        prompt = fake.call_args[0][0]
        self.assertIn("exit_code: 1", prompt)

    def test_fix_without_a_failure_says_so(self):
        s = _session()
        buf = io.StringIO()
        with mock.patch.object(s, "handle") as fake, redirect_stdout(buf):
            dispatch(s, "/fix")
        fake.assert_not_called()
        self.assertIn("no recent failure", buf.getvalue())


class AttentionAndIndicatorTest(unittest.TestCase):
    def test_bell_writes_a_bel(self):
        s = _session()
        with mock.patch("sys.stdout") as out:
            s._attention()
        out.write.assert_called_with("\x07")

    def test_a_failed_turn_rings_the_bell(self):
        s = _session()
        result = mock.MagicMock()
        result.stopped_reason = "error"
        result.metrics = {"denied": 0, "tool_errors": 1}
        result.final_message = "it broke"
        fake_loop = mock.MagicMock()
        fake_loop.run.return_value = result
        with mock.patch("core.console.AgentLoop", return_value=fake_loop), \
             mock.patch.object(s, "_attention") as bell, \
             mock.patch.object(s, "_print"), \
             mock.patch.object(s, "_record_memory"), mock.patch.object(s, "autosave"), \
             mock.patch.object(s, "_report_changes"):
            s.handle("run it")
        bell.assert_called_once()

    def test_info_bar_shows_the_fix_indicator(self):
        from tests.test_tui import _make_app

        app, session, _ = _make_app([])
        self.assertNotIn("[!]", app._info_text())
        session.last_error = "boom"
        self.assertIn("[!]", app._info_text())


if __name__ == "__main__":
    unittest.main()
