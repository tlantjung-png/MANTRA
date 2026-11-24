"""Closing-summary contract for every interactive turn."""

import unittest
from types import SimpleNamespace

from core.agent.loop import RunResult
from core.console import ConsoleSession


class _Style:
    def brand(self, text: str) -> str:
        return text


class ClosingSummaryTest(unittest.TestCase):
    def _session(self, changed=()):
        return SimpleNamespace(
            style=_Style(),
            sandbox=SimpleNamespace(changed=set(changed)),
        )

    def test_final_turn_ends_with_plain_language_summary(self):
        result = RunResult(
            task_id="t",
            passed=True,
            evaluation_detail="interactive run (no automatic grading)",
            steps_used=3,
            stopped_reason="final",
            elapsed_seconds=1.0,
        )
        out = ConsoleSession._closing_summary(self._session({"app.py"}), result)
        self.assertIn("Summary:", out)
        self.assertIn("The request was answered in 3 steps", out)
        self.assertIn("1 file changed", out)
        self.assertIn("Verification: interactive run", out)
        self.assertIn("Next:", out)
        self.assertNotIn("```", out)

    def test_error_turn_explains_the_stop_and_next_step(self):
        result = RunResult(
            task_id="t",
            passed=False,
            evaluation_detail="",
            steps_used=1,
            stopped_reason="error",
            elapsed_seconds=0.5,
        )
        out = ConsoleSession._closing_summary(self._session(), result)
        self.assertIn("stopped with an error", out)
        self.assertIn("incomplete (error)", out)
        self.assertIn("Next:", out)
        self.assertNotIn("```", out)

    def test_missing_result_is_explained(self):
        out = ConsoleSession._closing_summary(self._session(), None)
        self.assertIn("before a result was produced", out)
        self.assertIn("Next:", out)
        self.assertNotIn("```", out)


if __name__ == "__main__":
    unittest.main()
