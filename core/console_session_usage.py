# Split out of core/console.py; import it from core.console, never from here.

"""Usage accounting and context compaction on the session: per-turn
token totals, the /cost figures, the memory log entry, and the
summarise-and-replace compaction pass."""

from __future__ import annotations

import time

from core.agent.knowledge import (
    append_memory,
    plan_memory_write,
    read_raw_tail,
    rewrite_memory,
)
from core.agent.exceptions import HarnessError
from core.agent.observations import DEFAULT_MAX_CHARS
from core.console_common import _short

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # Type-only: console.py imports this module back, so a runtime import
    # here would be circular. ruff F821 is otherwise raised on every
    # forward reference in signatures.
    from core.agent.loop import RunResult
    from core.console import ConsoleSession


def _format_elapsed(seconds: float) -> str:
    """Format elapsed time: '1.6s', 'done in 1m23s', 'done in 1h05m'."""
    if seconds < 60:
        return f"{seconds:.1f}s"
    if seconds < 3600:
        m = int(seconds) // 60
        s = int(seconds) % 60
        return f"done in {m}m{s:02d}s"
    h = int(seconds) // 3600
    m = (int(seconds) % 3600) // 60
    return f"done in {h}h{m:02d}m"


def _transcript(messages: list[dict]) -> str:
    """Flatten history to plain text for the summariser."""
    lines = []
    for message in messages:
        role = message.get("role", "?")
        content = message.get("content") or ""
        if role == "system":
            continue
        if role == "assistant" and message.get("tool_calls"):
            names = ", ".join(
                (call.get("function") or {}).get("name", "?")
                for call in message["tool_calls"]
            )
            lines.append(f"assistant: [called {names}]")
            if content:
                lines.append(f"assistant: {content}")
            continue
        if role == "tool":
            body = content if len(content) <= 300 else content[:300] + " ..."
            lines.append(f"result of {message.get('name', 'tool')}: {body}")
            continue
        lines.append(f"{role}: {content}")
    return "\n".join(lines)


