"""End-to-end smoke test for the MANTRA harness core.

Runs the full agent loop offline: a scripted LLM fixes a seeded bug through
the real tools, the command evaluator grades the result, and edge behaviors
(context truncation, unknown components) are checked alongside.
Run from the project root:  python -m unittest discover -s tests -v
"""

from __future__ import annotations

import os
import json
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from mantra.config import merge_defaults
from mantra.core.agent_loop import AgentLoop
from mantra.core.context import ContextManager
from mantra.core.events import EventBus
from mantra.core.exceptions import ConfigError, LLMError
from mantra.implementations.evaluators.command_evaluator import CommandEvaluator
from mantra.implementations.evaluators.null_evaluator import NullEvaluator
from mantra.implementations.llm.mock_client import (
    ScriptedLLMClient,
    final_response,
    tool_call_response,
)
from mantra.implementations.loggers.jsonl_logger import JsonlLogger
from mantra.implementations.sandbox.local_sandbox import LocalSandbox
from mantra.registry import build_tools

GREET_BUGGY = 'def greet():\n    return "helo"\n'
GREET_TESTS = (
    "import unittest\n"
    "from greet import greet\n\n"
    "class TestGreet(unittest.TestCase):\n"
    "    def test_greeting(self):\n"
    "        self.assertEqual(greet(), \"hello\")\n"
)
TEST_CMD = f'"{sys.executable}" -m unittest test_greet -v'


def make_workspace() -> str:
    root = tempfile.mkdtemp(prefix="mantra-smoke-")
    with open(os.path.join(root, "greet.py"), "w", encoding="utf-8") as handle:
        handle.write(GREET_BUGGY)
    with open(os.path.join(root, "test_greet.py"), "w", encoding="utf-8") as handle:
        handle.write(GREET_TESTS)
    return root


def build_loop(llm: ScriptedLLMClient, workspace: str, log_path: str) -> AgentLoop:
    return AgentLoop(
        llm=llm,
        sandbox=LocalSandbox(workspace),
        tools=build_tools(
            ["read_file", "edit_file", "list_dir", "run_command"]
        ),
        evaluator=CommandEvaluator(test_cmd=TEST_CMD, timeout=60),
        logger=JsonlLogger(log_path),
        events=EventBus(),
        max_steps=10,
    )


TASK = {
    "task_id": "smoke-fix-greeting",
    "problem_statement": "greet.py returns 'helo'; make it return 'hello'.",
    "test_cmd": TEST_CMD,
}


class _MidToolCallFlaky(ScriptedLLMClient):
    """Raises like a stream that ended mid-tool-call, then plays its script."""

    def __init__(self, script: list, fail_times: int = 1) -> None:
        super().__init__(script)
        self.fail_times = fail_times

    def chat(self, messages, tools=None, on_delta=None):
        if self.fail_times > 0:
            self.fail_times -= 1
            raise LLMError(
                "the response ended mid-tool-call (write_file): "
                "Expecting value: line 1 column 2 (char 1)"
            )
        return super().chat(messages, tools=tools, on_delta=on_delta)


