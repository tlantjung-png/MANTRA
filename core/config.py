"""Config loading: JSON native, YAML optional. No secrets on disk."""

from __future__ import annotations

import copy
import json
import os

from core.agent.exceptions import ConfigError

# Reasoning efforts; null omits the field. Non-standard values may be
# rejected by some servers and are shed on a 400 by the client's
# downgrade path.
REASONING_EFFORTS = ("minimal", "low", "medium", "high", "xhigh")

DEFAULTS = {
    "system_prompt": None,  # falls back to the loop default
    "max_steps": 30,
    # Message limits (not tokens) live under "context".
    "llm": {
        "provider": "openai",
        "model": "gpt-4o",
        "api_key_env": "OPENAI_API_KEY",
        # Null for non-reasoning endpoints.
        "reasoning_effort": None,
    },
    "sandbox": {"provider": "local"},
    # External MCP servers bridged into the tool list. Empty by default:
    # nothing is launched unless the operator names a server here.
    "mcp": {"servers": {}},
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
        "extract_document",
        "query_tree",
        "git_diff",
        "git_reset",
        "web_fetch",
    ],
    "evaluator": {"type": "command", "test_cmd": "python -m pytest tests/ -q"},
    "logging": {"type": "jsonl", "path": "logs/mantra-run.jsonl"},
    # Default approval is yolo: every tool call is allowed without a prompt.
    # Only use this default in a disposable or otherwise trusted workspace;
    # set "default", "auto", or "plan" to be prompted or to restrict writes.
    "approvals": "yolo",  # yolo | default | auto | plan
    "context": {
        "max_messages": 200,
        "max_chars": 240000,
        # Rolling digest of turns the budget evicts, so eviction stops
        # being a silent loss. Off keeps the old lossy behaviour.
        "digest": True,
        "digest_max_chars": 4000,
    },
    # Observation reshaping: tool output is densified before it enters the
    # model's context. The operator still sees the raw output and /fix still
    # captures it; only the model's copy is reshaped. max_chars is the
    # ceiling for one observation (0 disables reshaping entirely).
    "observations": {"reshape": True, "max_chars": 12000},
    "auto_compact_tokens": 60000,  # compact when history exceeds; 0 disables
    "verbose": False,  # echo truncated tool output live
    # Post-task suggestion chips ("run the tests", "commit", ...) shown
    # after a finished turn; False removes the row entirely.
    "suggestions": True,
    "skills": {
        # Auto-attach skill per turn; bundles auto-launch by default.
        "auto": True,
        "auto_bundle": True,
    },
}


_MAX_CONFIG_BYTES = 1_000_000


def load_config(path: str) -> dict:
    """Load a config file and merge it deeply over the defaults."""
    # Accept path-like objects: os.path calls below require a string, and
    # a raw AttributeError would bury the real "bad config" diagnosis.
    path = os.fspath(path)
    if not os.path.isfile(path):
        raise ConfigError(f"config file not found: {path}")
    try:
        size = os.path.getsize(path)
        if size > _MAX_CONFIG_BYTES:
            raise ConfigError(f"config file too large ({size} bytes, limit {_MAX_CONFIG_BYTES})")
    except OSError:
        pass
    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw = handle.read(_MAX_CONFIG_BYTES + 1)
    except OSError as exc:
        # The isfile check above can race a concurrent deletion (or a
        # permission change); surface it as a config error, not a raw crash.
        raise ConfigError(f"cannot read config file {path}: {exc}") from exc
    if len(raw) > _MAX_CONFIG_BYTES:
        raise ConfigError(f"config file too large (exceeds {_MAX_CONFIG_BYTES} bytes)")
    # Empty file is valid: treat as empty config for consistent behavior.
    if not raw.strip():
        data = {}
    elif path.lower().endswith((".yaml", ".yml")):
        data = _load_yaml(raw)
    else:
        data = _load_json(raw)
    if not isinstance(data, dict):
        raise ConfigError(
            f"config must be an object mapping keys to sections, got "
            f"{type(data).__name__}"
        )
    return merge_defaults(data)


