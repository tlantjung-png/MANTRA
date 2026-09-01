"""Bounded history: pins system and task, drops oldest turn first."""

from __future__ import annotations

from typing import Any

CHARS_PER_TOKEN = 4


def estimate_tokens(messages: list[dict[str, Any]]) -> int:
    """Estimate tokens from chars; for budgeting only."""
    total = 0
    for message in messages:
        content = message.get("content")
        if isinstance(content, str):
            total += len(content)
        elif content is not None:
            total += len(str(content))
        for call in message.get("tool_calls") or []:
            function = call.get("function") or {}
            total += len(str(function.get("arguments") or "")) + 32
    return max(1, total // CHARS_PER_TOKEN)


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

    def seed(self, system_prompt: str, user_task: str) -> None:
        self.messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_task},
        ]
        self._recount()

    def append(self, message: dict[str, Any]) -> None:
        # Truncate single message over budget before append. Reserve space
        # for system prompt and at least one more turn.
        size = _message_size(message)
        if size > self.max_chars:
            truncated = dict(message)
            content = truncated.get("content")
            if isinstance(content, str) and len(content) > self.max_chars:
                # Keep at most 80% of budget for a single message so the
                # system prompt and subsequent turns still fit.
                cap = max(1000, int(self.max_chars * 0.8))
                truncated["content"] = content[:cap] + f"\n... [truncated — single message exceeded {cap} chars]"
            elif content is not None:
                # Non-string content also capped
                s = str(content)
                if len(s) > self.max_chars:
                    cap = max(1000, int(self.max_chars * 0.8))
                    truncated["content"] = s[:cap] + f"\n... [truncated — single message exceeded {cap} chars]"
            # Also clamp huge tool_calls arguments
            tool_calls = truncated.get("tool_calls")
            if isinstance(tool_calls, list) and tool_calls:
                # Cap total tool_calls payload to 50% of budget
                tc_budget = max(5000, int(self.max_chars * 0.5))
                tc_size = sum(len(str((c.get("function") or {}).get("arguments") or "")) + 32 for c in tool_calls)
                if tc_size > tc_budget:
                    # Truncate each call's arguments
                    new_calls = []
                    for c in tool_calls:
                        nc = dict(c)
                        fn = dict(c.get("function") or {})
                        args = fn.get("arguments") or ""
                        s_args = str(args)
                        if len(s_args) > tc_budget // max(1, len(tool_calls)):
                            cap_a = max(500, tc_budget // max(1, len(tool_calls)))
                            fn["arguments"] = s_args[:cap_a] + " ... [truncated]"
                        nc["function"] = fn
                        new_calls.append(nc)
                    truncated["tool_calls"] = new_calls
            message = truncated
        self.messages.append(message)
        self._chars += _message_size(message)
        self._truncate()

    def replace_body(self, messages: list[dict[str, Any]]) -> None:
        """Keep system prompt, replace body (for compaction)."""
        system = (
            self.messages[0]
            if self.messages and self.messages[0].get("role") == "system"
            else {"role": "system", "content": "You are a helpful assistant."}
        )
        # Ensure non-empty system content.
        if not system.get("content"):
            system = {"role": "system", "content": "You are a helpful assistant."}
        self.messages = [system] + list(messages)
        self._recount()
        self._truncate()

    def resync(self) -> None:
        """Recompute sizes after in-place edits."""
        self._recount()

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

    def _over_budget(self) -> bool:
        return len(self.messages) > self.max_messages or self._chars > self.max_chars

    def _drop_oldest_turn(self) -> bool:
        """Remove oldest assistant turn and its tool results."""
        for index in range(2, len(self.messages)):
            if self.messages[index].get("role") != "assistant":
                continue
            end = index + 1
            while end < len(self.messages) and self.messages[end].get("role") == "tool":
                end += 1
            del self.messages[index:end]
            self._recount()
            return True
        # No assistant turn: drop oldest non-tool message.
        for index in range(2, len(self.messages)):
            if self.messages[index].get("role") != "tool":
                del self.messages[index]
                # Sweep all consecutive orphaned tool messages.
                while index < len(self.messages) and self.messages[index].get("role") == "tool":
                    del self.messages[index]
                self._recount()
                return True
        # Only tool messages remain: remove oldest.
        if len(self.messages) > 2:
            del self.messages[2]
            self._recount()
            return True
        return False

    def _recount(self) -> None:
        self._chars = sum(_message_size(m) for m in self.messages)


def _message_size(message: dict[str, Any]) -> int:
    content = message.get("content")
    size = len(content) if isinstance(content, str) else len(str(content or ""))
    for call in message.get("tool_calls") or []:
        function = call.get("function") or {}
        size += len(str(function.get("arguments") or "")) + 32
    return size
