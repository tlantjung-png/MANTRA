"""Mock client: replay scripted responses for offline tests."""

from __future__ import annotations

import copy
import types

from mantra.core.exceptions import LLMError
from mantra.interfaces.llm_client import LLMClient, LLMResponse, ToolCall

_call_counter = 0


class ScriptedLLMClient(LLMClient):
    """Return next queued response; raise when empty."""

    def __init__(self, script: list[LLMResponse]) -> None:
        self.script = list(script)
        self.received_messages: list[list[dict]] = []

    def chat(self, messages, tools=None, on_delta=None) -> LLMResponse:
        self.received_messages.append(copy.deepcopy(messages))
        if not self.script:
            raise LLMError("scripted LLM exhausted before the run finished")
        return self.script.pop(0)


def tool_call_response(name: str, arguments: dict) -> LLMResponse:
    """Helper to build a one-tool-call response."""
    # Module-global counter keeps tool-call ids unique across calls and tests.
    global _call_counter
    _call_counter += 1
    return LLMResponse(
        tool_calls=[ToolCall(id=f"call_{name}_{_call_counter}", name=name, arguments=arguments)]
    )


def final_response(content: str, stream: bool = False) -> LLMResponse:
    """A no-tool final answer.

    ``stream=True`` is for console tests: it returns a ``ScriptedLLMClient``
    that emits the reply word-by-word through ``on_delta`` before returning
    the same response, so the streaming render path is exercised rather
    than the whole-delivery path.
    """
    response = LLMResponse(content=content)
    if not stream:
        return response

    def chat(self, messages, tools=None, on_delta=None):
        if on_delta:
            # Word-split with a trailing space per word, so the delta
            # stream round-trips word boundaries.
            for word in content.split(" "):
                on_delta(word + " ")
        return response

    client = ScriptedLLMClient([response])
    # Replace the instance method so the streaming variant emits deltas
    # instead of replaying the queued script.
    client.chat = types.MethodType(chat, client)
    return client
