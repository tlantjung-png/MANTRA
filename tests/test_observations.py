"""Observation reshaping: the dense context copy, the raw operator copy.

Two contracts are pinned here. The first is *what reshaping may do*, which
depends on what the model will do with the output: telemetry is collapsed,
verbatim tool output (above all file reads the model edits against) is only
capped. The second is that reshaping never touches the copy the operator and
/fix see - the loop's failure tracking and the UI both read the raw text.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "."))
_tests_dir = os.path.dirname(os.path.abspath(__file__))
if _tests_dir not in sys.path:
    sys.path.insert(0, _tests_dir)

from core.agent.observations import (  # noqa: E402
    DEFAULT_MAX_CHARS,
    TELEMETRY_TOOLS,
    reshape,
    reshape_observation,
)

from core.scripted import LLMResponse, tool_call_response  # noqa: E402
from tui_harness import _TEMP_WORKSPACES, _make_app, wait_until  # noqa: E402


def _noisy(rows: int = 60, width: int = 70) -> str:
    return "\n".join(f"row {i:03d} " + "z" * width for i in range(rows))


class ReshapeContractTest(unittest.TestCase):
    """What reshaping is allowed to change, per tool."""

    def test_telemetry_is_collapsed(self):
        text = "\n".join([
            "start", "", "", "", "same", "same", "same",
            "\u2500" * 40, "\u2500" * 40, "end",
        ])
        out = reshape("run_command", text, max_chars=10**6)
        self.assertIn("[3 blank lines elided]", out)
        self.assertIn("[2 identical lines elided]", out)
        self.assertIn("[2 separator lines elided]", out)
        self.assertIn("start", out)
        self.assertIn("end", out)

    def test_verbatim_tool_output_is_never_reordered_or_deduped(self):
        # read_file output is what edit_file's anchor checks match against:
        # collapsing a blank-line run here would put the model's view of the
        # file out of step with the file itself.
        source = "\n".join(["def f():", "", "", "    return 1", "    return 1", "    return 1"])
        for tool in ("read_file", "search_code", "find_file", "git_diff", "list_dir"):
            self.assertEqual(
                reshape(tool, source, max_chars=10**6), source,
                f"{tool} output was rewritten",
            )

    def test_verbatim_output_keeps_carriage_returns(self):
        # A CRLF file must reach the model as it is on disk, or an edit
        # built from the reshaped copy would not match.
        self.assertEqual(reshape("read_file", "a\r\nb", max_chars=10**6), "a\r\nb")

    def test_short_verbatim_observation_is_byte_identical(self):
        text = "Note: 'x.py' (12 lines, showing 12 lines)\nline one\nline two"
        self.assertEqual(reshape("read_file", text, max_chars=DEFAULT_MAX_CHARS), text)

    def test_short_telemetry_is_still_cleaned(self):
        # Escapes and CR cost context even in a short command log.
        self.assertEqual(reshape("run_command", "a\r\n\x1b[31mb\x1b[0m", max_chars=10**6), "a\nb")

    def test_leading_error_header_survives_the_cap(self):
        text = "ERROR: no such file: x.py\n" + "traceback line\n" * 200
        out = reshape("read_file", text, max_chars=400)
        self.assertTrue(out.startswith("ERROR: no such file: x.py"), out[:80])

    def test_leading_exit_code_header_survives_the_cap(self):
        text = "exit_code: 2\nstderr:\n boom\n" + "x" * 4000
        out = reshape("run_command", text, max_chars=400)
        self.assertTrue(out.startswith("exit_code: 2"), out[:60])

    def test_leading_note_header_survives_the_cap(self):
        text = "Note: 'big.py' (9000 lines, showing 2000 lines)\n" + "y" * 4000
        out = reshape("read_file", text, max_chars=400)
        self.assertTrue(out.startswith("Note: 'big.py'"), out[:60])

    def test_header_stops_at_the_first_content_line(self):
        text = "exit_code: 0\nstdout:\nERROR: not really a header\n" + "x" * 4000
        out = reshape("run_command", text, max_chars=400)
        self.assertIn("ERROR: not really a header", out.split("... [")[0] + out.split("] ...")[-1])

    def test_diff_syntax_is_never_mistaken_for_a_rule(self):
        diff = "--- a/x.py\n+++ b/x.py\n@@ -1,2 +1,2 @@\n-old\n+new\n ctx"
        self.assertEqual(reshape("run_command", diff, max_chars=10**6), diff)

    def test_cap_keeps_both_ends_and_marks_the_middle(self):
        out = reshape("read_file", _noisy(300), max_chars=3000)
        lines = out.splitlines()
        self.assertLessEqual(len(out), 3200)
        self.assertIn("row 000", lines[0])
        self.assertIn("row 299", lines[-1])
        self.assertTrue(any("characters elided" in ln for ln in lines), "elision is unmarked")

    def test_telemetry_cap_keeps_more_tail_than_head(self):
        # A command's verdict is at the end, so the tail share is larger.
        text = "\n".join(f"line {i:03d}" for i in range(400)) + "\nexit_code: 1"
        out = reshape("run_command", text, max_chars=2000)
        self.assertIn("line 399", out)
        self.assertIn("characters elided", out)

    def test_reshaping_is_deterministic(self):
        text = _noisy(120)
        self.assertEqual(reshape("run_command", text, 4000), reshape("run_command", text, 4000))
        self.assertEqual(reshape("read_file", text, 4000), reshape("read_file", text, 4000))

    def test_output_is_never_empty(self):
        self.assertTrue(reshape("run_command", "\n\n\n", max_chars=100))
        self.assertTrue(reshape("read_file", "\n\n\n", max_chars=100))

    def test_zero_or_negative_ceiling_disables_reshaping(self):
        text = _noisy(50)
        self.assertEqual(reshape("run_command", text, 0), text)
        self.assertEqual(reshape("run_command", text, -5), text)

    def test_non_string_observation_passes_through(self):
        for value in (None, 42, [], {}):
            self.assertIs(reshape("run_command", value, 100), value)

    def test_a_realistic_log_compresses_substantially(self):
        log = "\n".join(f"[{i:04d}] INFO processing item {i} " + "q" * 60 for i in range(400))
        out = reshape("run_command", log, DEFAULT_MAX_CHARS)
        self.assertLess(len(out), len(log) // 2, f"only {len(log)} -> {len(out)}")

    def test_every_telemetry_tool_is_known(self):
        self.assertEqual(TELEMETRY_TOOLS, {"run_command", "shell_output", "kill_shell"})


class ReshapeAccountingTest(unittest.TestCase):
    """The metrics that make the saving measurable rather than assumed."""

    def test_metrics_record_raw_and_shaped_sizes(self):
        metrics: dict = {}
        shaped = reshape_observation("run_command", _noisy(300), 3000, metrics)
        self.assertEqual(metrics["observation_chars_raw"], len(_noisy(300)))
        self.assertEqual(metrics["observation_chars_context"], len(shaped))
        self.assertGreater(metrics["observation_chars_saved"], 0)

    def test_metrics_accumulate_across_calls(self):
        metrics: dict = {}
        for _ in range(3):
            reshape_observation("run_command", _noisy(50), 1000, metrics)
        self.assertEqual(metrics["observation_chars_raw"], 3 * len(_noisy(50)))

    def test_no_saving_is_recorded_when_nothing_was_removed(self):
        metrics: dict = {}
        reshape_observation("read_file", "tiny", 10**6, metrics)
        self.assertEqual(metrics["observation_chars_saved"], 0)

    def test_metrics_are_optional(self):
        self.assertEqual(reshape_observation("read_file", "x", 100), "x")


class LoopIntegrationTest(unittest.TestCase):
    """The loop reshapes the context copy and nothing else."""

    def tearDown(self):
        for ws in list(_TEMP_WORKSPACES):
            import shutil

            shutil.rmtree(ws, ignore_errors=True)
        _TEMP_WORKSPACES.clear()

    def _run_read(self, max_chars: int):
        app, session, _backend = _make_app(
            [tool_call_response("read_file", {"path": "big.txt"}), LLMResponse(content="done")]
        )
        # The harness owns the workspace; write into the one the session
        # actually sandboxes rather than a directory of our own.
        with open(os.path.join(session.workspace, "big.txt"), "w", encoding="utf-8") as fh:
            fh.write(_noisy(300))
        session.config["observations"] = {"reshape": max_chars > 0, "max_chars": max_chars}
        return app, session

    def test_context_gets_the_dense_copy_the_ui_gets_the_raw_one(self):
        app, session = self._run_read(4000)
        seen: list[int] = []
        session._on_tool_observation = lambda tool, obs, step: seen.append(len(str(obs)))
        app.submit("read the file")
        self.assertTrue(wait_until(lambda: not app.busy, 20))

        tool_messages = [m for m in session.context.messages if m.get("role") == "tool"]
        self.assertEqual(len(tool_messages), 1)
        context_copy = tool_messages[0]["content"]
        self.assertLess(len(context_copy), 4000 + 400, "context copy was not capped")
        self.assertIn("characters elided", context_copy)
        self.assertGreater(seen[0], len(context_copy), "the UI copy was reshaped too")

    def test_failure_tracking_still_sees_the_raw_error(self):
        # The loop marks a call failed by a leading ERROR in the *raw*
        # observation and blocks the third identical repeat off that mark.
        # If reshaping ran first, a capped error line could silently
        # disable repeat-blocking and the agent would spin on a bad call.
        app, session, _backend = _make_app([
            tool_call_response("read_file", {"path": "missing.py"}),
            tool_call_response("read_file", {"path": "missing.py"}),
            tool_call_response("read_file", {"path": "missing.py"}),
            LLMResponse(content="giving up"),
        ])
        app.submit("read missing.py three times")
        self.assertTrue(wait_until(lambda: not app.busy, 20))
        tool_messages = [m["content"] for m in session.context.messages if m.get("role") == "tool"]
        self.assertGreaterEqual(len(tool_messages), 3, tool_messages)
        self.assertTrue(tool_messages[0].startswith("ERROR"), tool_messages[0])
        self.assertIn(
            "STOP RETRYING", tool_messages[2],
            f"repeat-blocking did not fire on the raw error: {tool_messages[2]}",
        )

    def test_the_saving_reaches_the_usage_line(self):
        app, session = self._run_read(4000)
        app.submit("read the file")
        self.assertTrue(wait_until(lambda: not app.busy, 20))
        self.assertGreater(session.totals.get("observation_saved", 0), 0)

    def test_reshaping_can_be_switched_off(self):
        app, session = self._run_read(0)
        app.submit("read the file")
        self.assertTrue(wait_until(lambda: not app.busy, 20))
        tool_messages = [m for m in session.context.messages if m.get("role") == "tool"]
        self.assertEqual(len(tool_messages), 1)
        self.assertNotIn("characters elided", tool_messages[0]["content"])
        self.assertEqual(session.totals.get("observation_saved", 0), 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


class MechanismTagTest(unittest.TestCase):
    """The run log records which mechanisms were in force."""

    def test_the_result_carries_the_mechanism_set(self):
        from core.agent.loop import AgentLoop, RunResult, _result_payload

        loop = AgentLoop(
            llm=None, sandbox=None, tools=[], evaluator=None, logger=None,
            observation_max_chars=4000,
        )
        result = RunResult(
            task_id="t", passed=True, evaluation_detail="", steps_used=1,
            stopped_reason="final",
        )
        result.mechanisms = {
            "observation_reshape": bool(loop.observation_max_chars),
            "observation_max_chars": int(loop.observation_max_chars or 0),
        }
        payload = _result_payload(result)
        self.assertEqual(payload["mechanisms"]["observation_reshape"], True)
        self.assertEqual(payload["mechanisms"]["observation_max_chars"], 4000)

    def test_a_disabled_mechanism_is_reported_as_off(self):
        from core.agent.loop import AgentLoop

        loop = AgentLoop(
            llm=None, sandbox=None, tools=[], evaluator=None, logger=None,
            observation_max_chars=0,
        )
        self.assertFalse(bool(loop.observation_max_chars))

    def test_the_loop_reshapes_by_default(self):
        from core.agent.loop import DEFAULT_MAX_CHARS as LOOP_DEFAULT

        self.assertEqual(LOOP_DEFAULT, DEFAULT_MAX_CHARS)
        from core.agent.loop import AgentLoop

        loop = AgentLoop(llm=None, sandbox=None, tools=[], evaluator=None, logger=None)
        self.assertEqual(loop.observation_max_chars, DEFAULT_MAX_CHARS)
class _DigestAwareLLM:
    """A scripted client that recognises its own digest round trip.

    The digest call is a single user message carrying the summariser
    prompt, so it is distinguishable from a turn request without relying on
    call order - which matters, because how many digest calls a run makes
    depends on how often the budget evicts.
    """

    def __init__(self, turn_responses):
        self.turn_responses = list(turn_responses)
        self.digest_calls = 0
        self.received: list[list[dict]] = []

    def chat(self, messages, tools=None, on_delta=None, **kwargs):
        import copy

        self.received.append(copy.deepcopy(messages))
        first = messages[0] if messages else {}
        if len(messages) == 1 and "Summarise the conversation turns below" in str(
            first.get("content")
        ):
            self.digest_calls += 1
            return LLMResponse(content="DIGEST: the parser was fixed earlier.")
        if not self.turn_responses:
            return LLMResponse(content="finished")
        return self.turn_responses.pop(0)


class EvictionDigestTest(unittest.TestCase):
    """Evicted turns are summarised into a digest the model then reads."""

    def tearDown(self):
        for ws in list(_TEMP_WORKSPACES):
            import shutil

            shutil.rmtree(ws, ignore_errors=True)
        _TEMP_WORKSPACES.clear()

    def _run(self, digest=True, max_messages=4):
        app, session, _backend = _make_app([])
        with open(os.path.join(session.workspace, "big.txt"), "w", encoding="utf-8") as fh:
            fh.write(_noisy(120))
        session.llm = _DigestAwareLLM([
            tool_call_response("read_file", {"path": "big.txt"}),
            tool_call_response("read_file", {"path": "big.txt"}),
            tool_call_response("read_file", {"path": "big.txt"}),
        ])
        # A budget this small evicts as soon as the tool results land.
        session.context.max_messages = max_messages
        session.context.max_chars = 6000
        session.config["context"]["digest"] = digest
        return app, session

    def test_a_digest_is_built_and_reaches_the_model(self):
        app, session = self._run()
        app.submit("do some work")
        self.assertTrue(wait_until(lambda: not app.busy, 30))
        self.assertIsNotNone(session.context.digest, "no digest was installed")
        self.assertGreater(session.llm.digest_calls, 0, "the summariser was never called")
        # The digest reaches a request, but is never written into the live
        # history it summarises away.
        self.assertTrue(
            any("DIGEST: the parser was fixed" in str(m.get("content"))
                for batch in session.llm.received for m in batch),
            "the digest never reached a request",
        )
        self.assertFalse(
            any("DIGEST: the parser was fixed" in str(m.get("content"))
                for m in session.context.messages),
            "the digest was written into the live history",
        )

    def test_evicted_turns_are_reported_in_metrics(self):
        app, session = self._run()
        app.submit("do some work")
        self.assertTrue(wait_until(lambda: not app.busy, 30))
        self.assertGreater(session.totals.get("digest_turns", 0), 0)

    def test_digest_off_keeps_eviction_lossy(self):
        app, session = self._run(digest=False)
        app.submit("do some work")
        self.assertTrue(wait_until(lambda: not app.busy, 30))
        self.assertEqual(session.llm.digest_calls, 0, "the summariser ran with the mechanism off")
        self.assertIsNone(session.context.digest)
        # The evicted turns are still detached rather than destroyed, so the
        # loss is visible in the queue; only a digest would fold them away.
        self.assertTrue(session.context.pending_evicted)

    def test_a_failed_summary_does_not_break_the_turn(self):
        from core.agent.exceptions import LLMError

        class _FailingDigest(_DigestAwareLLM):
            def chat(self, messages, tools=None, on_delta=None, **kwargs):
                first = messages[0] if messages else {}
                if len(messages) == 1 and "Summarise the conversation turns below" in str(
                    first.get("content")
                ):
                    self.digest_calls += 1
                    raise LLMError("digest endpoint down")
                return super().chat(messages, tools=tools, on_delta=on_delta)

        app, session, _backend = _make_app([])
        with open(os.path.join(session.workspace, "big.txt"), "w", encoding="utf-8") as fh:
            fh.write(_noisy(120))
        session.llm = _FailingDigest([tool_call_response("read_file", {"path": "big.txt"})] * 3)
        session.context.max_messages = 4
        session.context.max_chars = 6000
        app.submit("do some work")
        self.assertTrue(wait_until(lambda: not app.busy, 30))
        self.assertGreater(session.llm.digest_calls, 0, "the failure path was never exercised")
        self.assertIsNone(session.context.digest, "a failed summary still installed a digest")

    def test_the_digest_is_rolling_not_per_batch(self):
        # Each summary absorbs the previous one, so a turn evicted early is
        # not lost just because a later batch was summarised after it.
        app, session = self._run()
        app.submit("do some work")
        self.assertTrue(wait_until(lambda: not app.busy, 30))
        inputs = [
            str((batch[0] if batch else {}).get("content"))
            for batch in session.llm.received
            if len(batch) == 1 and "Summarise the conversation turns below" in str(
                (batch[0] if batch else {}).get("content")
            )
        ]
        self.assertGreaterEqual(len(inputs), 2, inputs)
        self.assertIn("DIGEST: the parser was fixed", inputs[1])
        self.assertIn("Previous digest", inputs[1])

    def test_the_digest_prompt_asks_for_state_to_be_carried_forward(self):
        from core.agent.loop import _DIGEST_PROMPT

        for marker in ("TODO DONE:", "TODO ADD:", "GOAL COMPLETE"):
            self.assertIn(marker, _DIGEST_PROMPT)
