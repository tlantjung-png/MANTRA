"""LLM client: SSE parsing, caps, retry semantics, keyless detection."""

from __future__ import annotations

import json

import pytest

from core.agent.exceptions import LLMError
from core.llm import KEYLESS_HOSTS, is_keyless_base_url, parse_sse_stream


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


# ---------------------------------------------------------------- rate limits

import urllib.error  # noqa: E402


def _rate_limited_client(**kwargs):
    from core.llm import OpenAICompatClient

    params = dict(
        model="test-model",
        base_url="http://llm.invalid/v1",
        api_key_env="MANTRA_TEST_429_KEY",
        stream=False,
        max_retries=1,
        rate_limit_wait=60.0,
    )
    params.update(kwargs)
    return OpenAICompatClient(**params)


def _too_many_requests(retry_after: str | None = None, detail: str = "slow down") -> urllib.error.HTTPError:
    headers = {"Retry-After": retry_after} if retry_after is not None else {}
    return urllib.error.HTTPError(
        "http://llm.invalid/v1/chat/completions", 429, "Too Many Requests", headers, None
    )


def _run_chat(client, side_effect, **chat_kwargs):
    from unittest import mock

    from core.types import LLMResponse

    ok = LLMResponse(content="recovered")
    calls = {"n": 0}
    sleeps: list[float] = []

    def flaky(body):
        calls["n"] += 1
        effect = side_effect(calls["n"]) if callable(side_effect) else side_effect
        if isinstance(effect, BaseException):
            raise effect
        return ok

    with mock.patch.dict("os.environ", {"MANTRA_TEST_429_KEY": "test-key"}):
        with mock.patch.object(client, "_request", side_effect=flaky):
            with mock.patch("core.llm.time.sleep", side_effect=sleeps.append):
                result = client.chat(
                    [{"role": "user", "content": "hi"}], **chat_kwargs
                )
    return result, calls, sleeps


def test_429_waits_out_the_limit_instead_of_failing() -> None:
    # The old behaviour spent all three retries inside the limited window
    # and then failed the turn; a rate limit is a queue, not a failure.
    client = _rate_limited_client(max_retries=1)
    waits: list[tuple[float, float]] = []

    def side(n):
        return _too_many_requests(retry_after="1") if n < 3 else None

    result, calls, sleeps = _run_chat(
        client, side, on_wait=lambda seconds, waited: waits.append((seconds, waited))
    )
    assert result.content == "recovered"
    assert calls["n"] == 3  # far past max_retries=1
    assert waits == [(1.0, 1.0), (1.0, 2.0)]  # the server's Retry-After, honoured
    assert sum(sleeps) == 2.0


def test_429_without_retry_after_backs_off_exponentially() -> None:
    client = _rate_limited_client()
    waits: list[tuple[float, float]] = []

    def side(n):
        return _too_many_requests() if n < 4 else None

    result, calls, _ = _run_chat(
        client, side, on_wait=lambda seconds, waited: waits.append((seconds, waited))
    )
    assert result.content == "recovered"
    assert [w[0] for w in waits] == [2.0, 4.0, 8.0]
    assert [w[1] for w in waits] == [2.0, 6.0, 14.0]


def test_429_backoff_is_capped() -> None:
    client = _rate_limited_client(rate_limit_wait=10_000.0)
    waits: list[float] = []

    def side(n):
        return _too_many_requests() if n < 8 else None

    _run_chat(client, side, on_wait=lambda seconds, waited: waits.append(seconds))
    assert waits == [2.0, 4.0, 8.0, 16.0, 30.0, 30.0, 30.0]


def test_429_gives_up_after_the_wait_budget() -> None:
    client = _rate_limited_client(rate_limit_wait=3.0)
    with pytest.raises(LLMError) as excinfo:
        _run_chat(client, _too_many_requests(retry_after="5"))
    assert "rate limited for more than 3s" in str(excinfo.value)


def test_429_wait_is_abortable() -> None:
    # Ctrl+C during a long wait ends the run; the operator is never held
    # hostage by a patient retry loop.
    client = _rate_limited_client(rate_limit_wait=600.0)
    abort = {"stop": False}

    def should_abort():
        return abort["stop"]

    def side(n):
        if n == 1:
            return _too_many_requests(retry_after="30")
        abort["stop"] = True  # the operator hits Ctrl+C during the wait
        return _too_many_requests(retry_after="30")

    with pytest.raises(LLMError) as excinfo:
        _run_chat(client, side, should_abort=should_abort)
    assert "aborted while waiting for the rate limit" in str(excinfo.value)


def test_429_wait_reports_progress() -> None:
    client = _rate_limited_client()
    waits: list[tuple[float, float]] = []

    def side(n):
        return _too_many_requests(retry_after="2") if n < 2 else None

    _run_chat(client, side, on_wait=lambda seconds, waited: waits.append((seconds, waited)))
    assert waits == [(2.0, 2.0)]


def test_non_429_http_errors_still_fail_after_max_retries() -> None:
    # The patient path is for rate limits only: a 500 keeps the bounded
    # retry contract it always had.
    client = _rate_limited_client(max_retries=2)
    with pytest.raises(LLMError) as excinfo:
        _run_chat(
            client,
            urllib.error.HTTPError(
                "http://llm.invalid/v1/chat/completions", 500, "Server Error", {}, None
            ),
        )
    assert "failed after 2 attempts" in str(excinfo.value)


def test_retry_after_parsing() -> None:
    from core.llm import parse_retry_after

    assert parse_retry_after("30") == 30.0
    assert parse_retry_after(" 12 ") == 12.0
    assert parse_retry_after("") is None
    assert parse_retry_after(None) is None
    assert parse_retry_after("soon") is None
    # An absurd ask is capped so one hostile header cannot hang a turn.
    assert parse_retry_after("99999") == 300.0
    # An HTTP-date in the past means retry now.
    assert parse_retry_after("Wed, 21 Oct 2015 07:28:00 GMT") == 0.0