class TruncatedToolCallTest(unittest.TestCase):
    """A model cut off mid-tool-call (output budget) must recover or fail cleanly."""

    def test_truncated_tool_call_retries_bounded_then_fails(self):
        """Persistent truncation fails after bounded nudges with advice."""
        workspace = make_workspace()
        llm = _MidToolCallFlaky([final_response("never reached")], fail_times=99)
        loop = build_loop(llm, workspace, os.path.join(workspace, "run.jsonl"))
        loop.max_steps = 30  # override the default so the retry budget governs
        result = loop.run(TASK)
        self.assertEqual(result.stopped_reason, "error")
        # 1 initial attempt + 2 nudged retries, then give up.
        self.assertEqual(result.steps_used, 3)
        self.assertIn("cut off mid-tool-call (write_file)", result.final_message)
        # The advice names the real cause, not a raw JSON fragment.
        self.assertIn("max_tokens", result.final_message)
        self.assertNotIn("Expecting value", result.final_message)

    def test_transient_truncated_tool_call_recovers(self):
        """A single cut-off must not fail the run: the nudge recovers it."""
        workspace = make_workspace()
        llm = _MidToolCallFlaky(
            [
                tool_call_response("read_file", {"path": "greet.py"}),
                final_response("done: read the file"),
            ],
            fail_times=1,
        )
        loop = build_loop(llm, workspace, os.path.join(workspace, "run.jsonl"))
        loop.max_steps = 30  # override the default so the retry budget governs
        result = loop.run(TASK)
        self.assertEqual(result.stopped_reason, "final")
        self.assertEqual(result.steps_used, 3)  # 1 cut + 1 tool step + 1 final
        self.assertIn("read the file", result.final_message)


