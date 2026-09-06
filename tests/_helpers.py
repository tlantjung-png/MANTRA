"""Shared helpers for the test suite (not collected as tests).

``make_session``/``make_config`` build a console session against a
workspace; ``messages`` builds a canned conversation for session-store
tests. They live here so no test module re-implements them or imports
another test module for its helpers.
"""

from __future__ import annotations

import os

from core.config import merge_defaults
from core.console import ConsoleSession, Style
from core.scripted import ScriptedLLMClient


def make_config(workspace: str, **overrides) -> dict:
    config = merge_defaults({})
    config["logging"] = {"type": "jsonl", "path": os.path.join(workspace, "session.jsonl")}
    config["approvals"] = "auto"
    config["auto_compact_tokens"] = 0  # disable mid-turn compaction here
    config.update(overrides)
    return config


def make_session(workspace: str, script: list, **overrides) -> ConsoleSession:
    # A script entry may itself be a ScriptedLLMClient (e.g. the streaming
    # helper returns one); use it directly rather than nesting it.
    llm = ScriptedLLMClient(script)
    if isinstance(script, ScriptedLLMClient):
        llm = script
    elif script and isinstance(script[0], ScriptedLLMClient):
        llm = script[0]
    return ConsoleSession(
        config=make_config(workspace, **overrides),
        workspace=workspace,
        style=Style(enabled=False),
        llm=llm,
        ask=lambda prompt: "y",  # every approval auto-answered yes
    )


def messages(count=2):
    out = [{"role": "system", "content": "you are MANTRA"}]
    for i in range(count):
        out.append({"role": "user", "content": f"question {i}"})
        out.append({"role": "assistant", "content": f"answer {i}"})
    return out
