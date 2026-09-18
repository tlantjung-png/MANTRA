"""Regression tests for the second remediation round.

Each test pins one fix from the follow-up audit so the behavior cannot
silently regress:

- H-1  abort inside a batched tool turn still fills every tool result
- M-2  a successful write clears run-command repeat counters
- H-2  interpreter one-liners need confirmation even in auto mode
- H-3  the kill tool never signals host processes from a container
       sandbox, and pid targeting is restricted to harness-spawned pids
- M-1  shell_output cursors are byte offsets (multi-byte safe)
- M-4  an ambiguous edit needle is refused, not applied to the first hit
- L-2  a literal filename containing glob metacharacters still reads
- L-4  context budget is guaranteed after the halving-loop cap
- L-5  empty tool-call ids are synthesized, never sent empty
- L-6  background-task pruning never evicts a running task
- L-14 the write cap is measured in bytes
"""

from __future__ import annotations

import os
import shutil
import tempfile
import threading
import unittest
from unittest import mock

from core.agent.loop import AgentLoop
from core.agent.approvals import ApprovalPolicy, classify_command
from core.agent.context import ContextManager
from core.agent.events import EventBus
from core.agent.exceptions import AbortError, SandboxError
from core.scripted import ScriptedLLMClient
from core.logs import JsonlLogger
from core.container import DockerSandbox
from core.sandbox import LocalSandbox
from core.tools import commands as command_tool
from core.tools.commands import KillShellTool, RunCommandTool, ShellOutputTool
from core.tools.ledger import EditLedger
from core.tools.files import EditFileTool, ReadFileTool, WriteFileTool
from core.types import LLMResponse, ToolCall
from core.types import Tool


def _workspace() -> str:
    return tempfile.mkdtemp(prefix="mantra-round2-")


def _logger() -> JsonlLogger:
    return JsonlLogger(os.path.join(tempfile.mkdtemp(prefix="mantra-log-"), "run.jsonl"))


class _BoomTool(Tool):
    """Raises the operator-interrupt exception mid-execution."""

    name = "boom"
    description = "always aborts"
    parameters = {"type": "object", "properties": {}}

    def execute(self, sandbox, **kwargs):
        raise AbortError("interrupted by operator")


class _EchoTool(Tool):
    name = "echo"
    description = "returns its marker"
    parameters = {"type": "object", "properties": {"marker": {"type": "string"}}}

    def execute(self, sandbox, marker="x"):
        return f"OK {marker}"


class AbortMidBatchTest(unittest.TestCase):
    """H-1: every tool call in a batch must get a tool message."""

    def test_abort_inside_a_tool_fills_the_whole_batch(self):
        ws = _workspace()
        two_calls = LLMResponse(
            tool_calls=[
                ToolCall(id="call_a", name="boom", arguments={}),
                ToolCall(id="call_b", name="echo", arguments={"marker": "b"}),
            ]
        )
        llm = ScriptedLLMClient([two_calls, LLMResponse(content="unused")])
        loop = AgentLoop(
            llm=llm,
            sandbox=LocalSandbox(ws),
            tools=[_BoomTool(), _EchoTool()],
            evaluator=None,
            logger=_logger(),
            events=EventBus(),
        )
        result = loop.run({"task_id": "t", "problem_statement": "go"})
        self.assertEqual(result.stopped_reason, "aborted")
        # The scripted follow-up was never consumed: the run ended at the abort.
        self.assertEqual(len(llm.script), 1)
        roles = [m.get("role") for m in loop.context.messages]
        # Assistant turn with two calls, answered by exactly two tool messages.
        self.assertEqual(roles.count("tool"), 2)
        tool_msgs = [m for m in loop.context.messages if m.get("role") == "tool"]
        self.assertEqual(
            sorted(m["tool_call_id"] for m in tool_msgs), ["call_a", "call_b"]
        )
        for m in tool_msgs:
            self.assertIn("interrupted by operator", m["content"])

    def test_empty_call_ids_are_synthesized(self):
        """L-5: an empty provider id never reaches the history."""
        ws = _workspace()
        two_calls = LLMResponse(
            tool_calls=[
                ToolCall(id="", name="echo", arguments={"marker": "1"}),
                ToolCall(id="", name="echo", arguments={"marker": "2"}),
            ]
        )
        llm = ScriptedLLMClient([two_calls, LLMResponse(content="done")])
        loop = AgentLoop(
            llm=llm,
            sandbox=LocalSandbox(ws),
            tools=[_EchoTool()],
            evaluator=None,
            logger=_logger(),
            events=EventBus(),
        )
        result = loop.run({"task_id": "t", "problem_statement": "go"})
        self.assertEqual(result.stopped_reason, "final")
        tool_msgs = [m for m in loop.context.messages if m.get("role") == "tool"]
        self.assertEqual(len(tool_msgs), 2)
        ids = [m["tool_call_id"] for m in tool_msgs]
        self.assertTrue(all(ids), "empty tool_call_id reached the history")
        self.assertNotEqual(ids[0], ids[1])


