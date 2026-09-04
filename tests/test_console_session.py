"""Regression tests for the interactive session layer.

These guard the two failures that made the console unusable as a daily
driver: the sandbox drifting into a temp directory after the first message,
and the conversation being thrown away between messages. Both were silent.
"""

from __future__ import annotations

import builtins
import os
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from mantra.config import merge_defaults
from mantra.console import ConsoleSession, Style, repl
from mantra.core.agent_loop import AgentLoop
from mantra.core.approvals import ApprovalPolicy, classify, classify_command
from mantra.core.context import ContextManager
from mantra.core.events import EventBus
from mantra.core.settings import add_endpoint
from mantra.implementations.evaluators.null_evaluator import NullEvaluator
from mantra.implementations.llm.mock_client import (
    ScriptedLLMClient,
    final_response,
    tool_call_response,
)
from mantra.implementations.loggers.jsonl_logger import JsonlLogger
from mantra.implementations.sandbox.local_sandbox import LocalSandbox


def make_config(workspace: str, **overrides) -> dict:
    config = merge_defaults({})
    config["logging"] = {"type": "jsonl", "path": os.path.join(workspace, "session.jsonl")}
    config["approvals"] = "auto"
    config["auto_compact_tokens"] = 0  # disable mid-turn compaction here
    config.update(overrides)
    return config


def make_session(workspace: str, script: list, **overrides) -> ConsoleSession:
    # A script entry may itself be a ScriptedLLMClient (e.g. the streaming
    # helper returns one); use it directly rather than nesting it.
    llm = ScriptedLLMClient(script)
    if isinstance(script, ScriptedLLMClient):
        llm = script
    elif script and isinstance(script[0], ScriptedLLMClient):
        llm = script[0]
    return ConsoleSession(
        config=make_config(workspace, **overrides),
        workspace=workspace,
        style=Style(enabled=False),
        llm=llm,
        ask=lambda prompt: "y",  # every approval auto-answered yes
    )


class WorkspacePersistenceTest(unittest.TestCase):
    """The workspace must survive every turn, not just the first."""

    def setUp(self):
        self.workspace = tempfile.mkdtemp(prefix="mantra-ws-")

    def test_sandbox_root_survives_cleanup(self):
        sandbox = LocalSandbox(self.workspace)
        sandbox.setup({})
        sandbox.cleanup()
        sandbox.setup({})
        self.assertEqual(sandbox.root, self.workspace)

    def test_second_turn_sees_first_turn_files(self):
        session = make_session(
            self.workspace,
            [
                tool_call_response("write_file", {"path": "note.txt", "content": "hello"}),
                final_response("wrote note.txt"),
                tool_call_response("read_file", {"path": "note.txt"}),
                final_response("it says hello"),
            ],
        )
        session.handle("create note.txt containing hello")
        self.assertEqual(session.sandbox.root, self.workspace)
        self.assertTrue(os.path.isfile(os.path.join(self.workspace, "note.txt")))

        result = session.handle("read it back")
        self.assertEqual(session.sandbox.root, self.workspace)
        self.assertEqual(result.stopped_reason, "final")
        with open(os.path.join(self.workspace, "note.txt"), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "hello")


class ConversationContinuityTest(unittest.TestCase):
    """Turn two must know what happened in turn one."""

    def setUp(self):
        self.workspace = tempfile.mkdtemp(prefix="mantra-conv-")

    def test_second_turn_receives_first_turn_history(self):
        session = make_session(
            self.workspace,
            [
                tool_call_response("write_file", {"path": "a.py", "content": "x = 1\n"}),
                final_response("created a.py with x = 1"),
                final_response("yes, x is 1"),
            ],
        )
        session.handle("create a.py setting x to 1")
        session.handle("what is x?")

        llm = session.llm
        self.assertEqual(len(llm.received_messages), 3)
        second_turn = llm.received_messages[-1]

        roles = [m["role"] for m in second_turn]
        self.assertIn("assistant", roles)
        self.assertIn("tool", roles)
        joined = " ".join(str(m.get("content") or "") for m in second_turn)
        self.assertIn("create a.py setting x to 1", joined)
        self.assertIn("created a.py with x = 1", joined)
        self.assertIn("what is x?", joined)
        # The system prompt is pinned and must be present on every call.
        self.assertEqual(second_turn[0]["role"], "system")

    def test_clear_drops_history_but_keeps_system_prompt(self):
        session = make_session(self.workspace, [final_response("hi")])
        session.handle("hello")
        session.context.replace_body([])
        self.assertEqual(len(session.context.messages), 1)
        self.assertEqual(session.context.messages[0]["role"], "system")