class _ExplodingLogger:
    """A logger whose log() always raises (disk full, bad impl)."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def log(self, event: str, payload=None) -> None:
        self.calls.append(event)
        raise OSError("disk full")


class LoggerFailureTest(unittest.TestCase):
    """A broken logger must never turn a completed run into an exception."""

    def test_failing_logger_does_not_break_run(self):
        workspace = make_workspace()
        llm = ScriptedLLMClient([final_response("done.")])
        logger = _ExplodingLogger()
        loop = AgentLoop(
            llm=llm,
            sandbox=LocalSandbox(workspace),
            tools=build_tools(["read_file", "edit_file", "list_dir", "run_command"]),
            evaluator=CommandEvaluator(test_cmd=TEST_CMD, timeout=60),
            logger=logger,  # type: ignore[arg-type]
            events=EventBus(),
            max_steps=10,
        )
        result = loop.run(TASK)
        self.assertEqual(result.stopped_reason, "final")
        self.assertEqual(result.final_message, "done.")
        # The run still reached the logger with the terminal event.
        self.assertEqual(logger.calls[-1], "run_result")


class SmokeTest(unittest.TestCase):
    def test_full_run_passes(self):
        workspace = make_workspace()
        llm = ScriptedLLMClient(
            [
                tool_call_response("list_dir", {"path": "."}),
                tool_call_response("read_file", {"path": "greet.py"}),
                tool_call_response(
                    "edit_file",
                    {"path": "greet.py", "old_string": '"helo"', "new_string": '"hello"'},
                ),
                tool_call_response("run_command", {"command": TEST_CMD}),
                final_response("Fixed the typo in greet()."),
            ]
        )
        log_path = os.path.join(workspace, "run.jsonl")
        result = build_loop(llm, workspace, log_path).run(TASK)

        self.assertTrue(result.passed, msg=result.evaluation_detail)
        self.assertEqual(result.stopped_reason, "final")
        self.assertEqual(result.steps_used, 5)

        # The edit actually landed on disk.
        with open(os.path.join(workspace, "greet.py"), encoding="utf-8") as handle:
            self.assertIn('"hello"', handle.read())

        # Structured JSONL evidence was written.
        with open(log_path, encoding="utf-8") as handle:
            events = [line.split('"event": "', 1)[1].split('"', 1)[0] for line in handle]
        self.assertIn("tool_call", events)
        self.assertIn("run_result", events)

    def test_failed_fix_reports_failure(self):
        workspace = make_workspace()
        llm = ScriptedLLMClient(
            [
                tool_call_response(
                    "edit_file",
                    {"path": "greet.py", "old_string": '"helo"', "new_string": '"hi"'},
                ),
                final_response("Done (but wrong)."),
            ]
        )
        result = build_loop(llm, workspace, os.path.join(workspace, "run.jsonl")).run(TASK)
        self.assertFalse(result.passed)
        self.assertEqual(result.stopped_reason, "final")

    def test_unknown_tool_becomes_observation_not_crash(self):
        workspace = make_workspace()
        llm = ScriptedLLMClient(
            [
                tool_call_response("nonexistent_tool", {}),
                final_response("Giving up."),
            ]
        )
        result = build_loop(llm, workspace, os.path.join(workspace, "run.jsonl")).run(TASK)
        self.assertFalse(result.passed)  # bug never fixed -> evaluation fails

    def test_max_steps_stops_the_loop(self):
        workspace = make_workspace()
        llm = ScriptedLLMClient(
            [tool_call_response("list_dir", {"path": "."})] * 20
        )
        loop = build_loop(llm, workspace, os.path.join(workspace, "run.jsonl"))
        loop.max_steps = 3
        result = loop.run(TASK)
        self.assertEqual(result.stopped_reason, "max_steps")
        self.assertEqual(len(llm.script), 17)  # exactly max_steps calls consumed

    def test_empty_final_retries_bounded_then_fails(self):
        """A persistently-empty model fails fast after bounded retries.

        An empty final is often transient (a reasoning model spending its
        output budget, a dropped completion), so the loop nudges the model
        a couple of times. But retrying with the identical context would
        reproduce the same empty reply, so the run must stop after the
        bounded retry budget instead of exhausting max_steps.
        """
        workspace = make_workspace()
        llm = ScriptedLLMClient(
            [final_response(""), final_response(""), final_response(""), final_response("never reached")]
        )
        loop = build_loop(llm, workspace, os.path.join(workspace, "run.jsonl"))
        loop.max_steps = 30  # override the default so the retry budget governs
        result = loop.run(TASK)
        self.assertEqual(result.stopped_reason, "error")
        # 1 initial attempt + 2 nudged retries, then give up.
        self.assertEqual(result.steps_used, 3)
        self.assertIn("empty final", result.final_message)

    def test_transient_empty_final_recovers(self):
        """A single empty final must not fail the run.

        Reasoning models can exhaust their output budget and emit an empty
        final once; the nudged retry should recover and finish normally.
        """
        workspace = make_workspace()
        llm = ScriptedLLMClient(
            [final_response(""), final_response("done: greet() fixed and tests pass")]
        )
        loop = build_loop(llm, workspace, os.path.join(workspace, "run.jsonl"))
        loop.max_steps = 30  # override the default so the retry budget governs
        result = loop.run(TASK)
        self.assertEqual(result.stopped_reason, "final")
        self.assertEqual(result.steps_used, 2)
        self.assertIn("done", result.final_message)


class _MidToolCallFlaky(ScriptedLLMClient):
    """Raises like a stream that ended mid-tool-call, then plays its script."""

    def __init__(self, script: list, fail_times: int = 1) -> None:
        super().__init__(script)
        self.fail_times = fail_times

    def chat(self, messages, tools=None, on_delta=None):
        if self.fail_times > 0:
            self.fail_times -= 1
            raise LLMError(
                "the response ended mid-tool-call (write_file): "
                "Expecting value: line 1 column 2 (char 1)"
            )
        return super().chat(messages, tools=tools, on_delta=on_delta)


class TruncatedToolCallTest(unittest.TestCase):
    """A model cut off mid-tool-call (output budget) must recover or fail cleanly."""

    def test_truncated_tool_call_retries_bounded_then_fails(self):
        """Persistent truncation fails after bounded nudges with advice."""
        workspace = make_workspace()
        llm = _MidToolCallFlaky([final_response("never reached")], fail_times=99)
        loop = build_loop(llm, workspace, os.path.join(workspace, "run.jsonl"))
        loop.max_steps = 30  # override the default so the retry budget governs
        result = loop.run(TASK)
        self.assertEqual(result.stopped_reason, "error")
        # 1 initial attempt + 2 nudged retries, then give up.
        self.assertEqual(result.steps_used, 3)
        self.assertIn("cut off mid-tool-call (write_file)", result.final_message)
        # The advice names the real cause, not a raw JSON fragment.
        self.assertIn("max_tokens", result.final_message)
        self.assertNotIn("Expecting value", result.final_message)

    def test_transient_truncated_tool_call_recovers(self):
        """A single cut-off must not fail the run: the nudge recovers it."""
        workspace = make_workspace()
        llm = _MidToolCallFlaky(
            [
                tool_call_response("read_file", {"path": "greet.py"}),
                final_response("done: read the file"),
            ],
            fail_times=1,
        )
        loop = build_loop(llm, workspace, os.path.join(workspace, "run.jsonl"))
        loop.max_steps = 30  # override the default so the retry budget governs
        result = loop.run(TASK)
        self.assertEqual(result.stopped_reason, "final")
        self.assertEqual(result.steps_used, 3)  # 1 cut + 1 tool step + 1 final
        self.assertIn("read the file", result.final_message)


class SmokeAliasTest(unittest.TestCase):
    """Alias resolution of tool names (webfetch -> web_fetch)."""

    def test_aliased_tool_name_is_resolved(self):
        """Registry aliases (webfetch -> web_fetch) must work at dispatch time."""
        from mantra.interfaces.tool import Tool

        class FakeWebTool(Tool):
            name = "web_fetch"
            description = "fake"

            def execute(self, sandbox, **kwargs):
                return "OK: fetched"

        workspace = make_workspace()
        loop = AgentLoop(
            llm=ScriptedLLMClient(
                [
                    tool_call_response("webfetch", {"url": "https://example.com"}),
                    final_response("fetched it"),
                ]
            ),
            sandbox=LocalSandbox(workspace),
            tools=[FakeWebTool()],
            evaluator=NullEvaluator(),
            logger=JsonlLogger(os.path.join(workspace, "run.jsonl")),
            max_steps=5,
        )
        result = loop.run(TASK)
        self.assertEqual(result.stopped_reason, "final")
        # The aliased call must have been dispatched, not reported unknown.
        self.assertEqual(result.metrics.get("tool_errors", 0), 0)


class ContextTest(unittest.TestCase):
    def test_truncation_keeps_pinned_messages(self):
        ctx = ContextManager(max_messages=4)
        ctx.seed("system prompt", "task")
        for i in range(6):
            ctx.append({"role": "tool", "content": f"obs-{i}"})
        self.assertEqual(len(ctx.messages), 4)
        self.assertEqual(ctx.messages[0]["content"], "system prompt")
        self.assertEqual(ctx.messages[1]["content"], "task")
        self.assertEqual(ctx.messages[-1]["content"], "obs-5")


class RegistryConfigTest(unittest.TestCase):
    def test_unknown_tool_name_fails_loudly(self):
        with self.assertRaises(ConfigError):
            build_tools(["read_file", "no_such_tool"])

    def test_config_merge_fills_defaults(self):
        merged = merge_defaults({"evaluator": {"type": "command", "test_cmd": "echo ok"}})
        self.assertEqual(merged["evaluator"]["test_cmd"], "echo ok")
        self.assertIn("tools", merged)

    def test_switching_component_type_drops_old_type_defaults(self):
        # A "none" evaluator must not inherit the default command
        # evaluator's keys: the registry rejects unknown constructor keys,
        # so a leaked test_cmd would break the switch.
        merged = merge_defaults({"evaluator": {"type": "none"}})
        self.assertEqual(merged["evaluator"], {"type": "none"})
        self.assertNotIn("test_cmd", merged["evaluator"])

        merged = merge_defaults({"llm": {"provider": "scripted"}})
        self.assertEqual(merged["llm"], {"provider": "scripted"})
        self.assertNotIn("model", merged["llm"])

        # Same-type partial configs still inherit defaults.
        merged = merge_defaults({"evaluator": {"type": "command", "timeout": 30}})
        self.assertEqual(merged["evaluator"]["test_cmd"], "python -m pytest tests/ -q")
        self.assertEqual(merged["evaluator"]["timeout"], 30)

    def test_evaluator_without_init_builds_cleanly(self):
        # NullEvaluator inherits object.__init__ (*args/**kwargs); the
        # missing-required check must not demand them.
        from mantra.registry import build_evaluator

        evaluator = build_evaluator({"type": "none"})
        self.assertEqual(evaluator.evaluate(None, {}).passed, True)


class EditLedgerTest(unittest.TestCase):
    """Read-before-edit contract adopted from the workflow-layer harness."""

    def setUp(self):
        self.workspace = make_workspace()

    def _tools(self):
        return build_tools(["read_file", "write_file", "edit_file"])

    def _edit(self, tools, old, new, path="greet.py"):
        edit = next(t for t in tools if t.name == "edit_file")
        return edit.execute(LocalSandbox(self.workspace), path=path, old_string=old, new_string=new)

    def test_unread_edit_is_rejected(self):
        result = self._edit(self._tools(), '"helo"', '"hello"')
        self.assertTrue(result.startswith("ERROR"), msg=result)
        # File untouched.
        with open(os.path.join(self.workspace, "greet.py"), encoding="utf-8") as handle:
            self.assertIn('"helo"', handle.read())

    def test_read_then_edit_passes(self):
        tools = self._tools()
        reader = next(t for t in tools if t.name == "read_file")
        reader.execute(LocalSandbox(self.workspace), path="greet.py")
        result = self._edit(tools, '"helo"', '"hello"')
        self.assertTrue(result.startswith("OK"), msg=result)

    def test_stale_edit_after_external_change_is_rejected(self):
        tools = self._tools()
        sandbox = LocalSandbox(self.workspace)
        reader = next(t for t in tools if t.name == "read_file")
        reader.execute(sandbox, path="greet.py")
        # External mutation behind the agent's back.
        sandbox.write_file("greet.py", GREET_BUGGY + "# touched\n")
        result = self._edit(tools, '"helo"', '"hello"')
        self.assertIn("changed on disk since your last read", result)

    def test_write_then_edit_passes_without_read(self):
        tools = self._tools()
        writer = next(t for t in tools if t.name == "write_file")
        sandbox = LocalSandbox(self.workspace)
        writer.execute(sandbox, path="new.py", content="x = 1\n")
        edit = next(t for t in tools if t.name == "edit_file")
        result = edit.execute(sandbox, path="new.py", old_string="1", new_string="2")
        self.assertTrue(result.startswith("OK"), msg=result)


class KnowledgeTest(unittest.TestCase):
    def test_assemble_includes_known_failures_and_memory_tail(self):
        from mantra.core.knowledge import assemble_system_prompt

        kf = os.path.join(tempfile.mkdtemp(prefix="mantra-kf-"), "kf.md")
        mem = os.path.join(kf, os.pardir, "mem.md")
        with open(kf, "w", encoding="utf-8") as handle:
            handle.write("## KF-9 | never do the bad thing\n")
        with open(mem, "w", encoding="utf-8") as handle:
            handle.write("- 2026-08-26 | earlier note\n")
        prompt = assemble_system_prompt(
            "base prompt", known_failures_path=kf, memory_path=mem
        )
        self.assertIn("base prompt", prompt)
        self.assertIn("KF-9", prompt)
        self.assertIn("earlier note", prompt)

    def test_missing_files_yield_base_prompt_only(self):
        from mantra.core.knowledge import assemble_system_prompt

        prompt = assemble_system_prompt(
            "solo", known_failures_path="Z:/none.md", memory_path="Z:/none2.md"
        )
        self.assertEqual(prompt, "solo")

    def test_append_memory_prunes_oldest_beyond_cap(self):
        from mantra.core.knowledge import append_memory

        mem = os.path.join(make_workspace(), ".mantra", "memory.md")
        for i in range(50):
            append_memory(mem, f"- entry {i:03d} " + "x" * 200, cap=2000)
        size = os.path.getsize(mem)
        with open(mem, encoding="utf-8") as handle:
            content = handle.read()
        self.assertLessEqual(size, 2100)  # cap plus a small header slack
        self.assertNotIn("entry 000", content)  # oldest pruned
        self.assertIn("entry 049", content)  # newest kept

    def test_workspace_instruction_file_discovered_and_injected(self):
        from mantra.core.knowledge import assemble_system_prompt, find_instructions_file

        ws = make_workspace()
        with open(os.path.join(ws, "AGENTS.md"), "w", encoding="utf-8") as handle:
            handle.write("Always use tabs in this repo.\n")
        found = find_instructions_file(ws)
        self.assertIsNotNone(found)
        prompt = assemble_system_prompt("base", instructions_path=found)
        self.assertIn("Always use tabs", prompt)

    def test_instruction_file_preference_order(self):
        from mantra.core.knowledge import find_instructions_file

        ws = make_workspace()
        self.assertIsNone(find_instructions_file(ws))
        with open(os.path.join(ws, "CLAUDE.md"), "w", encoding="utf-8") as handle:
            handle.write("claude rules\n")
        self.assertTrue(find_instructions_file(ws).endswith("CLAUDE.md"))
        with open(os.path.join(ws, "AGENTS.md"), "w", encoding="utf-8") as handle:
            handle.write("agents rules\n")
        self.assertTrue(find_instructions_file(ws).endswith("AGENTS.md"))


class SseStreamParseTest(unittest.TestCase):
    """Streaming parser must rebuild content and tool calls from chunks."""

    def _lines(self, *chunks):
        return [f"data: {json.dumps({'choices': [{'delta': c}]})}" for c in chunks] + [
            "data: [DONE]"
        ]

    def test_content_deltas_accumulate(self):
        from mantra.implementations.llm.openai_client import parse_sse_stream

        seen = []
        result = parse_sse_stream(
            self._lines({"content": "Hel"}, {"content": "lo"}, {"content": "!"}),
            on_delta=seen.append,
        )
        self.assertEqual(result.content, "Hello!")
        self.assertEqual(seen, ["Hel", "lo", "!"])
        self.assertTrue(result.is_final)

    def test_tool_call_fragments_reassemble(self):
        from mantra.implementations.llm.openai_client import parse_sse_stream

        lines = [
            "data: " + json.dumps(
                {"choices": [{"delta": {"tool_calls": [
                    {"index": 0, "id": "c1", "function": {"name": "edit_", "arguments": ""}},
                ]}}]}
            ),
            "data: " + json.dumps(
                {"choices": [{"delta": {"tool_calls": [
                    {"index": 0, "function": {"name": "file", "arguments": '{"path": '}},
                ]}}]}
            ),
            "data: " + json.dumps(
                {"choices": [{"delta": {"tool_calls": [
                    {"index": 0, "function": {"arguments": '"a.py"}'}},
                ]}}]}
            ),
            "data: [DONE]",
        ]
        result = parse_sse_stream(lines)
        self.assertEqual(len(result.tool_calls), 1)
        call = result.tool_calls[0]
        self.assertEqual(call.name, "edit_file")
        self.assertEqual(call.arguments, {"path": "a.py"})
        self.assertIsNone(result.content)

    def test_done_sentinel_and_noise_tolerated(self):
        from mantra.implementations.llm.openai_client import parse_sse_stream

        lines = [": keep-alive comment", "", "data: not-json{{", "data: [DONE]", "data: {}"]
        result = parse_sse_stream(lines)
        self.assertIsNone(result.content)


class SandboxScreenTest(unittest.TestCase):
    """The traversal guard must block real escapes, not cmd.exe switches."""

    @staticmethod
    def _traversal(cmd: str) -> bool:
        from mantra.implementations.sandbox.local_sandbox import _contains_traversal

        return _contains_traversal(cmd)

    def test_windows_cmd_switches_not_flagged_on_windows(self):
        # A real pipeline the agent runs on Windows. findstr /C:"..." is a
        # cmd.exe switch, not a POSIX absolute path, and must not be blocked.
        cmd = (
            'git diff --no-color -- flappy.py 2>nul | '
            'findstr /C:"on_flap" /C:"paused" /C:"^[-+]" /C:"@"'
        )
        # On POSIX sh a bare /word argument reads as an absolute path, so
        # each platform's heuristic follows its own shell.
        self.assertEqual(self._traversal(cmd), os.name != "nt")

    def test_real_escapes_still_blocked_everywhere(self):
        # Drive-letter, parent-directory, and env-home escapes must keep
        # failing on every platform regardless of the switch relaxation.
        self.assertTrue(self._traversal("cat C:\\Windows\\system32\\drivers\\etc\\hosts"))
        self.assertTrue(self._traversal("type ..\\secrets.txt"))
        self.assertTrue(self._traversal("cd .. && ls"))
        self.assertTrue(self._traversal("cat ../outside/file.txt"))
        self.assertTrue(self._traversal("echo %USERPROFILE%\\secret.txt"))


class EmptyStreamRetryTest(unittest.TestCase):
    """A stream closing before any SSE data must be retried, not fatal."""

    def _make(self, max_retries: int = 3):
        from mantra.implementations.llm.openai_client import OpenAICompatClient

        return OpenAICompatClient(
            model="test-model",
            base_url="http://llm.invalid/v1",
            api_key_env="MANTRA_TEST_EMPTY_KEY",
            max_retries=max_retries,
        )

    def _run(self, client, side_effect):
        from unittest import mock

        # The client reads its key from the environment at call time.
        with mock.patch.dict("os.environ", {"MANTRA_TEST_EMPTY_KEY": "test-key"}):
            with mock.patch.object(client, "_request_stream", side_effect=side_effect):
                with mock.patch("mantra.implementations.llm.openai_client.time.sleep"):
                    return client.chat(
                        [{"role": "user", "content": "hi"}],
                        on_delta=lambda piece: None,
                    )

    def test_empty_stream_retried_then_recovers(self):
        from mantra.core.exceptions import LLMError
        from mantra.interfaces.llm_client import LLMResponse

        client = self._make()
        calls = {"n": 0}

        def flaky(body, on_delta):
            calls["n"] += 1
            if calls["n"] < 3:
                raise LLMError("stream ended without DONE and no data")
            return LLMResponse(content="recovered")

        result = self._run(client, flaky)
        self.assertEqual(result.content, "recovered")
        self.assertEqual(calls["n"], 3)

    def test_persistent_empty_stream_fails_after_max_retries(self):
        from mantra.core.exceptions import LLMError

        client = self._make(max_retries=2)
        calls = {"n": 0}

        def always_empty(body, on_delta):
            calls["n"] += 1
            raise LLMError("stream ended without DONE and no data")

        with self.assertRaises(LLMError) as ctx:
            self._run(client, always_empty)
        self.assertEqual(calls["n"], 2)
        self.assertIn("after 2 attempts", str(ctx.exception))

    def test_non_transient_llm_error_not_retried(self):
        # A mid-tool-call cut is a different, model-recoverable failure the
        # agent loop nudges; it must not be silently swallowed by retries.
        from mantra.core.exceptions import LLMError

        client = self._make(max_retries=3)
        calls = {"n": 0}

        def hard(body, on_delta):
            calls["n"] += 1
            raise LLMError("the response ended mid-tool-call (edit_file): boom")

        with self.assertRaises(LLMError):
            self._run(client, hard)
        self.assertEqual(calls["n"], 1)


if __name__ == "__main__":
    unittest.main()