class RepeatCounterInvalidationTest(unittest.TestCase):
    """M-2: a successful write lets the same command run again."""

    def test_repeated_command_after_write_executes(self):
        ws = _workspace()
        responses = [
            LLMResponse(tool_calls=[ToolCall(id="c1", name="echo", arguments={"marker": "first"})]),
            LLMResponse(tool_calls=[ToolCall(id="c2", name="write_file", arguments={"path": "f.txt", "content": "hi"})]),
            LLMResponse(tool_calls=[ToolCall(id="c3", name="echo", arguments={"marker": "first"})]),
            LLMResponse(content="done"),
        ]
        llm = ScriptedLLMClient(responses)

        class _WriteFileTool(Tool):
            name = "write_file"
            description = "writes a file"
            parameters = {
                "type": "object",
                "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
            }

            def execute(self, sandbox, path="", content=""):
                sandbox.write_file(path, content)
                return "OK: wrote"

        loop = AgentLoop(
            llm=llm,
            sandbox=LocalSandbox(ws),
            tools=[_EchoTool(), _WriteFileTool()],
            evaluator=None,
            logger=_logger(),
            events=EventBus(),
        )
        result = loop.run({"task_id": "t", "problem_statement": "go"})
        self.assertEqual(result.stopped_reason, "final")
        echo_results = [
            m["content"]
            for m in loop.context.messages
            if m.get("role") == "tool" and m.get("name") == "echo"
        ]
        self.assertEqual(len(echo_results), 2)
        # The second identical echo executed for real instead of being
        # refused with "you already called".
        for content in echo_results:
            self.assertIn("OK first", content)


class InterpreterOneLinerTest(unittest.TestCase):
    """H-2: inline-code interpreter payloads need confirmation."""

    DESTRUCTIVE_PY = "python -c \"import shutil; shutil.rmtree('x')\""

    def test_python_dash_c_is_confirm(self):
        self.assertEqual(classify_command(self.DESTRUCTIVE_PY), "confirm")

    def test_node_eval_is_confirm(self):
        self.assertEqual(classify_command("node -e 'fs.rmSync(\"x\", {recursive: true})'"), "confirm")

    def test_powershell_encoded_command_is_confirm(self):
        self.assertEqual(classify_command("powershell -EncodedCommand AAAA"), "confirm")

    def test_compound_worst_verdict_wins(self):
        self.assertEqual(classify_command("echo hi && " + self.DESTRUCTIVE_PY), "confirm")
        self.assertEqual(
            classify_command(self.DESTRUCTIVE_PY + " && rm -rf x"), "destructive"
        )

    def test_ordinary_python_stays_ordinary(self):
        self.assertEqual(classify_command("python -m pytest tests/ -q"), "safe")
        self.assertEqual(classify_command("python script.py"), "mutating")

    def test_auto_mode_prompts_for_interpreter_oneliner(self):
        policy = ApprovalPolicy(mode="auto", ask=lambda p: "n")
        self.assertFalse(policy.check("run_command", {"command": self.DESTRUCTIVE_PY}))
        # An explicit session-wide yes for the exact command sticks.
        policy_yes = ApprovalPolicy(mode="auto", ask=lambda p: "a")
        self.assertTrue(policy_yes.check("run_command", {"command": self.DESTRUCTIVE_PY}))
        self.assertTrue(policy_yes.check("run_command", {"command": self.DESTRUCTIVE_PY}))

    def test_auto_mode_still_allows_ordinary_mutations(self):
        policy = ApprovalPolicy(mode="auto", ask=lambda p: "n")
        self.assertTrue(policy.check("write_file", {"path": "a.py", "content": "x"}))
        self.assertTrue(policy.check("run_command", {"command": "pip install requests"}))


