"""Regression tests for the plumbing remediation pass.

Covers: the plain-HTTP key warning (B-H1), BrokenPipe containment in
safe_write (B-M2), config value-type validation (B-M4), the tool-call
arguments cap in the SSE parser (B-M7), the 404 Responses fallback
(B-L9), content normalization for reasoning models (B-M1), one-delta
fallback emission (B-H3), first-fragment tool names (B-L12), ZWJ emoji
width (B-L15), non-serializable payload errors (B-L10), and the
MANTRA_SCRIPT ConfigError wrap (B-M3).
"""

import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.agent.exceptions import LLMError
from core.config import ConfigError, merge_defaults
from core.llm import OpenAICompatClient, _normalize_content, parse_sse_stream
from core.registry import build_llm


class InsecureTransportWarnTest(unittest.TestCase):
    """B-H1: a key crossing plain HTTP to a remote host must warn."""

    def _client(self, base_url: str) -> OpenAICompatClient:
        return OpenAICompatClient(
            model="m",
            base_url=base_url,
            api_key_env="MANTRA_PLUMB_KEY",
            max_retries=1,
            stream=False,
        )

    def test_plain_http_remote_with_key_warns(self):
        from core.agent import keys as keys_module
        from core.types import LLMResponse

        client = self._client("http://llm.invalid/v1")
        client._request = lambda body: LLMResponse(content="ok")  # type: ignore[method-assign]
        with mock.patch.dict(os.environ, {"MANTRA_PLUMB_KEY": "sk-test"}):
            with mock.patch.object(keys_module, "warn_insecure_transport") as warn:
                client.chat([{"role": "user", "content": "hi"}])
        warn.assert_called_once()

    def test_loopback_with_key_does_not_warn(self):
        from core.agent import keys as keys_module
        from core.types import LLMResponse

        client = self._client("http://127.0.0.1:9/v1")
        client._request = lambda body: LLMResponse(content="ok")  # type: ignore[method-assign]
        with mock.patch.dict(os.environ, {"MANTRA_PLUMB_KEY": "sk-test"}):
            with mock.patch.object(keys_module, "warn_insecure_transport") as warn:
                client.chat([{"role": "user", "content": "hi"}])
        warn.assert_not_called()


class SafeWriteTest(unittest.TestCase):
    """B-M2/B-L14: safe_write contains BrokenPipe and avoids duplication."""

    def test_broken_pipe_is_contained(self):
        from core import term

        class _Broken:
            encoding = "utf-8"

            def write(self, text):
                raise BrokenPipeError("closed")

        with mock.patch.object(term.sys, "stdout", _Broken()):
            term.safe_write("hello")  # must not raise

    def test_unencodable_fallback_writes_only_the_remainder(self):
        from core import term

        class _Latin1:
            encoding = "latin-1"

            def __init__(self):
                self.written = []

            def write(self, text):
                if "\u2192" in text:
                    idx = text.index("\u2192")
                    self.written.append(text[:idx])
                    raise UnicodeEncodeError(
                        "latin-1", text, idx, idx + 1, "ordinal not in range(256)"
                    )
                self.written.append(text)
                return len(text)

        sink = _Latin1()
        with mock.patch.object(term.sys, "stdout", sink):
            term.safe_write("ab\u2192cd")
        self.assertEqual("".join(sink.written), "ab?cd")


class ConfigTypeValidationTest(unittest.TestCase):
    """B-M4: component-section values are type-checked at config load."""

    def test_bad_llm_model_type_rejected(self):
        with self.assertRaises(ConfigError):
            merge_defaults({"llm": {"model": None}})

    def test_bad_temperature_type_rejected(self):
        with self.assertRaises(ConfigError):
            merge_defaults({"llm": {"temperature": "hot"}})

    def test_bad_logging_path_rejected(self):
        with self.assertRaises(ConfigError):
            merge_defaults({"logging": {"path": None}})

    def test_bool_rejected_for_max_tokens(self):
        with self.assertRaises(ConfigError):
            merge_defaults({"llm": {"max_tokens": True}})

    def test_null_reasoning_effort_still_allowed(self):
        merged = merge_defaults({"llm": {"reasoning_effort": None}})
        self.assertIsNone(merged["llm"]["reasoning_effort"])

    def test_valid_values_pass(self):
        merged = merge_defaults(
            {
                "llm": {
                    "model": "m",
                    "base_url": "https://x/v1",
                    "temperature": 0.1,
                    "max_tokens": 100,
                    "stream": False,
                },
                "sandbox": {"image": "python:3.11-slim", "mem_limit": "2g", "workdir": "/w"},
                "evaluator": {"timeout": 30, "test_cmd": "echo ok"},
                "logging": {"path": "logs/x.jsonl"},
            }
        )
        self.assertEqual(merged["llm"]["model"], "m")
        self.assertEqual(merged["evaluator"]["timeout"], 30)