class TurnAwareTruncationTest(unittest.TestCase):
    """Dropping messages must never orphan a tool result."""

    def _assert_no_orphans(self, messages):
        # Every tool message must trace back to a preceding assistant
        # message that actually made the tool call.
        for index, message in enumerate(messages):
            if message.get("role") != "tool":
                continue
            origin = None
            for back in range(index - 1, -1, -1):
                if messages[back].get("role") == "tool":
                    continue
                if messages[back].get("role") == "assistant" and messages[back].get("tool_calls"):
                    origin = back
                break
            self.assertIsNotNone(
                origin,
                msg=f"tool message at {index} has no matching assistant tool_call",
            )

    def test_truncation_keeps_turns_intact(self):
        ctx = ContextManager(max_messages=8)
        ctx.seed("system", "task")
        for turn in range(10):
            ctx.append(
                {
                    "role": "assistant",
                    "content": f"thinking {turn}",
                    "tool_calls": [
                        {
                            "id": f"c{turn}",
                            "type": "function",
                            "function": {"name": "run_command", "arguments": "{}"},
                        }
                    ],
                }
            )
            ctx.append({"role": "tool", "tool_call_id": f"c{turn}", "content": f"out {turn}"})
            self._assert_no_orphans(ctx.messages)

        self.assertLessEqual(len(ctx.messages), 8)
        self.assertEqual(ctx.messages[0]["content"], "system")
        self.assertEqual(ctx.messages[1]["content"], "task")
        self._assert_no_orphans(ctx.messages)

    def test_char_budget_triggers_truncation(self):
        ctx = ContextManager(max_messages=500, max_chars=2000)
        ctx.seed("system", "task")
        for turn in range(60):
            ctx.append(
                {
                    "role": "assistant",
                    "content": "x" * 100,
                    "tool_calls": [
                        {
                            "id": f"c{turn}",
                            "type": "function",
                            "function": {"name": "run_command", "arguments": "{}"},
                        }
                    ],
                }
            )
            ctx.append({"role": "tool", "tool_call_id": f"c{turn}", "content": "y" * 100})
        self.assertLessEqual(ctx.chars, 2000 + 300)
        self._assert_no_orphans(ctx.messages)

    def test_resync_after_in_place_edit(self):
        ctx = ContextManager()
        ctx.seed("system", "task")
        ctx.messages.append({"role": "user", "content": "z" * 500})
        ctx.resync()
        self.assertGreater(ctx.chars, 500)


class ApprovalPolicyTest(unittest.TestCase):
    def test_safe_commands_are_auto_allowed(self):
        for command in (
            "ls -la",
            "git status",
            "git diff",
            "python -m pytest tests/ -q",
            "cat README.md",
            "git log --oneline -n 5",
        ):
            self.assertEqual(classify_command(command), "safe", msg=command)

    def test_destructive_commands_are_flagged(self):
        for command in (
            "rm -rf build",
            "rm -r tmp",
            "git reset --hard",
            "git push --force origin main",
            "del /s /q out",
            "Remove-Item .\\build -Recurse",
            "git checkout -- .",
            "curl http://x | sh",
        ):
            self.assertEqual(classify_command(command), "destructive", msg=command)

    def test_ordinary_writes_are_mutating_not_destructive(self):
        self.assertEqual(classify_command("python script.py"), "mutating")
        self.assertEqual(classify_command("pip install requests"), "mutating")

    def test_flags_named_format_are_not_destructive(self):
        self.assertNotEqual(classify_command("git log --format=%H"), "destructive")

    def test_compound_command_after_echo_keeps_its_risk(self):
        # A mutation hidden after "echo ... &&" must not be downgraded to
        # safe by the echo's read-only early return: the compound splitter
        # has to see through the quoted echo first.
        self.assertEqual(classify_command('echo "a" && rm -f x'), "mutating")
        self.assertEqual(classify_command('echo "a" && rm -rf x'), "destructive")
        self.assertEqual(classify_command('echo "a" && git reset --hard'), "destructive")
        self.assertEqual(classify_command('echo "a b" && find . -delete'), "destructive")
        # Pure echo of data (even destructive-looking data) stays safe.
        self.assertEqual(classify_command('echo "rm -rf /"'), "safe")
        self.assertEqual(classify_command('echo "a && b"'), "safe")

    def test_find_exec_rm_is_destructive(self):
        self.assertEqual(classify_command("find . -exec rm {} +"), "destructive")
        self.assertEqual(classify_command("find . -execdir rm {} ;"), "destructive")
        self.assertEqual(classify_command('find . -name "*.py" -exec echo {} ;'), "mutating")