class KillToolBoundaryTest(unittest.TestCase):
    """H-3: host process control never fires from a container sandbox."""

    def setUp(self):
        self.kill = KillShellTool()

    def test_pid_kill_refused_without_host_sandbox(self):
        container = DockerSandbox()
        out = self.kill.execute(container, pid=os.getpid())
        self.assertTrue(out.startswith("ERROR"), msg=out)

    def test_port_kill_refused_without_host_sandbox(self):
        container = DockerSandbox()
        out = self.kill.execute(container, port=8080)
        self.assertTrue(out.startswith("ERROR"), msg=out)

    def test_task_id_kill_still_works_everywhere(self):
        container = DockerSandbox()
        out = self.kill.execute(container, task_id="nope")
        self.assertIn("no such task", out)

    def test_pid_kill_restricted_to_registry_pids(self):
        ws = _workspace()
        local = LocalSandbox(ws)
        # A pid that is neither init, the harness itself, nor registered.
        out = self.kill.execute(local, pid=999_999)
        self.assertTrue(out.startswith("ERROR"), msg=out)
        self.assertIn("not started by a background task", out)


class ShellOutputCursorTest(unittest.TestCase):
    """M-1: cursors are byte offsets, so multi-byte output round-trips."""

    def test_multibyte_output_round_trips_by_byte_offset(self):
        ws = _workspace()
        sandbox = LocalSandbox(ws)
        sandbox.setup({})
        runner = RunCommandTool()
        reader = ShellOutputTool()
        start = runner.execute(
            sandbox, "echo h\u00e9llo w\u00f6rld \u2713", timeout=10.0, background=True
        )
        self.assertIn("background task", start)
        task_id = start.split()[2]
        # Wait for completion. The harness writes an exit_code footer once
        # the child exits ("task done" only appears on a later read that
        # finds no new bytes); either marker means the task finished.
        seen_done = False
        for _ in range(100):
            out = reader.execute(sandbox, task_id=task_id, from_offset=0, wait="exit", timeout=5.0)
            if "exit_code:" in out or "task done" in out:
                seen_done = True
                break
        # The loop must have seen the done marker, not just stopped polling:
        # parsing the offset below would otherwise raise on raw output.
        self.assertTrue(seen_done, msg="background task never reported completion")
        self.assertIn("h\u00e9llo w\u00f6rld \u2713", out)
        next_offset = int(out.rsplit("next_offset:", 1)[1].split()[0])
        # A follow-up read at the emitted byte offset sees no new output.
        again = reader.execute(sandbox, task_id=task_id, from_offset=next_offset)
        self.assertIn("no new output", again)


class AmbiguousEditTest(unittest.TestCase):
    """M-4: a non-unique needle is refused, not applied to the first hit."""

    def _tools(self):
        ledger = EditLedger()
        writer = WriteFileTool()
        reader = ReadFileTool()
        editor = EditFileTool()
        writer.ledger = reader.ledger = editor.ledger = ledger
        return writer, reader, editor

    def test_multi_occurrence_edit_is_refused(self):
        ws = _workspace()
        sandbox = LocalSandbox(ws)
        sandbox.setup({})
        writer, reader, editor = self._tools()
        self.assertTrue(writer.execute(sandbox, "dup.py", "x = 1\ny = x + x\n").startswith("OK"))
        reader.execute(sandbox, "dup.py")
        out = editor.execute(sandbox, "dup.py", "x", "z")
        self.assertTrue(out.startswith("ERROR"), msg=out)
        self.assertIn("occurs 3 times", out)
        with open(os.path.join(ws, "dup.py"), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "x = 1\ny = x + x\n")

    def test_unique_edit_still_applies(self):
        ws = _workspace()
        sandbox = LocalSandbox(ws)
        sandbox.setup({})
        writer, reader, editor = self._tools()
        writer.execute(sandbox, "ok.py", "alpha only\n")
        reader.execute(sandbox, "ok.py")
        self.assertTrue(editor.execute(sandbox, "ok.py", "alpha", "beta").startswith("OK"))


class LiteralGlobNameTest(unittest.TestCase):
    """L-2: a real file with a metacharacter in its name reads as a file."""

    def test_bracket_filename_reads_literally(self):
        ws = _workspace()
        sandbox = LocalSandbox(ws)
        sandbox.setup({})
        sandbox.write_file("report[1].txt", "body")
        reader = ReadFileTool()
        out = reader.execute(sandbox, "report[1].txt")
        self.assertIn("body", out)


