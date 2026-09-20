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


# ── the rolling digest ────────────────────────────────────────────────────
#
# Eviction used to be lossy: a dropped turn was gone. The manager now
# detaches evicted turns into a pending queue and keeps a rolling digest of
# the ones already folded away, so the loss can be repaired by whoever owns
# the model client.

def test_evicted_turns_are_retained_not_dropped():
    ctx = ContextManager(max_messages=8, max_chars=100000)
    ctx.seed("system", "task")
    for i in range(20):
        ctx.append({"role": "assistant", "content": f"turn {i}"})
        ctx.append({"role": "tool", "name": "read_file", "content": f"result {i}"})
    pending = ctx.take_pending_evicted()
    assert pending, "evicted turns were dropped outright"
    # Small turns are all kept: the queue is bounded by characters, so a
    # little at a time accumulates until it is worth folding. Nothing was
    # dropped, so even the first turn is still there to be folded.
    assert ctx.evicted_without_digest == 0
    assert any("turn 0" in str(m.get("content")) for m in pending), pending[:3]


def test_take_pending_clears_the_queue():
    ctx = ContextManager(max_messages=8, max_chars=100000)
    ctx.seed("system", "task")
    for i in range(20):
        ctx.append({"role": "assistant", "content": f"turn {i}"})
    assert ctx.take_pending_evicted()
    assert ctx.take_pending_evicted() == []


def test_digest_rides_between_the_prefix_and_the_history():
    ctx = ContextManager(max_messages=8, max_chars=100000)
    ctx.seed("system prompt", "the task")
    ctx.append({"role": "assistant", "content": "live work"})
    ctx.set_digest("earlier: fixed the parser")
    request = ctx.request_messages()
    assert request[0]["content"] == "system prompt"
    assert request[1]["content"] == "the task"
    assert "earlier: fixed the parser" in request[2]["content"]
    assert request[3]["content"] == "live work"


def test_no_digest_means_the_request_is_the_history():
    ctx = ContextManager()
    ctx.seed("system", "task")
    assert ctx.request_messages() == ctx.messages
    assert ctx.digest is None


def test_digest_counts_against_the_budget():
    ctx = ContextManager(max_messages=50, max_chars=100000)
    ctx.seed("system", "task")
    before = ctx.chars
    ctx.set_digest("x" * 5000)
    assert ctx.chars > before
    ctx.clear_digest()
    assert ctx.chars == before


def test_digest_is_capped():
    ctx = ContextManager()
    ctx.set_digest("y" * 9000, max_chars=1000)
    assert len(ctx.digest) <= 1100
    assert "digest truncated" in ctx.digest


def test_an_empty_summary_clears_rather_than_installing_nothing():
    ctx = ContextManager()
    ctx.set_digest("real")
    ctx.set_digest("   ")
    assert ctx.digest is None


def test_replace_body_drops_the_digest():
    # A compaction pass summarises the live history; the rolling digest of
    # turns that history no longer holds is superseded, not merged.
    ctx = ContextManager()
    ctx.seed("system", "task")
    ctx.set_digest("stale summary")
    ctx.replace_body([{"role": "user", "content": "fresh summary"}])
    assert ctx.digest is None
    assert ctx.take_pending_evicted() == []


def test_pending_queue_is_bounded_by_characters_and_the_loss_is_counted():
    ctx = ContextManager(max_messages=6, max_chars=100000)
    ctx.seed("system", "task")
    for i in range(60):
        # Big enough that the character ceiling bites well before the run
        # ends: nothing ever drains the queue in this test.
        ctx.append({"role": "assistant", "content": f"turn {i} " + "z" * 2000})
    assert ctx._pending_chars() <= max(4_000, ctx.max_chars // 2) + 2100
    assert ctx.evicted_without_digest > 0, "the overflow was lost without a count"


def test_budget_is_still_enforced_with_a_digest():
    # The digest is context too: a run that keeps evicting must not be able
    # to grow without bound because its digest keeps growing.
    ctx = ContextManager(max_messages=10, max_chars=20000)
    ctx.seed("system", "task")
    for i in range(200):
        ctx.append({"role": "assistant", "content": f"turn {i} " + "z" * 200})
        if i % 5 == 0:
            ctx.set_digest("summary " + "s" * 200)
    assert ctx.chars <= ctx.max_chars + 5000
    assert len(ctx.messages) <= ctx.max_messages
