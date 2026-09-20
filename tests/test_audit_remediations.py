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
        from core.llm import OpenAICompatClient

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
                with mock.patch("core.llm.time.sleep"):
                    return client.chat([{"role": "user", "content": "hi"}], on_delta=lambda piece: None)

    def test_incomplete_read_before_output_is_retried(self):
        import http.client


        client = self._client(max_retries=3)
        calls = {"n": 0}

        def flaky(body, on_delta):
            calls["n"] += 1
            if calls["n"] < 3:
                raise http.client.IncompleteRead(b"partial")
            from core.types import LLMResponse

            return LLMResponse(content="recovered")

        result = self._run(client, flaky)
        self.assertEqual(result.content, "recovered")
        self.assertEqual(calls["n"], 3)

    def test_incomplete_read_after_output_fails_the_turn(self):
        import http.client

        from core.agent.exceptions import LLMError

        client = self._client(max_retries=3)

        def drops_after_output(body, on_delta):
            on_delta("partial answer ")
            raise http.client.IncompleteRead(b"partial")

        with self.assertRaises(LLMError) as ctx:
            self._run(client, drops_after_output)
        self.assertIn("partial output", str(ctx.exception))

    def test_stream_missing_done_sentinel_raises(self):
        from core.agent.exceptions import LLMError
        from core.llm import parse_sse_stream

        lines = [
            "data: " + json.dumps({"choices": [{"delta": {"content": "half an answer"}}]}),
        ]
        with self.assertRaises(LLMError):
            parse_sse_stream(lines)

    def test_stream_missing_done_without_data_raises(self):
        from core.agent.exceptions import LLMError
        from core.llm import parse_sse_stream

        with self.assertRaises(LLMError):
            parse_sse_stream([": keep-alive only"])

    def test_stream_without_done_is_retryable_when_nothing_emitted(self):
        from core.agent.exceptions import LLMError
        from core.types import LLMResponse

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

        from core.llm import parse_sse_stream

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
        from core.tools.commands import RunCommandTool

        return RunCommandTool()

    def test_background_refused_without_host_root(self):
        from core.container import DockerSandbox

        tool = self._tool()
        sandbox = DockerSandbox()
        out = tool.execute(sandbox, "echo hi", timeout=5.0, background=True)
        self.assertTrue(out.startswith("ERROR:"), out)
        self.assertIn("foreground", out)

    def test_host_sandbox_allows_background(self):
        import tempfile
        import time

        from core.sandbox import LocalSandbox
        from core.tools.commands import _TASKS, _TASKS_LOCK

        tool = self._tool()
        with tempfile.TemporaryDirectory() as ws:
            sandbox = LocalSandbox(ws)
            out = tool.execute(sandbox, "echo bg-ok", timeout=10.0, background=True)
            self.assertIn("background task", out)
            # The spawned shell holds the temp dir as its cwd; wait for it
            # to exit so the TemporaryDirectory cleanup does not hit
            # WinError 32 (dir in use by another process) on Windows.
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                with _TASKS_LOCK:
                    procs = [t.get("process") for t in _TASKS.values() if t.get("process")]
                if not procs or all(p.poll() is not None for p in procs):
                    break
                time.sleep(0.05)

    def test_port_kill_rejects_garbage_pid_targets(self):
        # The port path must never signal pid 0/1 or the harness itself:
        # a listener advertising one of those pids, or no listener at
        # all, is refused before os.kill can fire.
        import subprocess
        import tempfile
        from unittest import mock

        from core.sandbox import LocalSandbox
        from core.tools.commands import KillShellTool

        port = 59999
        with tempfile.TemporaryDirectory() as ws:
            sandbox = LocalSandbox(ws)
            tool = KillShellTool()
            with mock.patch("core.tools.commands.os.kill") as kill_mock:
                for bad_pid in (0, 1, os.getpid()):
                    def fake_run(cmd, *args, pid=bad_pid, **kwargs):  # noqa: ARG001
                        if os.name == "nt":
                            # Bracketed (IPv6-style) local address: the
                            # netstat parser's regex only reads the port
                            # off a "[...]:port" column.
                            line = (
                                f"  TCP    [::1]:{port}    [::]:0    "
                                f"LISTENING    {pid}\n"
                            )
                            return subprocess.CompletedProcess(cmd, 0, stdout=line, stderr="")
                        return subprocess.CompletedProcess(cmd, 0, stdout=f"{pid}\n", stderr="")

                    with mock.patch("core.tools.commands.subprocess.run", side_effect=fake_run):
                        out = tool.execute(sandbox, port=port)
                    self.assertIn("no killable process found on port", out)
                    self.assertEqual(kill_mock.call_count, 0)

                # No listener at all: same refusal, nothing signalled.
                def empty_run(cmd, *args, **kwargs):  # noqa: ARG001
                    return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

                with mock.patch("core.tools.commands.subprocess.run", side_effect=empty_run):
                    out = tool.execute(sandbox, port=port)
                self.assertIn("no killable process found on port", out)
                self.assertEqual(kill_mock.call_count, 0)


