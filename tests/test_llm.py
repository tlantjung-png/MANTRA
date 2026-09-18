"""LLM client: SSE parsing, caps, retry semantics, keyless detection."""

from __future__ import annotations

import json

import pytest

from core.agent.exceptions import LLMError
from core.llm import KEYLESS_HOSTS, is_keyless_base_url, parse_sse_stream
from core.types import ToolCall


def _sse(*chunks: str, done: bool = True) -> list[str]:
    lines = []
    for c in chunks:
        lines.append(f"data: {json.dumps(c)}")
        lines.append("")
    if done:
        lines.append("data: [DONE]")
        lines.append("")
    return lines


def _sse_content(*pieces: str, done: bool = True) -> list[str]:
    lines = []
    for p in pieces:
        lines.append("data: " + json.dumps({"choices": [{"delta": {"content": p}}]}))
        lines.append("")
    if done:
        lines.append("data: [DONE]")
        lines.append("")
    return lines


def test_plain_text_stream() -> None:
    response = parse_sse_stream(_sse_content("hel", "lo"))
    assert response.content == "hello"
    assert response.tool_calls == []


def test_stream_without_done_is_rejected() -> None:
    with pytest.raises(LLMError, match="without DONE"):
        parse_sse_stream(_sse_content("hi", done=False))


def test_deltas_reach_callback() -> None:
    seen: list[str] = []
    parse_sse_stream(_sse_content("a", "b"), on_delta=seen.append)
    assert seen == ["a", "b"]


def test_observer_errors_do_not_fail_stream() -> None:
    def broken(_piece: str) -> None:
        raise RuntimeError("observer bug")

    response = parse_sse_stream(_sse_content("x"), on_delta=broken)
    assert response.content == "x"


def test_tool_call_assembled_from_fragments() -> None:
    lines = [
        "data: " + json.dumps({"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "c1", "function": {"name": "rea", "arguments": ""}}]}}]}),
        "",
        "data: " + json.dumps({"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"name": "d_file", "arguments": '{"pa'}}]}}]}),
        "",
        "data: " + json.dumps({"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": 'th": "a.txt"}'}}]}}]}),
        "",
        "data: [DONE]",
        "",
    ]
    response = parse_sse_stream(lines)
    assert len(response.tool_calls) == 1
    call = response.tool_calls[0]
    assert call.name == "read_file"
    assert call.arguments == {"path": "a.txt"}


def test_tool_call_missing_name_rejected() -> None:
    lines = [
        "data: " + json.dumps({"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "c1", "function": {"arguments": "{}"}}]}}]}),
        "",
        "data: [DONE]",
        "",
    ]
    with pytest.raises(LLMError, match="missing name"):
        parse_sse_stream(lines)


def test_truncated_tool_arguments_marked_retryable() -> None:
    lines = [
        "data: " + json.dumps({"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "c1", "function": {"name": "read_file", "arguments": '{"path": "a'}}]}}]}),
        "",
        "data: [DONE]",
        "",
    ]
    with pytest.raises(LLMError) as excinfo:
        parse_sse_stream(lines)
    assert getattr(excinfo.value, "retryable_truncation", False) is True


def test_malformed_chunks_bounded() -> None:
    lines = ["data: not-json", ""] * 30 + ["data: [DONE]", ""]
    with pytest.raises(LLMError, match="malformed"):
        parse_sse_stream(lines)


def test_keepalive_noise_tolerated() -> None:
    lines = [
        ": keep-alive",
        "",
        "data: not-json",
        "",
        *(_sse_content("ok")),
    ]
    response = parse_sse_stream(lines)
    assert response.content == "ok"


def test_content_cap_enforced() -> None:
    piece = "x" * 1000
    lines = _sse_content(*([piece] * 3000))  # 3MB > 2MB cap
    with pytest.raises(LLMError, match="cap"):
        parse_sse_stream(lines)


def test_usage_extracted_from_final_chunk() -> None:
    lines = [
        "data: " + json.dumps({"choices": [{"delta": {"content": "hi"}}]}),
        "",
        "data: " + json.dumps({"choices": [], "usage": {"prompt_tokens": 3, "completion_tokens": 2}}),
        "",
        "data: [DONE]",
        "",
    ]
    response = parse_sse_stream(lines)
    assert response.usage == {"prompt_tokens": 3, "completion_tokens": 2}


def test_keyless_detection_loopback() -> None:
    assert is_keyless_base_url("http://localhost:8000/v1") is True
    assert is_keyless_base_url("http://127.0.0.1:8000/v1") is True
    assert is_keyless_base_url("http://[::1]:8000/v1") is True
    assert is_keyless_base_url("http://my-service.localhost:8000/v1") is True


def test_keyless_detection_public_rejected() -> None:
    assert is_keyless_base_url("https://api.example.com/v1") is False
    # Substring games must not pass the exact-host check.
    assert is_keyless_base_url("https://evil-localhost.proxy.com/v1") is False


def test_keyless_hosts_tuple_shared() -> None:
    # Console re-exports the same tuple: drift between setup guidance and
    # the request path is a regression.
    from core.console_common import KEYLESS_HOSTS as CONSOLE_KEYLESS

    assert CONSOLE_KEYLESS is KEYLESS_HOSTS


def test_keyless_schemeless_base_url() -> None:
    assert is_keyless_base_url("localhost:8000/v1") is True
