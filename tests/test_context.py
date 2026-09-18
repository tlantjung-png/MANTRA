"""Context budgeting: message caps, eviction, truncation, size accounting."""

from __future__ import annotations

import pytest

from core.agent.context import ContextManager, estimate_tokens


def test_rejects_invalid_limits() -> None:
    with pytest.raises(ValueError):
        ContextManager(max_messages=3)
    with pytest.raises(ValueError):
        ContextManager(max_chars=10)


def test_seed_pins_system_and_task() -> None:
    ctx = ContextManager()
    ctx.seed("sys", "task")
    assert ctx.messages[0]["role"] == "system"
    assert ctx.messages[1]["content"] == "task"


def test_append_grows_and_counts() -> None:
    ctx = ContextManager()
    ctx.seed("sys", "task")
    before = ctx.chars
    ctx.append({"role": "assistant", "content": "reply"})
    assert ctx.chars > before
    assert len(ctx.messages) == 3


def test_max_messages_eviction_drops_oldest_turn() -> None:
    ctx = ContextManager(max_messages=6)
    ctx.seed("sys", "task")
    for i in range(10):
        ctx.append({"role": "assistant", "content": f"m{i}"})
        ctx.append({"role": "user", "content": f"u{i}"})
    assert len(ctx.messages) <= 6
    # Newest content survives eviction.
    assert any("u9" in str(m.get("content")) for m in ctx.messages)


def test_single_oversized_message_truncated_on_append() -> None:
    ctx = ContextManager(max_chars=240_000)
    ctx.seed("sys", "task")
    ctx.append({"role": "assistant", "content": "x" * 300_000})
    size = estimate_tokens(ctx.messages) * 4
    assert size <= ctx.max_chars * 4  # rough bound: never grossly over budget
    assert any("truncated" in str(m.get("content")) for m in ctx.messages)


def test_seeded_task_truncated_when_oversized() -> None:
    ctx = ContextManager(max_chars=240_000)
    ctx.seed("sys", "x" * 500_000)
    assert "truncated" in ctx.messages[1]["content"]


def test_system_prompt_never_evicted() -> None:
    ctx = ContextManager(max_messages=6, max_chars=240_000)
    ctx.seed("SYSTEM PROMPT", "task")
    for i in range(20):
        ctx.append({"role": "assistant", "content": f"m{i}"})
    assert ctx.messages[0]["role"] == "system"
    assert ctx.messages[0]["content"] == "SYSTEM PROMPT"


def test_replace_body_keeps_system() -> None:
    ctx = ContextManager()
    ctx.seed("the system", "task")
    ctx.replace_body([{"role": "user", "content": "summarized"}])
    assert ctx.messages[0]["content"] == "the system"
    assert ctx.messages[1]["content"] == "summarized"


def test_replace_body_defaults_empty_system() -> None:
    ctx = ContextManager()
    ctx.replace_body([{"role": "user", "content": "hi"}])
    assert ctx.messages[0]["role"] == "system"
    assert ctx.messages[0]["content"]


def test_enforce_budget_after_wholesale_load() -> None:
    ctx = ContextManager(max_messages=6)
    ctx.seed("sys", "task")
    # Simulate a session load that bypassed append().
    ctx.messages.extend({"role": "assistant", "content": f"m{i}"} for i in range(50))
    ctx.enforce_budget()
    assert len(ctx.messages) <= 6


def test_tokens_property_positive() -> None:
    ctx = ContextManager()
    ctx.seed("sys", "task body")
    assert ctx.tokens >= 1


def test_tool_call_arguments_counted_in_size() -> None:
    small = {"role": "assistant", "content": "", "tool_calls": []}
    big = {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {"id": "c", "type": "function", "function": {"name": "f", "arguments": "x" * 5000}}
        ],
    }
    ctx = ContextManager()
    ctx.seed("sys", "task")
    base = estimate_tokens(ctx.messages)
    ctx.append(small)
    with_small = estimate_tokens(ctx.messages)
    ctx.append(big)
    with_big = estimate_tokens(ctx.messages)
    assert with_big - with_small > (with_small - base) * 5


def test_newest_unanswered_assistant_not_evicted() -> None:
    # A trailing assistant turn must survive eviction so its tool results
    # (appended next) are not orphaned.
    ctx = ContextManager(max_messages=6)
    ctx.seed("sys", "task")
    for i in range(5):
        ctx.append({"role": "assistant", "content": f"m{i}"})
    assert ctx.messages[-1]["role"] == "assistant"
