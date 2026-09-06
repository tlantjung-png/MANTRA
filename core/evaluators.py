"""Evaluator: passes when test command exits zero."""
from __future__ import annotations


from core.agent.approvals import _redact_sensitive
from core.types import EvaluationResult, Evaluator
from core.types import Sandbox


class CommandEvaluator(Evaluator):
    def __init__(self, test_cmd: str, timeout: float = 600.0) -> None:
        self.test_cmd = test_cmd
        self.timeout = timeout

    def evaluate(self, sandbox: Sandbox, task: dict) -> EvaluationResult:
        command = task.get("test_cmd", self.test_cmd)
        # task JSON is untrusted: a null or non-string test_cmd would
        # raise a raw TypeError inside sandbox.exec, so reject it as a
        # failed evaluation instead.
        if not isinstance(command, str) or not command.strip():
            return EvaluationResult(
                passed=False,
                detail=f"test_cmd must be a non-empty string, got {command!r}",
            )
        result = sandbox.exec(command, timeout=self.timeout)
        passed = result.exit_code == 0 and not result.timed_out
        # The last 4KB of tool output can carry credentials the agent
        # printed; redact known secret patterns before persisting.
        tail = _redact_sensitive((result.stdout + result.stderr)[-4000:])
        return EvaluationResult(
            passed=passed,
            detail=(
                f"test_cmd exited {result.exit_code}"
                + (" (timed out)" if result.timed_out else "")
                + f"; output tail:\n{tail}"
            ),
        )
"""Evaluator: always passes for interactive sessions."""


from core.types import EvaluationResult, Evaluator
from core.types import Sandbox


class NullEvaluator(Evaluator):
    def evaluate(self, sandbox: Sandbox, task: dict) -> EvaluationResult:
        return EvaluationResult(
            passed=True, detail="interactive run (no automatic grading)"
        )
