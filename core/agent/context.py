"""Bounded history: pins system and task, drops oldest turn first.

Eviction is normally lossy: a dropped turn is gone, and with it whatever
the model learned from it. The manager therefore *detaches* evicted turns
into a pending queue instead of discarding them, and keeps a rolling digest
of the turns already folded away. ``request_messages()`` is the list that
actually reaches the model: the live history plus the digest riding between
the pinned prefix and the work in flight.

The manager stays free of any model client. It decides *what* was lost and
holds it; whoever owns the client decides whether to pay for a summary, and
installs it with ``set_digest``.
"""

from __future__ import annotations

from typing import Any

CHARS_PER_TOKEN = 4

# A digest larger than this is truncated rather than allowed to eat the
# budget it exists to protect.
DEFAULT_DIGEST_MAX_CHARS = 4_000


def estimate_tokens(messages: list[dict[str, Any]]) -> int:
    """Estimate tokens from chars; for budgeting only."""
    # Derived from _message_size so the two size models cannot drift apart.
    return max(1, sum(_message_size(m) for m in messages) // CHARS_PER_TOKEN)


class ContextManager:
    """Owns message list and budget enforcement."""

    def __init__(self, max_messages: int = 200, max_chars: int = 240_000) -> None:
        if max_messages < 4:
            raise ValueError("max_messages must be at least 4")
        if not isinstance(max_chars, int) or max_chars < 2000:
            raise ValueError("max_chars must be an integer >= 2000")
        self.max_messages = max_messages
        self.max_chars = max_chars
        self.messages: list[dict[str, Any]] = []
        self._chars = 0
        # Rolling summary of turns evicted so far, and the turns evicted
        # since the last digest was installed.
        self._digest: str | None = None
        self.pending_evicted: list[dict[str, Any]] = []
        # Turns dropped without ever reaching a digest, because nothing
        # drained the queue. A non-zero count is a signal, not a defect:
        # a run with no summariser wired simply cannot pay for one.
        self.evicted_without_digest = 0

    def seed(self, system_prompt: str, user_task: str) -> None:
        # Enforce the same single-message cap as append(): a seeded task
        # sits below the eviction floor, so nothing else would ever
        # truncate an oversized one.
        cap = max(self.max_chars // 4, 100)
        if len(system_prompt) > cap:
            system_prompt = system_prompt[:cap] + f"\n... [truncated — single message exceeded {cap} chars]"
        if len(user_task) > cap:
            user_task = user_task[:cap] + f"\n... [truncated — single message exceeded {cap} chars]"
        self.messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_task},
        ]
        self._recount()
        self._truncate()

    def append(self, message: dict[str, Any]) -> None:
        # Truncate a single message over budget before append, reserving
        # space for the system prompt and one more turn.
        size = _message_size(message)
        if size > self.max_chars:
            truncated = dict(message)
            content = truncated.get("content")
            if isinstance(content, str) and len(content) > self.max_chars:
                # Keep at most a quarter of budget for a single message so
                # the system prompt and subsequent turns still fit.
                cap = max(self.max_chars // 4, 100)
                truncated["content"] = content[:cap] + f"\n... [truncated — single message exceeded {cap} chars]"
            elif isinstance(content, list):
                # Non-string content: cap each text block instead of
                # stringifying the whole list (block structure survives).
                cap = max(self.max_chars // 4, 100)
                blocks = []
                for block in content:
                    if (
                        isinstance(block, dict)
                        and isinstance(block.get("text"), str)
                        and len(block["text"]) > cap
                    ):
                        nb = dict(block)
                        nb["text"] = block["text"][:cap] + "\n... [truncated — block exceeded cap]"
                        blocks.append(nb)
                    else:
                        blocks.append(block)
                truncated["content"] = blocks
            elif content is not None:
                # Other non-string content: stringify and cap.
                s = str(content)
                if len(s) > self.max_chars:
                    cap = max(self.max_chars // 4, 100)
                    truncated["content"] = s[:cap] + f"\n... [truncated — single message exceeded {cap} chars]"
            # Also clamp huge tool_calls arguments
            tool_calls = truncated.get("tool_calls")
            if isinstance(tool_calls, list) and tool_calls:
                # Cap total tool_calls payload to 50% of budget (per-call clamp below)
                tc_budget = max(5000, int(self.max_chars * 0.5))
                tc_size = sum(len(str((c.get("function") or {}).get("arguments") or "")) + 32 for c in tool_calls)
                if tc_size > tc_budget:
                    # Truncate each call's arguments
                    per_call = tc_budget // max(1, len(tool_calls))
                    cap_a = max(500, per_call)
                    new_calls = []
                    for c in tool_calls:
                        nc = dict(c)
                        fn = dict(c.get("function") or {})
                        args = fn.get("arguments") or ""
                        s_args = str(args)
                        # Include the per-call overhead so a single
                        # oversized call is clamped too.
                        if len(s_args) + 32 > per_call:
                            fn["arguments"] = s_args[:cap_a] + " ... [truncated]"
                        nc["function"] = fn
                        new_calls.append(nc)
                    truncated["tool_calls"] = new_calls
            message = truncated
        self.messages.append(message)
        self._chars += _message_size(message)
        self._truncate()

    def replace_body(self, messages: list[dict[str, Any]]) -> None:
        """Keep system prompt, replace body (for compaction).

        The digest is dropped here on purpose: a compaction pass produces a
        fresh summary of the live history, which supersedes any rolling
        digest of turns that history no longer contains.
        """
        system = (
            self.messages[0]
            if self.messages and self.messages[0].get("role") == "system"
            else {"role": "system", "content": "You are a helpful assistant."}
        )
        # Ensure non-empty system content.
        if not system.get("content"):
            system = {"role": "system", "content": "You are a helpful assistant."}
        self.messages = [system] + list(messages)
        self._digest = None
        self.pending_evicted = []
        self._recount()
        self._truncate()

    # ── the rolling digest ───────────────────────────────────────

    def request_messages(self) -> list[dict[str, Any]]:
        """The message list that actually reaches the model.

        The digest rides between the pinned prefix and the live history, so
        the model reads it as background for the work in flight rather than
        as an instruction that precedes the task.
        """
        if not self._digest:
            return list(self.messages)
        head = self.messages[:2]
        return head + [self._digest_message()] + list(self.messages[2:])

    def _digest_message(self) -> dict[str, Any]:
        return {
            "role": "user",
            "content": (
                "Earlier in this session, summarised from turns that were "
                "evicted from the live history:\n" + (self._digest or "")
            ),
        }

    def _digest_size(self) -> int:
        return _message_size(self._digest_message()) if self._digest else 0

    def set_digest(self, text: str, max_chars: int = DEFAULT_DIGEST_MAX_CHARS) -> None:
        """Install a rolling summary of evicted turns."""
        summary = (text or "").strip()
        if not summary:
            self.clear_digest()
            return
        if max_chars and len(summary) > max_chars:
            summary = summary[:max_chars] + f"\n... [digest truncated at {max_chars} chars]"
        self._digest = summary
        self._recount()

    def clear_digest(self) -> None:
        if self._digest is not None:
            self._digest = None
            self._recount()

    @property
    def digest(self) -> str | None:
        return self._digest

    def take_pending_evicted(self) -> list[dict[str, Any]]:
        """Detach the turns evicted since the last call, for summarising."""
        pending = self.pending_evicted
        self.pending_evicted = []
        return pending

    def _retain_evicted(self, removed: list[dict[str, Any]]) -> None:
        """Hold evicted turns for the digest instead of dropping them."""
        kept = [m for m in removed if isinstance(m, dict)]
        if not kept:
            return
        self.pending_evicted.extend(kept)
        # Bounded by characters, not messages: a small eviction is kept so
        # it can be folded together with the next one, and the ceiling only
        # bites on a run whose loop never drains the queue at all. The
        # oldest turns go first, and are counted so the loss is visible
        # rather than silent.
        ceiling = max(4_000, self.max_chars // 2)
        while self.pending_evicted and self._pending_chars() > ceiling:
            self.pending_evicted.pop(0)
            self.evicted_without_digest += 1

    def _pending_chars(self) -> int:
        return sum(_message_size(m) for m in self.pending_evicted)

    def resync(self) -> None:
        """Recompute sizes after in-place edits."""
        self._recount()

    def enforce_budget(self) -> None:
        """Apply the budget to the current list, whatever put it there.

        Loading a saved session replaces the message list wholesale, so
        the budget must be re-applied explicitly afterwards.
        """
        self._recount()
        self._truncate()

    @property
    def tokens(self) -> int:
        return estimate_tokens(self.messages)

    @property
    def chars(self) -> int:
        return self._chars

    def _truncate(self) -> None:
        # Drop oldest turns until within message count
        while self._over_budget() and self._drop_oldest_turn():
            pass
        # Still over budget: repeatedly truncate largest messages until within
        # character budget or only system+one exchange remain.
        # Previously this recursed only once, leaving many-large-output cases
        # over budget.
        iterations = 0
        while self._over_budget() and len(self.messages) > 2 and iterations < 10:
            iterations += 1
            largest_idx = max(range(2, len(self.messages)), key=lambda i: _message_size(self.messages[i]))
            msg = self.messages[largest_idx]
            if not isinstance(msg, dict):
                if not self._drop_oldest_turn():
                    break
                continue
            content = msg.get("content")
            if isinstance(content, str) and len(content) > 500:
                # Halve each time, but keep at least 500 chars for usefulness
                # Reserve at least 1/4 of budget for system+latest turn
                target = max(500, self.max_chars // 4)
                if len(content) > target:
                    truncated = dict(msg)
                    truncated["content"] = content[:target] + "\n... [truncated — history exceeded budget]"
                    self.messages[largest_idx] = truncated
                    self._recount()
                    continue
            # If truncation didn't help, drop another turn
            if not self._drop_oldest_turn():
                break
        # The halving loop is capped, so a pathological history — many
        # mid-size messages, or oversized tool-call payloads the content
        # branch cannot shrink — could still be over budget here. Guarantee
        # the budget with unconditional oldest-turn eviction; the pinned
        # system+one-exchange floor is the only stopping point.
        while self._over_budget() and len(self.messages) > 2:
            if not self._drop_oldest_turn():
                break

    def _over_budget(self) -> bool:
        return len(self.messages) > self.max_messages or self._chars > self.max_chars

    def _drop_oldest_turn(self) -> bool:
        """Remove oldest assistant turn and its tool results."""
        # The newest assistant turn is exempt while it is still the last
        # message: its tool results are appended in a later call, and
        # evicting it now would orphan them.
        last = self.messages[-1] if self.messages else None
        newest_unanswered = isinstance(last, dict) and last.get("role") == "assistant"
        for index in range(2, len(self.messages)):
            msg = self.messages[index]
            if not isinstance(msg, dict) or msg.get("role") != "assistant":
                continue
            if newest_unanswered and index == len(self.messages) - 1:
                continue
            end = index + 1
            while end < len(self.messages) and isinstance(self.messages[end], dict) and self.messages[end].get("role") == "tool":
                end += 1
            self._retain_evicted(self.messages[index:end])
            del self.messages[index:end]
            self._recount()
            return True
        # No evictable assistant turn: drop oldest non-tool message.
        for index in range(2, len(self.messages)):
            msg = self.messages[index]
            if newest_unanswered and index == len(self.messages) - 1:
                continue
            if not isinstance(msg, dict) or msg.get("role") != "tool":
                self._retain_evicted([self.messages[index]])
                del self.messages[index]
                # Sweep all consecutive orphaned tool messages.
                while index < len(self.messages) and (
                    not isinstance(self.messages[index], dict)
                    or self.messages[index].get("role") == "tool"
                ):
                    self._retain_evicted([self.messages[index]])
                    del self.messages[index]
                self._recount()
                return True
        # Only tool messages remain: remove oldest.
        if len(self.messages) > 2 and not (newest_unanswered and len(self.messages) == 3):
            self._retain_evicted([self.messages[2]])
            del self.messages[2]
            self._recount()
            return True
        return False

    def _recount(self) -> None:
        # Recompute the char total from scratch (O(n); used after any in-place mutation).
        # The digest counts even though it is not a stored message: it is
        # sent with every request, so the budget must see it.
        self._chars = sum(_message_size(m) for m in self.messages) + self._digest_size()


def _message_size(message: Any) -> int:
    if not isinstance(message, dict):
        return 0
    content = message.get("content")
    size = len(content) if isinstance(content, str) else len(str(content or ""))
    for call in message.get("tool_calls") or []:
        if not isinstance(call, dict):
            continue
        function = call.get("function") or {}
        if not isinstance(function, dict):
            function = {}
        size += len(str(function.get("arguments") or "")) + 32
    return size