def _validate_mcp(section: object) -> None:
    """Validate the MCP section: a mapping of named stdio servers.

    A malformed server definition would otherwise fail at launch time as
    a bare OSError from a child process, so it is rejected here with the
    offending key named.
    """
    if section is None:
        return
    if not isinstance(section, dict):
        raise ConfigError(f"config mcp must be an object, got {type(section).__name__}")
    servers = section.get("servers")
    if servers is None:
        return
    if not isinstance(servers, dict):
        raise ConfigError("config mcp.servers must be an object mapping names to servers")
    for name, spec in servers.items():
        if not isinstance(spec, dict):
            raise ConfigError(f"config mcp.servers.{name} must be an object")
        unknown = [k for k in spec if k not in _MCP_SERVER_KEYS]
        if unknown:
            raise ConfigError(
                f"unknown config keys in mcp.servers.{name}: {sorted(unknown)} "
                f"(known: {sorted(_MCP_SERVER_KEYS)})"
            )
        command = spec.get("command")
        if isinstance(command, str):
            if not command.strip():
                raise ConfigError(f"config mcp.servers.{name}.command must not be empty")
        elif isinstance(command, list):
            if not command or any(
                not isinstance(part, str) or not part.strip() for part in command
            ):
                raise ConfigError(
                    f"config mcp.servers.{name}.command must be a non-empty list of strings"
                )
        else:
            raise ConfigError(
                f"config mcp.servers.{name}.command must be a string or a list of strings"
            )
        enabled = spec.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ConfigError(f"config mcp.servers.{name}.enabled must be true or false")
        cwd = spec.get("cwd")
        if cwd is not None and not isinstance(cwd, str):
            raise ConfigError(f"config mcp.servers.{name}.cwd must be a string")
        env = spec.get("env")
        if env is not None and not isinstance(env, dict):
            raise ConfigError(f"config mcp.servers.{name}.env must be an object")
        timeout = spec.get("timeout")
        if timeout is not None and (
            isinstance(timeout, bool) or not isinstance(timeout, (int, float))
        ):
            raise ConfigError(f"config mcp.servers.{name}.timeout must be a number")


def _deep_merge(base: dict, incoming: dict) -> dict:
    out = dict(base)
    for key, value in incoming.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value) if isinstance(value, (dict, list)) else value
    return out


# Sections whose keys are validated here (the rest are forwarded to the
# registry, which already rejects unknown constructor keys).
_SECTION_KEYS = {
    "context": {"max_messages", "max_chars", "digest", "digest_max_chars"},
    "observations": {"reshape", "max_chars"},
    "skills": {"auto", "auto_bundle"},
    "mcp": {"servers"},
}

# Keys accepted inside one MCP server definition.
_MCP_SERVER_KEYS = {"command", "cwd", "env", "enabled", "timeout"}

# The key inside each component section that names the concrete class. When
# an operator switches component type (e.g. evaluator "command" -> "none"
# or llm provider "openai" -> "scripted"), the section must not inherit the
# previous type's default keys: the registry rejects unknown constructor
# keys, so a leftover default "test_cmd" would break the switch.
_DISCRIMINATOR_KEYS = {
    "llm": "provider",
    "sandbox": "provider",
    "evaluator": "type",
    "logging": "type",
}

# Value types for the component sections. These sections are forwarded to
# the registry, so a wrong type would only surface deep inside a
# constructor (or at request time); validate here instead. Fields not
# listed keep whatever the component constructor accepts.
_COMPONENT_VALUE_TYPES = {
    "llm": {
        "model": (str,),
        "api_key_env": (str,),
        "base_url": (str,),
        "temperature": (int, float),
        "max_tokens": (int,),
        "stream": (bool,),
    },
    "sandbox": {
        "image": (str,),
        "mem_limit": (str,),
        "workdir": (str,),
    },
    "evaluator": {
        "timeout": (int, float),
        "test_cmd": (str,),
    },
    "logging": {
        "path": (str,),
    },
}

_TYPE_LABELS = {str: "a string", int: "an integer", float: "a number", bool: "true or false"}


