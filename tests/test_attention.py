"""Tests for the attention bell and the failure surfaces that remain.

The /fix command and its [!] FIX info-bar indicator are gone: a failure
surfaces in the conversation itself (the error line the loop prints) and
in the post-task suggestion row, which leads with a diagnosis step. What
remains under test is the bell for a failed turn or a denial, and the
fact that no chrome re-advertises a removed command.
"""

import atexit
import os
import tempfile
import unittest
from unittest import mock

# Isolate the settings store for direct runs of this file; suite runs
# already redirect it in tests/__init__.py. The temp dir is removed at
# process exit instead of leaking.
_SETTINGS_TMP = tempfile.TemporaryDirectory(prefix="mantra-attention-settings-")
os.environ.setdefault("MANTRA_SETTINGS", os.path.join(_SETTINGS_TMP.name, "config.json"))
atexit.register(_SETTINGS_TMP.cleanup)

from core.config import merge_defaults  # noqa: E402
from core.console import ConsoleSession, Style  # noqa: E402
from core.scripted import ScriptedLLMClient  # noqa: E402


def _session(workspace: str | None = None):
    ws = workspace or tempfile.mkdtemp(prefix="mantra-attention-")
    return ConsoleSession(
        config=merge_defaults({}),
        workspace=ws,
        style=Style(enabled=False),
        llm=ScriptedLLMClient([]),
        ask=lambda p: "y",
    )


class AttentionBellTest(unittest.TestCase):
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


class NoFixChromeTest(unittest.TestCase):
    """Nothing re-advertises the removed /fix command."""

    def test_the_info_bar_has_no_failure_indicator(self):
        from tests.tui_harness import _make_app

        app, session, _ = _make_app([])
        # A failure state is the loop's business; the bar shows workspace,
        # model, approval, and cache only.
        info = app._info_text()
        self.assertNotIn("[!]", info)
        self.assertNotIn("FIX", info.upper())

    def test_a_failed_turn_leaves_no_indicator_behind(self):
        from tests.tui_harness import _make_app

        app, session, _ = _make_app([])
        result = mock.MagicMock()
        result.stopped_reason = "error"
        result.metrics = {"denied": 0, "tool_errors": 1}
        result.final_message = "LLM request failed"
        fake_loop = mock.MagicMock()
        fake_loop.run.return_value = result
        with mock.patch("core.console.AgentLoop", return_value=fake_loop), \
             mock.patch.object(session, "_print"), \
             mock.patch.object(session, "_record_memory"), \
             mock.patch.object(session, "autosave"), \
             mock.patch.object(session, "_report_changes"):
            session.handle("run it")
        self.assertNotIn("[!]", app._info_text())

    def test_the_session_keeps_no_failure_state_for_a_command(self):
        s = _session()
        # The old capture hook stored the last failure for /fix; with the
        # command gone there is nothing to store it for.
        self.assertFalse(hasattr(s, "last_error"))


if __name__ == "__main__":
    unittest.main()
