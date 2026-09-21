"""Agent loop: seeding, tool dispatch, repeat blocking, abort, metrics."""

from __future__ import annotations



from core.agent.events import EventBus
from core.agent.loop import AgentLoop
from core.agent.exceptions import AbortError
from core.evaluators import NullEvaluator
from core.logs import JsonlLogger
from core.sandbox import LocalSandbox
from core.scripted import final_response, tool_call_response
from core.tools.files import ListDirTool, ReadFileTool, WriteFileTool
from core.tools.ledger import EditLedger


def _loop(script, tmp_path, **kwargs) -> tuple[AgentLoop, LocalSandbox]:
    sandbox = LocalSandbox(workspace_root=str(tmp_path))
    sandbox.setup({})
    tools = [ReadFileTool(), WriteFileTool(), ListDirTool()]
    ledger = EditLedger()
    for tool in tools:
        if hasattr(tool, "ledger"):
            tool.ledger = ledger
    logger = JsonlLogger(str(tmp_path / "run.jsonl"))
    loop = AgentLoop(
        llm=script,
        sandbox=sandbox,
        tools=tools,
        evaluator=NullEvaluator(),
        logger=logger,
        max_steps=kwargs.pop("max_steps", 6),
        **kwargs,
    )
    return loop, sandbox


def _task(**overrides) -> dict:
    task = {"task_id": "t1", "problem_statement": "do the thing"}
    task.update(overrides)
    return task


def test_final_answer_passes(tmp_path) -> None:
    loop, sandbox = _loop(_script_client(final_response("done")), tmp_path)
    result = loop.run(_task())
    assert result.passed is True
    assert result.stopped_reason == "final"
    assert result.final_message == "done"
    sandbox.cleanup()


def test_tool_call_executes_and_returns_observation(tmp_path) -> None:
    script = _script_client(
        tool_call_response("write_file", {"path": "out.txt", "content": "hi"}),
        final_response("wrote it"),
    )
    loop, sandbox = _loop(script, tmp_path)
    result = loop.run(_task())
    assert result.stopped_reason == "final"
    assert (tmp_path / "out.txt").read_text(encoding="utf-8") == "hi"
    sandbox.cleanup()


def test_unknown_tool_becomes_error_observation(tmp_path) -> None:
    script = _script_client(
        tool_call_response("no_such_tool", {}),
        final_response("gave up"),
    )
    loop, sandbox = _loop(script, tmp_path)
    loop.run(_task())
    # The second request must have seen the tool error observation.
    second = script.received_messages[1]
    tool_msgs = [m for m in second if m.get("role") == "tool"]
    assert tool_msgs and "unknown tool" in tool_msgs[-1]["content"]
    sandbox.cleanup()


def test_identical_read_blocked_after_first(tmp_path) -> None:
    (tmp_path / "a.txt").write_text("x", encoding="utf-8")
    script = _script_client(
        tool_call_response("read_file", {"path": "a.txt"}),
        tool_call_response("read_file", {"path": "a.txt"}),
        final_response("stopped repeating"),
    )
    loop, sandbox = _loop(script, tmp_path)
    loop.run(_task())
    third = script.received_messages[2]
    tool_msgs = [m for m in third if m.get("role") == "tool"]
    assert tool_msgs and "already called read_file" in tool_msgs[-1]["content"]
    sandbox.cleanup()


def test_write_resets_repeat_blockers(tmp_path) -> None:
    # Read, write, then read the same path again: the write must clear
    # the read counter so the verify re-read executes.
    script = _script_client(
        tool_call_response("read_file", {"path": "a.txt"}),
        tool_call_response("write_file", {"path": "a.txt", "content": "new"}),
        tool_call_response("read_file", {"path": "a.txt"}),
        final_response("verified"),
    )
    (tmp_path / "a.txt").write_text("old", encoding="utf-8")
    loop, sandbox = _loop(script, tmp_path)
    result = loop.run(_task())
    assert result.stopped_reason == "final"
    sandbox.cleanup()


def test_abort_mid_run(tmp_path) -> None:
    class _AbortingClient(_ScriptedBase):
        def chat(self, messages, tools=None, on_delta=None, **kwargs):
            raise AbortError("interrupted by operator")

    loop, sandbox = _loop(_AbortingClient([]), tmp_path)
    result = loop.run(_task())
    assert result.stopped_reason == "aborted"
    assert result.passed is False
    sandbox.cleanup()


