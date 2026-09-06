"""Registry: map config names to classes; extend without core changes."""

from __future__ import annotations

import inspect

from core.agent.exceptions import ConfigError
from core.evaluators import CommandEvaluator
from core.evaluators import NullEvaluator
from core.scripted import ScriptedLLMClient
from core.llm import OpenAICompatClient
from core.logs import JsonlLogger
from core.container import DockerSandbox
from core.sandbox import LocalSandbox
from core.tools.commands import (
    GitDiffTool,
    GitResetTool,
    KillShellTool,
    RunCommandTool,
    ShellOutputTool,
)
from core.tools.files import (
    EditFileTool,
    ListDirTool,
    ReadFileTool,
    WriteFileTool,
)
from core.tools.search import FindFileTool, SearchCodeTool
from core.tools.web import WebFetchTool
from core.types import Evaluator
from core.types import LLMClient
from core.types import Logger
from core.types import Sandbox
from core.types import Tool

LLM_REGISTRY: dict[str, type[LLMClient]] = {
    "openai": OpenAICompatClient,
    "scripted": ScriptedLLMClient,
}

SANDBOX_REGISTRY: dict[str, type[Sandbox]] = {
    "local": LocalSandbox,
    "docker": DockerSandbox,
}

EVALUATOR_REGISTRY: dict[str, type[Evaluator]] = {
    "command": CommandEvaluator,
    "none": NullEvaluator,
}

LOGGER_REGISTRY: dict[str, type[Logger]] = {
    "jsonl": JsonlLogger,
}

TOOL_REGISTRY: dict[str, type[Tool]] = {
    tool.name: tool
    for tool in (
        ReadFileTool,
        WriteFileTool,
        EditFileTool,
        ListDirTool,
        RunCommandTool,
        ShellOutputTool,
        KillShellTool,
        SearchCodeTool,
        FindFileTool,
        GitDiffTool,
        GitResetTool,
        WebFetchTool,
    )
}
# Canonical name normalization: add alias without underscore by normalizing
# incoming names at lookup time rather than duplicating entries. Keep alias
# entry for backward compat (tests index TOOL_REGISTRY directly) but also
# normalize on lookup to avoid divergence.
_TOOL_ALIASES: dict[str, str] = {"webfetch": "web_fetch"}


def _normalize_tool_name(name: str) -> str:
    n = (name or "").strip().lower().replace("-", "_")
    return _TOOL_ALIASES.get(n, n)


# Keep backward-compat alias entry so TOOL_REGISTRY["webfetch"] works for direct indexing
TOOL_REGISTRY["webfetch"] = WebFetchTool


def build_llm(config: dict) -> LLMClient:
    kind = config.get("provider")
    cls = LLM_REGISTRY.get(kind)
    if cls is None:
        raise ConfigError(f"unknown llm provider '{kind}' (known: {sorted(LLM_REGISTRY)})")
    return _construct(cls, config)


def build_sandbox(config: dict) -> Sandbox:
    kind = config.get("provider", "local")
    cls = SANDBOX_REGISTRY.get(kind)
    if cls is None:
        raise ConfigError(f"unknown sandbox provider '{kind}' (known: {sorted(SANDBOX_REGISTRY)})")
    return _construct(cls, config)


def build_evaluator(config: dict) -> Evaluator:
    kind = config.get("type", "command")
    cls = EVALUATOR_REGISTRY.get(kind)
    if cls is None:
        raise ConfigError(f"unknown evaluator '{kind}' (known: {sorted(EVALUATOR_REGISTRY)})")
    return _construct(cls, config)


def build_logger(config: dict) -> Logger:
    kind = config.get("type", "jsonl")
    cls = LOGGER_REGISTRY.get(kind)
    if cls is None:
        raise ConfigError(f"unknown logger '{kind}' (known: {sorted(LOGGER_REGISTRY)})")
    return _construct(cls, config)


def build_tools(names: list[str]) -> list[Tool]:
    """Create tools by name; share one EditLedger; dedupe aliases."""
    from core.tools.ledger import EditLedger

    ledger = EditLedger()
    seen_classes: set[type[Tool]] = set()
    tools = []
    for name in names:
        norm = _normalize_tool_name(name)
        cls = TOOL_REGISTRY.get(norm)
        if cls is None:
            raise ConfigError(
                f"unknown tool '{name}' (known: {sorted(TOOL_REGISTRY)})"
            )
        if cls in seen_classes:
            continue
        seen_classes.add(cls)
        tool = cls()
        if hasattr(tool, "ledger"):
            tool.ledger = ledger
        tools.append(tool)
    return tools


def _construct(cls, config: dict):
    """Build component; forward only matching params, reject unknown keys."""
    params = _constructor_params(cls)
    # Known keys are constructor params plus the discriminant keys
    known = set(params.keys()) | {"provider", "type"}
    unknown = [k for k in config.keys() if k not in known]
    if unknown:
        raise ConfigError(
            f"{cls.__name__} received unknown config keys {sorted(unknown)} "
            f"(known: {sorted(known)}) — check for typos"
        )
    kwargs = {}
    for key, value in config.items():
        if key in ("provider", "type"):
            continue
        if key in params:
            kwargs[key] = value
    missing_required = {
        name for name, param in params.items()
        if param.default is param.empty
        and param.kind not in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)
        and name not in kwargs
        and not name.startswith("_")
    }
    if missing_required:
        raise ConfigError(
            f"{cls.__name__} requires parameters not present in config: "
            f"{sorted(missing_required)}"
        )
    return cls(**kwargs)


def _constructor_params(cls) -> dict[str, inspect.Parameter]:
    return {
        name: param
        for name, param in inspect.signature(cls.__init__).parameters.items()
        if name != "self"
    }