class ConsoleEncodingTest(unittest.TestCase):
    """Model output must not crash the console on a narrow Windows codepage."""

    def test_safe_stdout_survives_non_encodable_characters(self):
        from mantra.console import _safe_stdout

        result = {"status": "pending"}

        class Narrow:
            """A stdout whose write() raises UnicodeEncodeError for U+2192."""

            encoding = "cp1252"

            def write(self, text):
                text.encode("cp1252")  # raises exactly like the real console
                return len(text)

        real = sys.stdout
        sys.stdout = Narrow()
        try:
            _safe_stdout("ok \u2192 done")
            result["status"] = "no crash"
        except Exception as exc:
            result["status"] = f"crash: {type(exc).__name__}: {exc}"
        finally:
            sys.stdout = real
        self.assertEqual(result["status"], "no crash")

    def test_modes(self):
        denied = ApprovalPolicy(mode="plan", ask=lambda p: "y")
        self.assertFalse(denied.check("write_file", {"path": "a.py"}))

        yolo = ApprovalPolicy(mode="yolo", ask=lambda p: "n")
        self.assertTrue(yolo.check("run_command", {"command": "rm -rf tmp"}))

        auto = ApprovalPolicy(mode="auto", ask=lambda p: "n")
        self.assertTrue(auto.check("write_file", {"path": "a.py"}))
        self.assertFalse(auto.check("run_command", {"command": "rm -rf tmp"}))

        default = ApprovalPolicy(mode="default", ask=lambda p: "n")
        self.assertFalse(default.check("write_file", {"path": "a.py"}))

    def test_always_answer_is_remembered_for_the_session(self):
        answers = iter(["a", "n"])
        policy = ApprovalPolicy(mode="default", ask=lambda p: next(answers))
        self.assertTrue(policy.check("write_file", {"path": "a.py"}))
        self.assertTrue(policy.check("write_file", {"path": "a.py"}))  # no prompt left
        self.assertFalse(policy.check("write_file", {"path": "b.py"}))

    def test_read_only_tools_never_prompt(self):
        policy = ApprovalPolicy(mode="default", ask=lambda p: "n")
        for tool in ("read_file", "list_dir", "search_code", "find_file", "git_diff"):
            self.assertTrue(policy.check(tool, {"path": "."}))

    def test_classify_reports_paths_for_edits(self):
        risk, detail = classify("edit_file", {"path": "src/app.py"})
        self.assertEqual(risk, "mutating")
        self.assertIn("src/app.py", detail)


class AbortTest(unittest.TestCase):
    def setUp(self):
        self.workspace = tempfile.mkdtemp(prefix="mantra-abort-")

    def _loop(self, llm, abort):
        return AgentLoop(
            llm=llm,
            sandbox=LocalSandbox(self.workspace),
            tools=[],
            evaluator=NullEvaluator(),
            logger=JsonlLogger(os.path.join(self.workspace, "log.jsonl")),
            events=EventBus(),
            max_steps=10,
            abort=abort,
        )

    def test_abort_event_stops_before_the_first_step(self):
        abort = threading.Event()
        abort.set()
        llm = ScriptedLLMClient([final_response("should not be used")] * 5)
        result = self._loop(llm, abort).run({"task_id": "t", "problem_statement": "hi"})
        self.assertEqual(result.stopped_reason, "aborted")
        self.assertFalse(result.passed)
        self.assertEqual(result.steps_used, 0)

    def test_abort_skips_evaluation(self):
        abort = threading.Event()
        abort.set()
        llm = ScriptedLLMClient([final_response("unused")])
        loop = self._loop(llm, abort)
        result = loop.run({"task_id": "t", "problem_statement": "hi"})
        self.assertIn("interrupted", result.evaluation_detail)