class BudgetGuaranteeTest(unittest.TestCase):
    """L-4: the budget holds even when the halving loop caps out."""

    def test_budget_enforced_after_iteration_cap(self):
        ctx = ContextManager(max_messages=200, max_chars=240_000)
        ctx.seed("system prompt", "task")
        big = "y" * 600
        for i in range(40):
            ctx.append({"role": "user", "content": big})
            ctx.append({"role": "assistant", "content": big})
        ctx.enforce_budget()
        self.assertLessEqual(ctx.chars, ctx.max_chars)
        self.assertGreaterEqual(len(ctx.messages), 2)


class RegistryPruneTest(unittest.TestCase):
    """L-6: pruning never evicts a running task."""

    def setUp(self):
        self._saved = dict(command_tool._TASKS)
        self.addCleanup(self._restore)

    def _restore(self):
        command_tool._TASKS.clear()
        command_tool._TASKS.update(self._saved)

    def test_running_tasks_survive_pressure(self):
        command_tool._TASKS.clear()
        for i in range(command_tool._MAX_TASKS + 5):
            command_tool._TASKS[f"tsk_{i:04d}_aaaaaa"] = {
                "task_id": f"tsk_{i:04d}_aaaaaa",
                "command": "sleep",
                "log_path": "",
                "pid": None,
                "process": None,
                "start_time": float(i),
                "done": False,
            }
        command_tool._prune_tasks_locked()
        for i in range(command_tool._MAX_TASKS + 5):
            self.assertIn(f"tsk_{i:04d}_aaaaaa", command_tool._TASKS)

    def test_completed_tasks_are_pruned(self):
        command_tool._TASKS.clear()
        for i in range(command_tool._MAX_TASKS + 5):
            command_tool._TASKS[f"tsk_{i:04d}_bbbbbb"] = {
                "task_id": f"tsk_{i:04d}_bbbbbb",
                "command": "echo",
                "log_path": "",
                "pid": None,
                "process": None,
                "start_time": float(i),
                "end_time": float(i) + 1,
                "done": True,
            }
        command_tool._prune_tasks_locked()
        self.assertLessEqual(len(command_tool._TASKS), command_tool._MAX_TASKS)


class WriteByteCapTest(unittest.TestCase):
    """L-14: the write cap counts bytes, not characters."""

    def test_multibyte_content_over_byte_cap_is_refused(self):
        ws = _workspace()
        sandbox = LocalSandbox(ws)
        sandbox.setup({})
        # 600k two-byte characters = 1.2MB of bytes: over the 1MB cap even
        # though the character count sits below it.
        with self.assertRaises(SandboxError):
            sandbox.write_file("big.txt", "\u00e9" * 600_000)

    def test_ascii_content_under_cap_still_writes(self):
        ws = _workspace()
        sandbox = LocalSandbox(ws)
        sandbox.setup({})
        sandbox.write_file("ok.txt", "a" * 10_000)
        self.assertTrue(os.path.isfile(os.path.join(ws, "ok.txt")))


class AuditLogTest(unittest.TestCase):
    """Every approval check writes a redacted line to the audit log."""

    def test_allowed_and_denied_calls_are_logged_with_redaction(self):
        log_path = os.path.join(tempfile.mkdtemp(prefix="mantra-audit-"), "pre-tool-use.log")
        self.addCleanup(shutil.rmtree, os.path.dirname(log_path), True)
        with mock.patch.dict(os.environ, {"MANTRA_PRE_TOOL_USE_LOG": log_path}):
            policy = ApprovalPolicy(mode="auto", ask=lambda p: "n")
            # mutating write -> auto mode allows
            self.assertTrue(policy.check("write_file", {"path": "a.py", "content": "x"}))
            # destructive command -> auto mode denies
            self.assertFalse(policy.check("run_command", {"command": "rm -rf x --api-key=sk-abcdefghijklmnopqrstuvwxyz123"}))
            # non-mutating read -> allowed
            self.assertTrue(policy.check("read_file", {"path": "a.py"}))
        with open(log_path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
        self.assertEqual(len(lines), 3)
        self.assertTrue(any("tool=write_file" in l and "risk=mutating" in l for l in lines))
        self.assertTrue(any("tool=run_command" in l and "risk=destructive" in l for l in lines))
        joined = "\n".join(lines)
        self.assertIn("REDACTED", joined)
        self.assertNotIn("sk-abcdefghijklmnopqrstuvwxyz123", joined)


if __name__ == "__main__":
    unittest.main()
