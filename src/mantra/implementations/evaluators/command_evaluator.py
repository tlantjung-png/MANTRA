"""Evaluator: passes when test command exits zero."""

from __future__ import annotations

from mantra.interfaces.evaluator import EvaluationResult, Evaluator
from mantra.interfaces.sandbox import Sandbox


class CommandEvaluator(Evaluator):
    def __init__(self, test_cmd: str, timeout: float = 600.0) -> None:
        self.test_cmd = test_cmd
        self.timeout = timeout

    def evaluate(self, sandbox: Sandbox, task: dict) -> EvaluationResult:
        command = task.get("test_cmd", self.test_cmd)
        result = sandbox.exec(command, timeout=self.timeout)
        passed = result.exit_code == 0 and not result.timed_out
        tail = (result.stdout + result.stderr)[-4000:]  # last 4KB of combined output
        return EvaluationResult(
            passed=passed,
            detail=(
                f"test_cmd exited {result.exit_code}"
                + (" (timed out)" if result.timed_out else "")
                + f"; output tail:\n{tail}"
            ),
        )