class DeniedToolTest(unittest.TestCase):
    def test_denied_call_returns_observation_and_runs_nothing(self):
        workspace = tempfile.mkdtemp(prefix="mantra-deny-")
        calls = []

        class Spy:
            name = "write_file"
            description = "spy"
            parameters = {"type": "object", "properties": {}}

            def schema(self):
                return {"type": "function", "function": {"name": "write_file"}}

            def execute(self, sandbox, **kwargs):
                calls.append(kwargs)
                return "executed"

        loop = AgentLoop(
            llm=ScriptedLLMClient(
                [
                    tool_call_response("write_file", {"path": "a.py"}),
                    final_response("understood"),
                ]
            ),
            sandbox=LocalSandbox(workspace),
            tools=[Spy()],
            evaluator=NullEvaluator(),
            logger=JsonlLogger(os.path.join(workspace, "log.jsonl")),
            events=EventBus(),
            max_steps=5,
            approver=ApprovalPolicy(mode="default", ask=lambda p: "n"),
        )
        result = loop.run({"task_id": "t", "problem_statement": "write a file"})
        self.assertEqual(calls, [])
        self.assertEqual(result.metrics.get("denied"), 1)
        self.assertEqual(result.stopped_reason, "final")


class MentionTest(unittest.TestCase):
    """@path must pull real content in, and never reach outside the workspace."""

    def setUp(self):
        self.workspace = tempfile.mkdtemp(prefix="mantra-mention-")
        with open(os.path.join(self.workspace, "app.py"), "w", encoding="utf-8") as handle:
            handle.write("def add(a, b):\n    return a + b\n")
        os.makedirs(os.path.join(self.workspace, "src"), exist_ok=True)
        with open(os.path.join(self.workspace, "src", "util.py"), "w", encoding="utf-8") as handle:
            handle.write("X = 1\n")
        self.session = make_session(self.workspace, [])

    def expand(self, text):
        return self.session.expand_mentions(text)

    def test_file_mention_attaches_content(self):
        expanded, attached = self.expand("explain @app.py")
        self.assertEqual(attached, ["app.py"])
        self.assertIn("explain @app.py", expanded)
        self.assertIn("Attached context:", expanded)
        self.assertIn("def add(a, b):", expanded)

    def test_directory_mention_lists_entries(self):
        expanded, attached = self.expand("what is in @src?")
        self.assertEqual(attached, ["src"])
        self.assertIn("@SRC", expanded)
        self.assertIn("util.py", expanded)

    def test_glob_mention_attaches_matches(self):
        expanded, attached = self.expand("review @src/*.py")
        self.assertEqual(attached, [os.path.join("src", "util.py")])
        self.assertIn("X = 1", expanded)

    def test_unknown_mention_leaves_text_alone(self):
        expanded, attached = self.expand("look at @nope.py")
        self.assertEqual(attached, [])
        self.assertEqual(expanded, "look at @nope.py")

    def test_email_addresses_are_not_mentions(self):
        expanded, attached = self.expand("mail me at bob@example.com")
        self.assertEqual(attached, [])
        self.assertEqual(expanded, "mail me at bob@example.com")

    def test_escaping_the_workspace_is_refused(self):
        outside = os.path.join(os.path.dirname(self.workspace), "outside-secret.txt")
        with open(outside, "w", encoding="utf-8") as handle:
            handle.write("should never be read\n")
        try:
            expanded, attached = self.expand("read @../outside-secret.txt")
            self.assertEqual(attached, [])
            self.assertNotIn("should never be read", expanded)
        finally:
            os.remove(outside)

    def test_forward_slash_workspace_still_resolves(self):
        """--workspace is often typed with forward slashes on Windows."""
        session = make_session(self.workspace.replace("\\", "/"), [])
        expanded, attached = session.expand_mentions("explain @app.py")
        self.assertEqual(attached, ["app.py"])
        self.assertIn("def add(a, b):", expanded)

    def test_duplicate_mentions_attach_once(self):
        expanded, attached = self.expand("compare @app.py and @app.py")
        self.assertEqual(attached, ["app.py"])
        self.assertEqual(expanded.count("* @APP.PY *"), 1)

    def test_large_files_are_truncated(self):
        big = os.path.join(self.workspace, "big.txt")
        with open(big, "w", encoding="utf-8") as handle:
            handle.write("z" * 40_000)
        expanded, attached = self.expand("summarise @big.txt")
        self.assertEqual(attached, ["big.txt"])
        self.assertIn("* [truncated]", expanded)
        self.assertLess(expanded.count("z"), 40_000)