class ToolArgsCapTest(unittest.TestCase):
    """B-M7: accumulated tool-call arguments are byte-capped."""

    def test_args_cap_raises(self):
        def chunk(payload: str) -> str:
            return "data: " + json.dumps(
                {"choices": [{"delta": {"tool_calls": [
                    {"index": 0, "function": {"name": "run", "arguments": payload}}
                ]}}]}
            )

        lines = [
            chunk('{"path": "' + "x" * 800 + '"}'),
            chunk('"tail"'),
            "data: [DONE]",
        ]
        with mock.patch("core.llm._MAX_TOOL_ARGS_BYTES", 1000):
            with self.assertRaises(LLMError):
                parse_sse_stream(lines)

    def test_below_cap_parses_cleanly(self):
        lines = [
            "data: " + json.dumps(
                {"choices": [{"delta": {"tool_calls": [
                    {"index": 0, "function": {"name": "run", "arguments": '{"a": 1}'}}
                ]}}]}
            ),
            "data: [DONE]",
        ]
        result = parse_sse_stream(lines)
        self.assertEqual(len(result.tool_calls), 1)
        self.assertEqual(result.tool_calls[0].arguments, {"a": 1})


class ResponsesFallback404Test(unittest.TestCase):
    """B-L9: a 404 on chat completions falls back to the Responses API."""

    def _resp(self, data: bytes):
        class _Resp:
            def read(self, *args, **kwargs):  # noqa: ARG001
                return data

            def __enter__(self):
                return self

            def __exit__(self, *args) -> bool:  # noqa: ARG001
                return False

        return _Resp()

    def test_404_falls_back_to_responses(self):
        calls: list[str] = []
        payload = {
            "output": [{"type": "message", "content": [{"type": "output_text", "text": "404 fallback"}]}]
        }

        def fake_urlopen(request, timeout=None):  # noqa: ARG001
            calls.append(request.full_url)
            if len(calls) == 1:
                raise urllib.error.HTTPError(
                    request.full_url, 404, "Not Found", {}, io.BytesIO(b"not found")
                )
            return self._resp(json.dumps(payload).encode("utf-8"))

        client = OpenAICompatClient(
            model="m", base_url="https://llm.test/v1", api_key_env="MANTRA_404_KEY",
            stream=False, max_retries=1,
        )
        with mock.patch.dict(os.environ, {"MANTRA_404_KEY": "sk-test"}):
            with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
                resp = client.chat([{"role": "user", "content": "hi"}])
        self.assertEqual(resp.content, "404 fallback")
        self.assertEqual(len(calls), 2)
        self.assertTrue(calls[1].endswith("/responses"), calls)


class FallbackDeltaTest(unittest.TestCase):
    """B-H3: the streamed Responses fallback emits its text as one delta."""

    def test_stream_fallback_emits_one_delta(self):
        calls: list[str] = []
        payload = {
            "output": [{"type": "message", "content": [{"type": "output_text", "text": "fallback reply"}]}]
        }

        def fake_urlopen(request, timeout=None):  # noqa: ARG001
            calls.append(request.full_url)
            if len(calls) == 1:
                raise urllib.error.HTTPError(
                    request.full_url, 500, "err", {}, io.BytesIO(b"boom")
                )
            return FallbackDeltaTest._resp(json.dumps(payload).encode("utf-8"))

        client = OpenAICompatClient(
            model="m", base_url="https://llm.test/v1", api_key_env="MANTRA_DELTA_KEY",
            stream=True, max_retries=1,
        )
        deltas: list[str] = []
        with mock.patch.dict(os.environ, {"MANTRA_DELTA_KEY": "sk-test"}):
            with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
                resp = client.chat([{"role": "user", "content": "hi"}], on_delta=deltas.append)
        self.assertEqual(resp.content, "fallback reply")
        self.assertEqual(deltas, ["fallback reply"])

    @staticmethod
    def _resp(data: bytes):
        class _Resp:
            def read(self, *args, **kwargs):  # noqa: ARG001
                return data

            def __enter__(self):
                return self

            def __exit__(self, *args) -> bool:  # noqa: ARG001
                return False

        return _Resp()