def test_abort_between_tools_fills_remaining_results(tmp_path) -> None:
    # Two tool calls in one turn; the first raises AbortError. The loop
    # must fill the second tool result so the history stays valid.
    class _AbortOnFirst(_ScriptedBase):
        def __init__(self) -> None:
            super().__init__([])
            self.asked = False

        def chat(self, messages, tools=None, on_delta=None, **kwargs):
            from core.types import LLMResponse, ToolCall

            if not self.asked:
                self.asked = True
                return LLMResponse(
                    tool_calls=[
                        ToolCall(id="c1", name="read_file", arguments={"path": "a.txt"}),
                        ToolCall(id="c2", name="read_file", arguments={"path": "b.txt"}),
                    ]
                )
            return LLMResponse(content="after abort")

    class _AbortingRead(ReadFileTool):
        def execute(self, sandbox, path, offset=0, limit=2000):  # type: ignore[override]
            raise AbortError("interrupted by operator")

    sandbox = LocalSandbox(workspace_root=str(tmp_path))
    sandbox.setup({})
    tools = [_AbortingRead(), WriteFileTool(), ListDirTool()]
    loop = AgentLoop(
        llm=_AbortOnFirst(),
        sandbox=sandbox,
        tools=tools,
        evaluator=NullEvaluator(),
        logger=JsonlLogger(str(tmp_path / "run.jsonl")),
    )
    result = loop.run(_task())
    assert result.stopped_reason == "aborted"
    sandbox.cleanup()


def test_empty_final_nudges_then_gives_up(tmp_path) -> None:
    # A client that always returns an empty final: the loop nudges twice,
    # then stops with an error instead of burning max_steps.
    loop, sandbox = _loop(_script_client(final_response(""), final_response(""),
                                         final_response(""), final_response("")), tmp_path)
    result = loop.run(_task(), )
    assert result.stopped_reason == "error"
    assert "empty final" in (result.final_message or "")
    sandbox.cleanup()


def test_max_steps_stops_loop(tmp_path) -> None:
    script = _NeverFinal()
    loop, sandbox = _loop(script, tmp_path, max_steps=3)
    result = loop.run(_task())
    assert result.stopped_reason == "max_steps"
    assert result.steps_used == 3
    sandbox.cleanup()


def test_usage_metrics_absorbed(tmp_path) -> None:
    from core.types import LLMResponse

    class _UsageClient(_ScriptedBase):
        def chat(self, messages, tools=None, on_delta=None, **kwargs):
            return LLMResponse(content="ok", usage={"prompt_tokens": 10, "completion_tokens": 5})

    loop, sandbox = _loop(_UsageClient(), tmp_path)
    result = loop.run(_task())
    assert result.metrics.get("tokens_in") == 10
    assert result.metrics.get("tokens_out") == 5
    sandbox.cleanup()


def test_usage_unknown_when_absent(tmp_path) -> None:
    loop, sandbox = _loop(_script_client(final_response("ok")), tmp_path)
    result = loop.run(_task())
    # A scripted response carries no usage: the loop records the gap.
    assert result.metrics.get("usage_unknown") == 1
    sandbox.cleanup()


def test_event_bus_receives_lifecycle(tmp_path) -> None:
    seen: list[str] = []
    bus = EventBus()
    bus.subscribe(lambda name, payload: seen.append(name))
    loop, sandbox = _loop(_script_client(final_response("done")), tmp_path, events=bus)
    loop.run(_task())
    assert "run_start" in seen
    assert "run_end" in seen
    sandbox.cleanup()


def test_seeded_task_truncated_when_oversized(tmp_path) -> None:
    loop, sandbox = _loop(_script_client(final_response("ok")), tmp_path)
    # Far beyond the default 240k char budget: seeding must cap it.
    big = "x" * 500_000
    result = loop.run(_task(problem_statement=big))
    assert result.stopped_reason == "final"
    seeded = loop.context.messages[1]["content"]
    assert "truncated" in seeded


class _ScriptedBase:
    """Minimal LLMClient stand-in recording received messages."""

    def __init__(self, script=()) -> None:
        from core.scripted import ScriptedLLMClient

        self._inner = ScriptedLLMClient(list(script))

    @property
    def received_messages(self):
        return self._inner.received_messages

    def chat(self, messages, tools=None, on_delta=None, **kwargs):
        return self._inner.chat(messages, tools=tools, on_delta=on_delta)


def _script_client(*responses):
    return _ScriptedBase(list(responses))


class _NeverFinal(_ScriptedBase):
    """Always asks for a tool; never returns a final answer."""

    def chat(self, messages, tools=None, on_delta=None, **kwargs):
        from core.types import LLMResponse, ToolCall

        self._inner.received_messages.append(list(messages))
        return LLMResponse(
            tool_calls=[ToolCall(id=f"c{len(self._inner.received_messages)}", name="list_dir", arguments={"path": "."})]
        )