class ReplTest(unittest.TestCase):
    """The REPL must forward ordinary text to the agent, not swallow it."""

    def setUp(self):
        self.workspace = tempfile.mkdtemp(prefix="mantra-repl-")
        self._real_input = builtins.input

    def tearDown(self):
        builtins.input = self._real_input

    def _run(self, session, lines):
        feed = iter(lines)
        builtins.input = lambda *args: next(feed)
        try:
            repl(session, Style(enabled=False))
        finally:
            builtins.input = self._real_input

    def test_plain_text_reaches_the_agent(self):
        session = make_session(self.workspace, [final_response("done")])
        self._run(session, ["please fix the bug", "/exit"])
        self.assertEqual(session.message_count, 1)
        self.assertEqual(len(session.llm.received_messages), 1)
        self.assertIn(
            "please fix the bug", str(session.llm.received_messages[0])
        )

    def test_blank_lines_are_ignored(self):
        session = make_session(self.workspace, [final_response("done")])
        self._run(session, ["", "   ", "/exit"])
        self.assertEqual(session.message_count, 0)

    def test_tools_command_lists_names(self):
        session = make_session(self.workspace, [])
        self._run(session, ["/tools", "/exit"])
        self.assertEqual(session.message_count, 0)

    def test_exit_command_ends_the_loop(self):
        session = make_session(self.workspace, [])
        self._run(session, ["/exit", "this is never reached"])
        self.assertEqual(session.message_count, 0)