class ContentNormalizationTest(unittest.TestCase):
    """B-M1: model content is normalized to plain text."""

    def test_list_content_is_joined(self):
        self.assertEqual(_normalize_content([{"text": "hel"}, {"text": "lo"}]), "hello")
        self.assertEqual(_normalize_content("plain"), "plain")
        self.assertIsNone(_normalize_content(None))
        self.assertEqual(_normalize_content(["a", {"text": "b"}]), "ab")

    def test_bad_content_type_rejected(self):
        with self.assertRaises(LLMError):
            _normalize_content(42)


class ToolNameFirstFragmentTest(unittest.TestCase):
    """B-L12: a repeated full name per chunk is kept once."""

    def test_name_kept_on_first_fragment(self):
        lines = [
            "data: " + json.dumps(
                {"choices": [{"delta": {"tool_calls": [
                    {"index": 0, "function": {"name": "edit_file", "arguments": ""}}
                ]}}]}
            ),
            "data: " + json.dumps(
                {"choices": [{"delta": {"tool_calls": [
                    {"index": 0, "function": {"name": "edit_file", "arguments": '{"path": "a.py"}'}}
                ]}}]}
            ),
            "data: [DONE]",
        ]
        result = parse_sse_stream(lines)
        self.assertEqual(len(result.tool_calls), 1)
        self.assertEqual(result.tool_calls[0].name, "edit_file")
        self.assertEqual(result.tool_calls[0].arguments, {"path": "a.py"})


class ZwjWidthTest(unittest.TestCase):
    """B-L15: a ZWJ emoji sequence counts as one grapheme."""

    def test_zwj_emoji_sequence_counts_once(self):
        from core.term import _WidthScanner, _char_width

        family = "\U0001f468\u200d\U0001f469\u200d\U0001f467"
        scanner = _WidthScanner()
        total = sum(scanner.feed(c) for c in family)
        self.assertEqual(total, 2)
        # The state is per-scan: a fresh scanner must not inherit the
        # previous sequence's ZWJ flag.
        self.assertEqual(_WidthScanner().feed("x"), 1)
        self.assertEqual(_char_width("x"), 1)


class SerializationErrorTest(unittest.TestCase):
    """B-L10: a non-serializable payload fails with LLMError, not a traceback."""

    def test_non_serializable_payload_raises_llm_error(self):
        client = OpenAICompatClient(
            model="m", base_url="http://127.0.0.1:9/v1", api_key_env="MANTRA_JSON_KEY",
            max_retries=1,
        )
        with mock.patch.dict(os.environ, {"MANTRA_JSON_KEY": "sk-test"}):
            with self.assertRaises(LLMError):
                client.chat([{"role": "user", "content": object()}])


class MantraScriptConfigErrorTest(unittest.TestCase):
    """B-M3: a broken MANTRA_SCRIPT file surfaces as ConfigError."""

    def test_missing_script_file_raises_config_error(self):
        bad_path = os.path.join(tempfile.mkdtemp(prefix="mantra-plumb-"), "missing.json")
        cfg = {"provider": "openai", "model": "m"}
        with mock.patch.dict(os.environ, {"MANTRA_SCRIPT": bad_path}):
            with self.assertRaises(ConfigError):
                build_llm(cfg)


class ConstructDiscriminatorTest(unittest.TestCase):
    """B-L7: only the section's own discriminator key is permitted."""

    def test_stray_type_in_llm_section_rejected(self):
        cfg = {"provider": "openai", "model": "m", "type": "junk"}
        with self.assertRaises(ConfigError):
            build_llm(cfg)


if __name__ == "__main__":
    unittest.main()