def merge_defaults(data: dict) -> dict:
    if not isinstance(data, dict):
        raise ConfigError(
            f"config must be an object mapping keys to sections, got "
            f"{type(data).__name__}"
        )
    # Unknown keys are rejected rather than silently ignored: a misspelled
    # section would otherwise leave the default value in force with no
    # diagnostic at all.
    unknown_top = [k for k in data if k not in DEFAULTS]
    if unknown_top:
        raise ConfigError(
            f"unknown config keys {sorted(unknown_top)} (known: {sorted(DEFAULTS)})"
        )
    for section, allowed in _SECTION_KEYS.items():
        incoming = data.get(section)
        if isinstance(incoming, dict):
            unknown = [k for k in incoming if k not in allowed]
            if unknown:
                raise ConfigError(
                    f"unknown config keys in '{section}': {sorted(unknown)} "
                    f"(known: {sorted(allowed)})"
                )
    # A bad observation ceiling must not reach the loop as a surprise: a
    # non-integer would compare against a string length and never trigger.
    obs = data.get("observations")
    if isinstance(obs, dict):
        for key, label in (("reshape", "true or false"), ("max_chars", "an integer")):
            if key in obs:
                value = obs[key]
                ok = (
                    isinstance(value, bool)
                    if key == "reshape"
                    else isinstance(value, int) and not isinstance(value, bool)
                )
                if not ok:
                    raise ConfigError(f"config observations.{key} must be {label}")
                if key == "max_chars" and value < 0:
                    raise ConfigError("config observations.max_chars must not be negative")
    # Deep copy prevents mutation of shared DEFAULTS.
    merged = copy.deepcopy(DEFAULTS)
    for key, value in (data or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            discriminator = _DISCRIMINATOR_KEYS.get(key)
            if (
                discriminator is not None
                and discriminator in value
                and value.get(discriminator) != merged[key].get(discriminator)
            ):
                # Different component type: start from the operator's own
                # section rather than the default's, so the previous type's
                # keys cannot leak into the new type's constructor.
                merged[key] = copy.deepcopy(value)
            else:
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
    for section, fields in _COMPONENT_VALUE_TYPES.items():
        section_data = merged.get(section)
        if not isinstance(section_data, dict):
            continue
        for field, expected in fields.items():
            if field not in section_data:
                continue
            value = section_data[field]
            if isinstance(value, bool) and bool not in expected:
                raise ConfigError(
                    f"config {section}.{field} must be "
                    f"{'/'.join(_TYPE_LABELS[t] for t in expected)}, got {value!r}"
                )
            if not isinstance(value, expected):
                raise ConfigError(
                    f"config {section}.{field} must be "
                    f"{'/'.join(_TYPE_LABELS[t] for t in expected)}, got {value!r}"
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
    compact = merged.get("auto_compact_tokens", 0)
    if not isinstance(compact, int) or isinstance(compact, bool) or compact < 0:
        raise ConfigError(
            f"config auto_compact_tokens must be a non-negative integer, got {compact!r}"
        )
    verbose = merged.get("verbose", False)
    if not isinstance(verbose, bool):
        raise ConfigError(f"config verbose must be true or false, got {verbose!r}")
    prompt = merged.get("system_prompt")
    if prompt is not None and not isinstance(prompt, str):
        raise ConfigError(f"config system_prompt must be a string or null, got {prompt!r}")
    mode = merged.get("approvals", "yolo")
    if mode not in ("default", "auto", "yolo", "plan"):
        raise ConfigError(
            f"config approvals must be one of yolo/default/auto/plan, got '{mode}'"
        )
    _validate_mcp(merged.get("mcp"))
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
        dg = ctx.get("digest")
        if dg is not None and not isinstance(dg, bool):
            raise ConfigError(f"config context.digest must be true or false, got {dg!r}")
        dc = ctx.get("digest_max_chars")
        if dc is not None and (
            not isinstance(dc, int) or isinstance(dc, bool) or dc < 0
        ):
            raise ConfigError(
                f"config context.digest_max_chars must be a non-negative integer, got {dc!r}"
            )
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