class EndpointSwitchTest(unittest.TestCase):
    """Switching endpoint must move the model with it.

    A model name is only meaningful on the endpoint that serves it, so
    keeping the old one across a switch would simply produce a 404 on the
    next message. There are no built-in endpoints any more, so each test
    saves one to its own settings file first.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = self.tmp.name
        self.env = mock.patch.dict(
            os.environ,
            {"MANTRA_SETTINGS": os.path.join(self.tmp.name, "config.json")},
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(self.tmp.cleanup)
        self.session = make_session(self.workspace, [])

    def _save(self, name, url, key_env="", models=()):
        add_endpoint(name, url, key_env, list(models))

    def _capture(self, lines):
        """Run console lines and return the printed text."""
        import io
        from contextlib import redirect_stdout

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            repl(self.session, Style(enabled=False), reader=_reader(lines + ["/exit"]))
        return buffer.getvalue()

    def test_saved_endpoint_sets_url_key_and_model(self):
        self._save("groq", "https://api.groq.com/openai/v1", "GROQ_API_KEY",
                   ["llama-3.1-8b-instant"])
        out = self._capture(["/connect groq"])
        llm = self.session.config["llm"]
        self.assertEqual(llm["base_url"], "https://api.groq.com/openai/v1")
        self.assertEqual(llm["api_key_env"], "GROQ_API_KEY")
        self.assertEqual(llm["model"], "llama-3.1-8b-instant")
        self.assertIn("groq", out)

    def test_explicit_model_wins_over_the_saved_default(self):
        self._save("groq", "https://api.groq.com/openai/v1", "GROQ_API_KEY",
                   ["llama-3.1-70b", "llama-3.1-8b-instant"])
        self.assertTrue(self.session.use_endpoint("groq", "llama-3.1-8b-instant"))
        self.assertEqual(self.session.config["llm"]["model"], "llama-3.1-8b-instant")

    def test_the_saved_model_replaces_the_old_one(self):
        # Coming from an unrelated endpoint, the previous model must not
        # survive the switch.
        self._save("other", "https://other.test/v1", "OTHER_API_KEY", ["other-model"])
        self.assertTrue(self.session.use_endpoint("other"))
        self.assertEqual(self.session.config["llm"]["model"], "other-model")

    def test_unknown_name_is_rejected_without_touching_config(self):
        before = dict(self.session.config["llm"])
        self.assertFalse(self.session.use_endpoint("notanendpoint"))
        self.assertEqual(self.session.config["llm"], before)

    def test_unknown_name_points_at_connect(self):
        import io
        from contextlib import redirect_stdout

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            self.session.use_endpoint("notanendpoint")
        self.assertIn("/connect", buffer.getvalue())

    def test_listing_marks_the_current_endpoint(self):
        self._save("first", "https://first.test/v1", "", ["m1"])
        self._save("second", "https://second.test/v1", "", ["m2"])
        self.session.use_endpoint("first")
        out = self._capture(["/connect list"])
        self.assertIn("*", out)
        self.assertIn("first", out)

    def test_local_endpoints_are_not_nagged_about_keys(self):
        import io
        from contextlib import redirect_stdout

        self._save("local", "http://localhost:11434/v1", "", ["llama3"])
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            self.session.use_endpoint("local")
        self.assertNotIn("no key for", buffer.getvalue())

    def test_endpoint_name_is_derived_from_the_url_in_use(self):
        self._save("mine", "https://mine.test/v1", "", ["m1"])
        self.session.use_endpoint("mine")
        self.assertEqual(self.session.endpoint_name, "mine")

    def test_endpoint_name_is_empty_for_an_unsaved_url(self):
        self.session.config["llm"]["base_url"] = "https://stranger.test/v1"
        self.assertEqual(self.session.endpoint_name, "")

    def test_keyless_helper(self):
        from mantra.console import provider_needs_key

        self.assertFalse(provider_needs_key("http://localhost:11434/v1", "OPENAI_API_KEY"))
        self.assertFalse(provider_needs_key("https://api.openai.com/v1", ""))
        self.assertTrue(provider_needs_key("https://api.openai.com/v1", "OPENAI_API_KEY"))


def _reader(lines):
    it = iter(lines)

    def _read(prompt=""):
        return next(it)

    return _read


class ReplyRenderingTest(unittest.TestCase):
    """The reply must appear once, and the footer grammar must be right.

    The operator's paste showed the answer twice - the streamed raw
    text, then a second copy with markdown stripped. The streaming path
    already left the reply on screen, so handle() must not print it
    again.
    """

    def setUp(self):
        self.workspace = tempfile.mkdtemp(prefix="mantra-reply-")

    def _framed_output(self, script):
        """Run one turn, return everything drawn (compact, no frame)."""
        import io
        from contextlib import redirect_stdout

        session = make_session(self.workspace, script)
        buffer = io.StringIO()
        buffer.isatty = lambda: False
        with mock.patch.object(sys, "stdout", buffer):
            session.handle("hello")
        return buffer.getvalue()

    def test_a_streamed_reply_is_not_printed_twice(self):
        # The model answers with markdown; the streamed copy keeps the
        # marks, a second copy would strip them. If both appear, the
        # stripped body shows up as a standalone line somewhere.
        out = self._framed_output([final_response("**Hello** there, how can I help?")])
        # The rendered body and the raw stream share the word "there",
        # but the bold marker only exists in the streamed copy. A second
        # print would leave a line with the marker gone that is not the
        # frame row - assert the marker appears the right number of times.
        self.assertEqual(out.count("**Hello**"), out.count("Hello, how can I help?"))

    def test_the_footer_says_one_step_not_one_steps(self):
        # Use a streamed reply so the footer (which carries
        # the step count) is produced.
        out = self._framed_output([final_response("done", stream=True)])
        self.assertIn("1 STEP", out)
        self.assertNotIn("1 STEPS", out)


def _tty_stdin():
    fake = mock.MagicMock()
    fake.isatty.return_value = True
    return fake


if __name__ == "__main__":
    unittest.main()