class BoolArgumentValidationTest(unittest.TestCase):
    """Booleans must not pass as integer/number tool arguments."""

    def test_bool_rejected_for_integer(self):
        from core.agent.repairs import validate_arguments

        schema = {
            "type": "object",
            "properties": {"limit": {"type": "integer"}},
        }
        issues = validate_arguments({"limit": True}, schema)
        self.assertEqual(len(issues), 1)

    def test_bool_rejected_for_number(self):
        from core.agent.repairs import validate_arguments

        schema = {
            "type": "object",
            "properties": {"timeout": {"type": "number"}},
        }
        issues = validate_arguments({"timeout": False}, schema)
        self.assertEqual(len(issues), 1)

    def test_int_still_accepted(self):
        from core.agent.repairs import validate_arguments

        schema = {
            "type": "object",
            "properties": {"limit": {"type": "integer"}},
        }
        self.assertEqual(validate_arguments({"limit": 5}, schema), [])


class RepairArgumentsCoverageTest(unittest.TestCase):
    """Direct coverage for the argument-repair machinery in core/agent/repairs.py."""

    SCHEMA = {
        "type": "object",
        "properties": {
            "command": {"type": "string"},
            "timeout": {"type": "number"},
            "background": {"type": "boolean"},
            "items": {"type": "array"},
        },
        "required": ["command"],
    }

    def test_alias_claiming_maps_cmd_to_command(self):
        from core.agent.repairs import repair_arguments

        repaired, notes = repair_arguments("run_command", {"cmd": "git status"}, self.SCHEMA)
        self.assertEqual(repaired.get("command"), "git status")
        self.assertNotIn("cmd", repaired)
        self.assertTrue(any("aliased cmd" in n for n in notes), notes)

    def test_stale_alias_removed_when_canonical_present(self):
        from core.agent.repairs import repair_arguments

        repaired, notes = repair_arguments("run_command", {"command": "ls", "cmd": "rm -rf /"}, self.SCHEMA)
        self.assertEqual(repaired.get("command"), "ls")
        self.assertNotIn("cmd", repaired)

    def test_stringified_json_array_is_parsed(self):
        from core.agent.repairs import repair_arguments

        repaired, notes = repair_arguments("run_command", {"command": "ls", "items": '["a","b"]'}, self.SCHEMA)
        self.assertEqual(repaired["items"], ["a", "b"])
        self.assertTrue(any("parsed JSON string items" in n for n in notes), notes)

    def test_bare_string_wrapped_into_array_param(self):
        from core.agent.repairs import repair_arguments

        repaired, notes = repair_arguments("run_command", {"command": "ls", "items": "a"}, self.SCHEMA)
        self.assertEqual(repaired["items"], ["a"])

    def test_numeric_string_coerced_for_timeout(self):
        from core.agent.repairs import repair_arguments

        repaired, notes = repair_arguments("run_command", {"command": "ls", "timeout": "30"}, self.SCHEMA)
        self.assertEqual(repaired["timeout"], 30)
        self.assertTrue(any("coerced string timeout" in n for n in notes), notes)

    def test_null_optional_value_is_stripped(self):
        from core.agent.repairs import repair_arguments

        repaired, notes = repair_arguments("run_command", {"command": "ls", "timeout": None}, self.SCHEMA)
        self.assertNotIn("timeout", repaired)
        self.assertTrue(any("stripped null timeout" in n for n in notes), notes)

    def test_windows_path_escape_repair_round_trips(self):
        import json as _json

        from core.agent.repairs import repair_quoted_escapes_json_text

        raw = r'{"path": "C:\Users\arif-\mantra\x"}'
        repaired = repair_quoted_escapes_json_text(raw)
        parsed = _json.loads(repaired)
        self.assertEqual(parsed["path"], "C:\\Users\\arif-\\mantra\\x")


class ResponsesFallbackTest(unittest.TestCase):
    """The chat-to-Responses-API fallback (400/500 on chat) translates
    the payload and extracts the text."""

    def test_chat_400_falls_back_to_responses(self):
        import json as _json
        import urllib.error
        from unittest import mock

        from core.llm import OpenAICompatClient

        calls: list[str] = []
        responses_payload = {
            "output": [
                {"type": "message", "content": [{"type": "output_text", "text": "fallback reply"}]}
            ]
        }

        class _Resp:
            def __init__(self, data: bytes) -> None:
                self._data = data

            def read(self, *args, **kwargs):  # noqa: ARG001
                return self._data

            def __enter__(self) -> "_Resp":
                return self

            def __exit__(self, *args) -> bool:  # noqa: ARG001
                return False

        def _fake_urlopen(request, timeout=None):  # noqa: ARG001
            calls.append(request.full_url)
            if len(calls) == 1:
                raise urllib.error.HTTPError(request.full_url, 400, "Bad Request", {}, None)
            return _Resp(_json.dumps(responses_payload).encode("utf-8"))

        client = OpenAICompatClient(
            model="m", base_url="https://llm.test/v1", api_key_env="MANTRA_RESP_TEST_KEY",
            stream=False, max_retries=1,
        )
        with mock.patch.dict(os.environ, {"MANTRA_RESP_TEST_KEY": "sk-test"}):
            with mock.patch("urllib.request.urlopen", side_effect=_fake_urlopen):
                resp = client.chat([{"role": "user", "content": "hi"}])
        self.assertEqual(resp.content, "fallback reply")
        self.assertEqual(len(calls), 2)
        self.assertTrue(calls[0].endswith("/chat/completions"), calls[0])
        self.assertTrue(calls[1].endswith("/responses"), calls[1])


if __name__ == "__main__":
    unittest.main()
