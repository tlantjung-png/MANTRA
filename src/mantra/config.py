"""Config loading: JSON native, YAML optional. No secrets on disk."""

from __future__ import annotations

import copy
import json
import os

from mantra.core.exceptions import ConfigError

# Reasoning efforts; null omits the field.
REASONING_EFFORTS = ("minimal", "low", "medium", "high", "xhigh")

DEFAULTS = {
    "system_prompt": None,  # loop default if unset
    "max_steps": 30,
    # Message budget lives under context only.
    "llm": {
        "provider": "openai",
        "model": "gpt-4o",
        "api_key_env": "OPENAI_API_KEY",
        # Null for non-reasoning endpoints.
        "reasoning_effort": None,
    },
    "sandbox": {"provider": "local"},
    "tools": [
        "read_file",
        "write_file",
        "edit_file",
        "list_dir",
        "run_command",
        "shell_output",
        "kill_shell",
        "search_code",
        "find_file",
        "git_diff",
        "git_reset",
        "web_fetch",
    ],
    "evaluator": {"type": "command", "test_cmd": "python -m pytest tests/ -q"},
    "logging": {"type": "jsonl", "path": "logs/mantra-run.jsonl"},
    # Strictest approval for interactive use.
    "approvals": "default",  # default | auto | yolo | plan
    "context": {"max_messages": 200, "max_chars": 240000},
    "auto_compact_tokens": 60000,  # compact when history exceeds; 0 disables
    "verbose": False,  # echo truncated tool output live
    # True uses native scrollback; false uses fixed frame.
    "native_scrollback": True,
    "skills": {
        # Auto-attach skill per turn; bundles auto-launch by default.
        "auto": True,
        "auto_bundle": True,
    },
}


_MAX_CONFIG_BYTES = 1_000_000


def load_config(path: str) -> dict:
    """Load a config file and merge it deeply over the defaults."""
    if not os.path.isfile(path):
        raise ConfigError(f"config file not found: {path}")
    try:
        size = os.path.getsize(path)
        if size > _MAX_CONFIG_BYTES:
            raise ConfigError(f"config file too large ({size} bytes, limit {_MAX_CONFIG_BYTES})")
    except OSError:
        pass
    with open(path, "r", encoding="utf-8") as handle:
        raw = handle.read(_MAX_CONFIG_BYTES + 1)
    if len(raw) > _MAX_CONFIG_BYTES:
        raise ConfigError(f"config file too large (exceeds {_MAX_CONFIG_BYTES} bytes)")
    # Empty file is valid: treat as empty config for consistent behavior.
    if not raw.strip():
        data = {}
    elif path.lower().endswith((".yaml", ".yml")):
        data = _load_yaml(raw)
    else:
        data = _load_json(raw)
    return merge_defaults(data)


def _deep_merge(base: dict, incoming: dict) -> dict:
    out = dict(base)
    for key, value in incoming.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value) if isinstance(value, (dict, list)) else value
    return out


def merge_defaults(data: dict) -> dict:
    # Deep copy prevents mutation of shared DEFAULTS.
    merged = copy.deepcopy(DEFAULTS)
    for key, value in (data or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value) if isinstance(value, (dict, list)) else value
    # Validate required sections; defaults always supply keys.
    required = ("llm", "sandbox", "evaluator")
    wrong_type = [
        key for key in required if not isinstance(merged.get(key), dict)
    ]
    if wrong_type:
        raise ConfigError(
            f"config sections must be objects: {', '.join(wrong_type)}"
        )
    tools = merged.get("tools")
    if not isinstance(tools, list) or not tools:
        raise ConfigError("config must list at least one tool")
    _sentinel = object()
    bad_tool = next((t for t in tools if not isinstance(t, str) or not t.strip()), _sentinel)
    if bad_tool is not _sentinel:
        raise ConfigError(f"config tools must be non-empty names, got {bad_tool!r}")
    steps = merged.get("max_steps", 30)
    if not isinstance(steps, int) or isinstance(steps, bool) or steps < 1:
        raise ConfigError(f"config max_steps must be a positive integer, got {steps!r}")
    mode = merged.get("approvals", "default")
    if mode not in ("default", "auto", "yolo", "plan"):
        raise ConfigError(
            f"config approvals must be one of default/auto/yolo/plan, got '{mode}'"
        )
    effort = merged.get("llm", {}).get("reasoning_effort")
    if effort is not None and effort not in REASONING_EFFORTS:
        raise ConfigError(
            f"config llm.reasoning_effort must be one of "
            f"{'/'.join(REASONING_EFFORTS)} or null, got '{effort}'"
        )
    ctx = merged.get("context")
    if ctx is not None and not isinstance(ctx, dict):
        raise ConfigError("config context must be an object")
    if isinstance(ctx, dict):
        mm = ctx.get("max_messages")
        if mm is not None and (not isinstance(mm, int) or isinstance(mm, bool) or mm < 4):
            raise ConfigError(f"config context.max_messages must be an integer >=4, got {mm!r}")
        mc = ctx.get("max_chars")
        if mc is not None and (not isinstance(mc, int) or isinstance(mc, bool) or mc < 2000):
            raise ConfigError(f"config context.max_chars must be an integer >=2000, got {mc!r}")
    return merged


def _load_json(raw: str) -> dict:
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"invalid JSON config: {exc}") from exc


def _load_yaml(raw: str) -> dict:
    try:
        import yaml
    except ImportError as exc:
        raise ConfigError(
            "YAML config requires PyYAML; use a JSON config file instead"
        ) from exc
    try:
        return yaml.safe_load(raw) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML config: {exc}") from exc
