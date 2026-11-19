"""Flatten conversation history to plain text for the summariser."""

from __future__ import annotations

from typing import Any

_TOOL_BODY_CHARS = 300


def flatten_messages(
    messages: list[dict[str, Any]],
    *,
    skip_system: bool = False,
    tolerant: bool = False,
) -> str:
    """Render message history as labelled lines.

    ``skip_system`` drops the system prompt; ``tolerant`` copes with
    non-string content and non-dict tool-call entries.
    """
    lines: list[str] = []
    for message in messages:
        role = message.get("role", "?")
        if skip_system and role == "system":
            continue
        content = message.get("content") or ""
        if tolerant and not isinstance(content, str):
            content = str(content)
        if role == "assistant" and message.get("tool_calls"):
            calls = message["tool_calls"]
            if tolerant:
                names = ", ".join(
                    (call.get("function") or {}).get("name", "?")
                    for call in calls
                    if isinstance(call, dict)
                )
            else:
                names = ", ".join(
                    (call.get("function") or {}).get("name", "?") for call in calls
                )
            lines.append(f"assistant: [called {names}]")
            if content:
                lines.append(f"assistant: {content}")
            continue
        if role == "tool":
            body = content if len(content) <= _TOOL_BODY_CHARS else content[:_TOOL_BODY_CHARS] + " ..."
            lines.append(f"result of {message.get('name', 'tool')}: {body}")
            continue
        lines.append(f"{role}: {content}")
    return "\n".join(lines)