class UsageMixin:
    """Token accounting, memory records, and compaction on a ConsoleSession."""

    def _record_usage(self: "ConsoleSession", result: "RunResult") -> None:
        self.totals["turns"] += 1
        tin = int(result.metrics.get("tokens_in", 0))
        tout = int(result.metrics.get("tokens_out", 0))
        cache = int(result.metrics.get("cache_hit", 0))
        # Fallback to estimated tokens when provider gave no usage or all zeros.
        # This keeps /cost and the top-bar CACHE useful even when the
        # endpoint does not return usage (e.g. after stream_options downgrade).
        if tin == 0 and tout == 0:
            est_in = self.context.tokens
            est_out = max(1, len(result.final_message) // 4) if result.final_message else 0
            # If cache was reported but prompt was 0, use cache as at least part of input
            if cache > 0 and est_in == 0:
                est_in = cache
            # Only estimate if we have something to estimate from
            if est_in > 0 or est_out > 0:
                if tin == 0:
                    tin = est_in
                    result.metrics["tokens_in"] = tin
                if tout == 0:
                    tout = est_out
                    result.metrics["tokens_out"] = tout
                result.metrics["usage_estimated"] = 1
        # Cache-hit tokens also ride inside tokens_in when the provider
        # reports them, and they are re-counted in cache_hit below; the
        # input total keeps tin as reported so the same tokens are not
        # counted twice in the /cost figures.
        self.totals["tokens_in"] += tin
        self.totals["tokens_out"] += tout
        self.totals["tool_errors"] += int(result.metrics.get("tool_errors", 0))
        self.totals["cache_hit"] += cache
        # Session-wide observation saving, so /cost can report the total
        # across turns rather than only the turn that just finished.
        self.totals["observation_saved"] = (
            self.totals.get("observation_saved", 0)
            + int(result.metrics.get("observation_chars_saved", 0))
        )
        # Digest accounting: how many evictions were folded away and how
        # many summariser calls failed, so /cost can report both.
        self.totals["digest_turns"] = (
            self.totals.get("digest_turns", 0) + int(result.metrics.get("digest_turns", 0))
        )
        self.totals["digest_chars"] = (
            self.totals.get("digest_chars", 0) + int(result.metrics.get("digest_chars", 0))
        )
        self.totals["digest_failures"] = (
            self.totals.get("digest_failures", 0) + int(result.metrics.get("digest_failures", 0))
        )
        # Record per-turn metrics for trend analysis.
        rate = min(100, cache * 100 // tin) if tin > 0 else 0
        self.turn_history.append({
            "turn": self.totals["turns"],
            "tokens_in": tin,
            "tokens_out": tout,
            "cache_hit": cache,
            "cache_rate": rate,
        })
        # Wire CACHE instantly — top bar shows hit rate without waiting for next chrome redraw
        if self.layout is not None and getattr(self.layout, "active", False):
            try:
                self.layout.draw_chrome()
            except Exception:
                pass  # duck-typed bridge: chrome redraw is cosmetic, never fatal

    def _usage_line(self: "ConsoleSession", result: "RunResult") -> str:
        tin = int(result.metrics.get("tokens_in", 0))
        tout = int(result.metrics.get("tokens_out", 0))
        cache = int(result.metrics.get("cache_hit", 0))
        steps = result.steps_used
        elapsed = time.monotonic() - self._turn_started
        elapsed_str = _format_elapsed(elapsed)
        cache_bit = f" · {_short(cache)} CACHED" if cache else ""
        # Observation reshaping: what the dense context copy removed this
        # turn. Shown only when it removed something, so a turn whose output
        # already fit is not decorated with a zero.
        saved = int(result.metrics.get("observation_chars_saved", 0))
        obs_bit = f" · {_short(saved)} OBS SAVED" if saved > 0 else ""
        if not tin and not tout:
            return f"{elapsed_str} · {steps} STEP{cache_bit}{obs_bit} · CTX {_short(self.context.tokens)}"
        return (
            f"{elapsed_str} · {steps} STEP · "
            f"I/O {_short(tin)} / {_short(tout)}{cache_bit}{obs_bit} · CTX {_short(self.context.tokens)}"
        )

    def _record_memory(self: "ConsoleSession", task: dict, result: "RunResult") -> None:
        final = (result.final_message or "").strip().replace("\n", " ")[:300]
        entry = (
            f"- {time.strftime('%Y-%m-%d %H:%M')} | {task['task_id']} | "
            f"{result.stopped_reason}: {final} | status=active"
        )
        # Search-before-write: a near-duplicate is skipped entirely, and
        # a new entry on a topic already covered marks the old one
        # superseded instead of stacking copies (memory rot).
        existing = read_raw_tail(self.memory_path)
        action, updated = plan_memory_write(existing, entry)
        if action == "skip":
            return
        if action == "supersede":
            rewrite_memory(self.memory_path, updated, new_entry=entry)
            return
        ok = append_memory(
            self.memory_path,
            entry,
        )
        if not ok:
            self._print("(memory write skipped: store busy or unwritable)")

    def _report_changes(self: "ConsoleSession") -> None:
        """Announce files the agent touched, newest first, once each."""
        changed = getattr(self.sandbox, "changed", set()) or set()
        fresh = sorted(changed - self.reported_changes)
        if not fresh:
            return
        self.reported_changes.update(fresh)
        shown = fresh[:8]
        more = "" if len(fresh) <= 8 else f" (+{len(fresh) - 8} more)"
        self._print(f"  {self.style.dim('changed: ' + ', '.join(shown) + more)}")

    # ---- context management ---------------------------------------------

    def _observation_max_chars(self: "ConsoleSession") -> int:
        """Per-observation ceiling for this session; 0 disables reshaping.

        Read per turn rather than cached at startup so a config reload (or a
        test that rewrites the section) takes effect on the next turn.
        """
        section = self.config.get("observations") or {}
        if not isinstance(section, dict) or not section.get("reshape", True):
            return 0
        value = section.get("max_chars", DEFAULT_MAX_CHARS)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return DEFAULT_MAX_CHARS
        return value

    def _auto_compact(self: "ConsoleSession") -> None:
        # merge_defaults rejects non-integer values, but stay defensive:
        # a bad value must disable compaction, not crash the first turn.
        raw = self.config.get("auto_compact_tokens", 0) or 0
        limit = raw if isinstance(raw, int) and not isinstance(raw, bool) and raw > 0 else 0
        if limit and self.context.tokens > limit:
            self._print(self.style.dim(f"  (context ~{self.context.tokens} tokens, compacting)"))
            self.compact()

    def compact(self: "ConsoleSession") -> bool:
        """Summarise the conversation, keeping the system prompt and summary."""
        if len(self.context.messages) <= 3:
            return False
        # request_messages() rather than messages: the rolling digest of
        # already-evicted turns rides along in what the model sees, so a
        # compaction that ignored it would throw that knowledge away and
        # replace it with a summary of a history that no longer contains it.
        transcript = _transcript(self.context.request_messages())
        request = (
            "Summarise this coding session so work can continue without the "
            "original transcript. Cover: the goal, every file created or "
            "modified and why, commands that were run and their outcome, any "
            "error encountered and how it was resolved, and the exact state "
            "left off at. Be dense and concrete; no preamble.\n\n"
            f"{transcript}"
        )
        try:
            response = self.llm.chat(
                [{"role": "user", "content": request}], tools=None, on_delta=None
            )
        except HarnessError as exc:
            self._print(self.style.ember(f"  compaction failed: {exc}"))
            return False
        summary = (response.content or "").strip()
        if not summary:
            return False
        before = self.context.tokens
        self.context.replace_body(
            [
                {
                    "role": "user",
                    "content": "Earlier in this session (compressed summary):\n" + summary,
                }
            ]
        )
        self._print(
            self.style.dim(f"  compacted: ~{before} -> ~{self.context.tokens} tokens")
        )
        return True
