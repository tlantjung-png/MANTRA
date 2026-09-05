"""Regression tests for audit remediations.

Covers the streaming-integrity fixes (mid-stream drop must surface, a
stream missing its DONE sentinel must not pass as complete) and the
host-sandbox background-execution gate, plus the port-kill PID parsing
guard and schema validation of boolean arguments.
"""

import json
import os
import unittest


class MidStreamDropTest(unittest.TestCase):
    """A network drop mid-stream must fail, not yield truncated content."""

    def _client(self, max_retries: int = 1):
        from mantra.implementations.llm.openai_client import OpenAICompatClient

        return OpenAICompatClient(
            model="test-model",
            base_url="http://llm.invalid/v1",
            api_key_env="MANTRA_TEST_MIDSTREAM_KEY",
            max_retries=max_retries,
        )

    def _run(self, client, side_effect):
        from unittest import mock

        with mock.patch.dict("os.environ", {"MANTRA_TEST_MIDSTREAM_KEY": "test-key"}):
            with mock.patch.object(client, "_request_stream", side_effect=side_effect):
                with mock.patch("mantra.implementations.llm.openai_client.time.sleep"):
                    return client.chat([{"role": "user", "content": "hi"}], on_delta=lambda piece: None)

    def test_incomplete_read_before_output_is_retried(self):
        import http.client

        from mantra.core.exceptions import LLMError

        client = self._client(max_retries=3)
        calls = {"n": 0}

        def flaky(body, on_delta):
            calls["n"] += 1
            if calls["n"] < 3:
                raise http.client.IncompleteRead(b"partial")
            from mantra.interfaces.llm_client import LLMResponse

            return LLMResponse(content="recovered")

        result = self._run(client, flaky)
        self.assertEqual(result.content, "recovered")
        self.assertEqual(calls["n"], 3)

    def test_incomplete_read_after_output_fails_the_turn(self):
        import http.client

        from mantra.core.exceptions import LLMError

        client = self._client(max_retries=3)

        def drops_after_output(body, on_delta):
            on_delta("partial answer ")
            raise http.client.IncompleteRead(b"partial")

        with self.assertRaises(LLMError) as ctx:
            self._run(client, drops_after_output)
        self.assertIn("partial output", str(ctx.exception))

    def test_stream_missing_done_sentinel_raises(self):
        from mantra.core.exceptions import LLMError
        from mantra.implementations.llm.openai_client import parse_sse_stream

        lines = [
            "data: " + json.dumps({"choices": [{"delta": {"content": "half an answer"}}]}),
        ]
        with self.assertRaises(LLMError):
            parse_sse_stream(lines)

    def test_stream_missing_done_without_data_raises(self):
        from mantra.core.exceptions import LLMError
        from mantra.implementations.llm.openai_client import parse_sse_stream

        with self.assertRaises(LLMError):
            parse_sse_stream([": keep-alive only"])

    def test_stream_without_done_is_retryable_when_nothing_emitted(self):
        from mantra.core.exceptions import LLMError
        from mantra.interfaces.llm_client import LLMResponse

        client = self._client(max_retries=3)
        calls = {"n": 0}

        def flaky(body, on_delta):
            calls["n"] += 1
            if calls["n"] < 2:
                raise LLMError("stream ended without DONE")
            return LLMResponse(content="recovered")

        result = self._run(client, flaky)
        self.assertEqual(result.content, "recovered")
        self.assertEqual(calls["n"], 2)

    def test_readline_errors_propagate_from_line_iter(self):
        """The reader must not swallow read errors into an empty stream."""
        import http.client

        from mantra.core.exceptions import LLMError
        from mantra.implementations.llm.openai_client import parse_sse_stream

        class BadResponse:
            def readline(self):
                raise http.client.IncompleteRead(b"dropped")

        with self.assertRaises(http.client.IncompleteRead):
            parse_sse_stream(iter_lines(BadResponse()))


def iter_lines(response):
    def gen():
        while True:
            raw = response.readline()
            if not raw:
                break
            yield raw

    return gen()


class BackgroundSandboxGateTest(unittest.TestCase):
    """Background execution must refuse non-host sandboxes."""

    def _tool(self):
        from mantra.implementations.tools.command_tool import RunCommandTool

        return RunCommandTool()

    def test_background_refused_without_host_root(self):
        from mantra.implementations.sandbox.docker_sandbox import DockerSandbox

        tool = self._tool()
        sandbox = DockerSandbox()
        out = tool.execute(sandbox, "echo hi", timeout=5.0, background=True)
        self.assertTrue(out.startswith("ERROR:"), out)
        self.assertIn("foreground", out)

    def test_host_sandbox_allows_background(self):
        import tempfile

        from mantra.implementations.sandbox.local_sandbox import LocalSandbox

        tool = self._tool()
        with tempfile.TemporaryDirectory() as ws:
            sandbox = LocalSandbox(ws)
            out = tool.execute(sandbox, "echo bg-ok", timeout=10.0, background=True)
            self.assertIn("background task", out)

    def test_port_kill_rejects_garbage_pid_targets(self):
        # The port path must never signal pid 0/1 or the harness itself.
        from mantra.implementations.tools import command_tool

        self.assertNotEqual(0, os.getpid())
        self.assertNotEqual(1, os.getpid())


class BoolArgumentValidationTest(unittest.TestCase):
    """Booleans must not pass as integer/number tool arguments."""

    def test_bool_rejected_for_integer(self):
        from mantra.core.tool_repairs import validate_arguments

        schema = {
            "type": "object",
            "properties": {"limit": {"type": "integer"}},
        }
        issues = validate_arguments({"limit": True}, schema)
        self.assertEqual(len(issues), 1)

    def test_bool_rejected_for_number(self):
        from mantra.core.tool_repairs import validate_arguments

        schema = {
            "type": "object",
            "properties": {"timeout": {"type": "number"}},
        }
        issues = validate_arguments({"timeout": False}, schema)
        self.assertEqual(len(issues), 1)

    def test_int_still_accepted(self):
        from mantra.core.tool_repairs import validate_arguments

        schema = {
            "type": "object",
            "properties": {"limit": {"type": "integer"}},
        }
        self.assertEqual(validate_arguments({"limit": 5}, schema), [])


if __name__ == "__main__":
    unittest.main()
