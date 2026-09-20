"""Logger and evaluators: rotation, never-raise contracts, validation."""

from __future__ import annotations

import json

import pytest

from core.evaluators import CommandEvaluator, NullEvaluator
from core.logs import JsonlLogger


class _FakeSandbox:
    """Minimal Sandbox stand-in returning a canned exec result."""

    def __init__(self, exit_code: int, stdout: str = "", stderr: str = "") -> None:
        from core.types import ExecResult

        self._result = ExecResult(exit_code=exit_code, stdout=stdout, stderr=stderr)
        self.last_command: str | None = None

    def exec(self, command: str, timeout: float = 120.0):
        self.last_command = command
        return self._result


def test_jsonl_writes_and_reads_back(tmp_path) -> None:
    logger = JsonlLogger(str(tmp_path / "run.jsonl"))
    logger.log("evt", {"k": 1})
    logger.close()
    lines = (tmp_path / "run.jsonl").read_text(encoding="utf-8").splitlines()
    record = json.loads(lines[0])
    assert record["event"] == "evt"
    assert record["k"] == 1
    assert "ts" in record


def test_jsonl_never_raises_on_unserializable(tmp_path) -> None:
    logger = JsonlLogger(str(tmp_path / "run.jsonl"))

    class _Odd:
        def __repr__(self) -> str:
            return "<odd>"

    logger.log("evt", {"x": _Odd()})
    logger.close()
    record = json.loads((tmp_path / "run.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert record["x"] == "<odd>"


def test_jsonl_rotation(tmp_path) -> None:
    path = tmp_path / "run.jsonl"
    logger = JsonlLogger(str(path))
    big = "x" * 10_000
    for i in range(150):
        logger.log("evt", {"i": i, "blob": big})
    logger.close()
    # Original stays bounded and a backup exists.
    assert path.stat().st_size < 1_000_000
    assert (tmp_path / "run.jsonl.1").exists()


def test_jsonl_close_idempotent(tmp_path) -> None:
    logger = JsonlLogger(str(tmp_path / "run.jsonl"))
    logger.log("evt", {})
    logger.close()
    logger.close()  # must not raise
    logger.log("evt2", {})  # reopens lazily; must not raise
    logger.close()


def test_command_evaluator_passes_on_zero_exit() -> None:
    evaluator = CommandEvaluator(test_cmd="true")
    result = evaluator.evaluate(_FakeSandbox(0), {})
    assert result.passed is True


def test_command_evaluator_fails_on_nonzero() -> None:
    evaluator = CommandEvaluator(test_cmd="false")
    result = evaluator.evaluate(_FakeSandbox(3, stderr="boom"), {})
    assert result.passed is False
    assert "exited 3" in result.detail


def test_command_evaluator_fails_on_timeout() -> None:
    from core.types import ExecResult

    class _TimedOut(_FakeSandbox):
        def exec(self, command: str, timeout: float = 120.0):
            self.last_command = command
            return ExecResult(exit_code=-1, stdout="", stderr="", timed_out=True)

    result = CommandEvaluator(test_cmd="x").evaluate(_TimedOut(0), {})
    assert result.passed is False


def test_command_evaluator_uses_task_override() -> None:
    evaluator = CommandEvaluator(test_cmd="default")
    sandbox = _FakeSandbox(0)
    evaluator.evaluate(sandbox, {"test_cmd": "override"})
    assert sandbox.last_command == "override"


def test_command_evaluator_rejects_bad_task_test_cmd() -> None:
    evaluator = CommandEvaluator(test_cmd="true")
    for bad in (None, 5, "   "):
        result = evaluator.evaluate(_FakeSandbox(0), {"test_cmd": bad})
        assert result.passed is False


def test_command_evaluator_timeout_validation() -> None:
    with pytest.raises(ValueError):
        CommandEvaluator(test_cmd="x", timeout=0)
    with pytest.raises(ValueError):
        CommandEvaluator(test_cmd="x", timeout=601)
    with pytest.raises(ValueError):
        CommandEvaluator(test_cmd="x", timeout="soon")  # type: ignore[arg-type]


def test_command_evaluator_swallows_sandbox_crash() -> None:
    class _Crashing(_FakeSandbox):
        def exec(self, command: str, timeout: float = 120.0):
            raise RuntimeError("sandbox exploded")

    result = CommandEvaluator(test_cmd="x").evaluate(_Crashing(0), {})
    assert result.passed is False
    assert "evaluator error" in result.detail


def test_null_evaluator_always_passes() -> None:
    result = NullEvaluator().evaluate(None, {})  # type: ignore[arg-type]
    assert result.passed is True
