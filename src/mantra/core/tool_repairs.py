"""Tool call repairs — validate-then-repair for LLM tool inputs.

Adapted from Command Code harness engineering /tool-call-repairs.

Four shape repairs cover ~90% of open-model failures:
  1. null-for-optional: {"timeoutMs": null} → {}
  2. json-array-parse: '"[\"a\",\"b\"]"' → ["a","b"]
  3. empty-placeholder: {} where array expected → handle
  4. bare-string-wrap: "foo" where ["foo"] expected → ["foo"]

Plus:
  - pathString alias repair (filePath → path, etc.)
  - markdown auto-link unwrap: "/a/[notes.md](http://notes.md)" → "/a/notes.md"
  - numeric string coercion via Number() semantics (not parseInt)

Design: validate first, repair only on issue paths, re-validate.
"""

from __future__ import annotations

import json
import re
from typing import Any


def _coerce_numeric_string(s: str, expect: str) -> int | float | None:
    """Coerce a numeric string via strict Number() semantics.

    Returns int or float on success, None on failure. Rejects "2abc",
    empty, whitespace-only, or fractional for integer fields.
    """
    if not s or not s.strip():
        return None
    s = s.strip()
    # Must be pure integer or float, no trailing chars (unlike parseInt)
    # Allow optional leading minus and single dot.
    if expect == "integer":
        if s.lstrip("-").isdigit():
            try:
                return int(s)
            except ValueError:
                return None
        return None
    # expect number: allow int or float
    try:
        # Strict: string must round-trip via float without extra chars
        # Use regex to validate shape first
        if not re.match(r"^-?\d+(\.\d+)?$", s):
            return None
        if "." in s:
            return float(s)
        return int(s)
    except (ValueError, TypeError):
        return None

# Aliases for path-like params — model sends filePath, absolutePath, etc.
ALIASES: dict[str, list[str]] = {
    "path": ["path", "file_path", "filePath", "filepath", "absolutePath", "absolute_path", "target_file", "filename", "file"],
    "command": ["command", "cmd", "shellCommand", "shell_command", "shellCmd"],
    "timeout": ["timeout", "timeoutMs", "timeout_ms", "timeoutMS"],
    "query": ["query", "pattern", "search", "searchTerm", "search_term", "term"],
    "pattern": ["pattern", "query", "glob", "filePattern", "file_pattern"],
    "url": ["url", "link", "href", "targetUrl"],
    "content": ["content", "text", "body", "fileContent", "file_content", "data"],
    "old_string": ["old_string", "oldString", "old", "search", "oldText"],
    "new_string": ["new_string", "newString", "new", "replace", "newText", "content"],
    "max_chars": ["max_chars", "maxChars", "maxLength", "limit"],
}

# Reverse map: alias -> canonical
_ALIAS_REVERSE: dict[str, str] = {}
for canon, alist in ALIASES.items():
    for a in alist:
        _ALIAS_REVERSE[a] = canon
        _ALIAS_REVERSE[a.lower()] = canon

# Markdown auto-link: only when link text == url without protocol
_AUTO_LINK_RE = re.compile(r"\[([^\]]+)\]\(https?://([^\)]+)\)")

def _unwrap_auto_link(value: str) -> str:
    """Unwrap degenerate auto-link where text equals url-without-protocol."""
    def _repl(m: re.Match[str]) -> str:
        text, url = m.group(1), m.group(2)
        # Normalize: text == url or text == url without trailing slash
        if text.strip() == url.strip() or text.strip() == url.strip("/"):
            return text.strip()
        # Also handle case where text is filename and url is same filename
        # e.g. [notes.md](http://notes.md) -> notes.md is same as url's last part
        if "/" not in text and url.endswith(text):
            return text
        return m.group(0)
    return _AUTO_LINK_RE.sub(_repl, value)

def _repair_quoted_escapes_json(text: str) -> str:
    r"""Fix single-backslash Windows paths in JSON strings.

    Repair-QuotedEscapesLocal: handles C:\ paths
    Handles C:\\ vs C:\ and invalid \U escapes by doubling.
    """
    # Use regex to find JSON string values: "...." with escapes
    def _fix_string(m: re.Match[str]) -> str:
        inner = m.group(1)
        # Check if inner looks like Windows path
        is_path = bool(re.search(r"[A-Za-z]:\\|\\", inner))
        out = []
        i = 0
        while i < len(inner):
            c = inner[i]
            if c == "\\":
                if i + 1 < len(inner):
                    nxt = inner[i + 1]
                    if is_path:
                        # Path: every single \ -> \\, existing \\ stays \\
                        if nxt == "\\":
                            out.append("\\\\")
                            i += 2
                            continue
                        else:
                            out.append("\\\\")
                            i += 1
                            continue
                    else:
                        # Non-path: only valid escapes are \" \\ / b f n r t u
                        if nxt in ('"', "\\", "/", "b", "f", "n", "r", "t", "u"):
                            out.append("\\")
                            out.append(nxt)
                            i += 2
                            continue
                        else:
                            out.append("\\\\")
                            i += 1
                            continue
                else:
                    out.append("\\\\")
                    i += 1
                    continue
            else:
                out.append(c)
                i += 1
        return '"' + "".join(out) + '"'

    # Find all JSON string literals: "..." with possible escapes
    return re.sub(r'"((?:\\.|[^"\\])*)"', _fix_string, text)

def repair_quoted_escapes_json_text(json_text: str) -> str:
    """Repair single-backslash Windows paths in JSON text."""
    return _repair_quoted_escapes_json(json_text)

def _is_json_array_string(s: str) -> bool:
    s = s.strip()
    return (s.startswith("[") and s.endswith("]")) or (s.startswith("{") and s.endswith("}")) or (s.startswith('"') and s.endswith('"'))

def repair_arguments(tool_name: str, arguments: dict[str, Any], schema: dict[str, Any] | None = None) -> tuple[dict[str, Any], list[str]]:
    """Validate then repair arguments.

    Returns (repaired_args, notes) where notes are human-readable repairs applied.
    Schema is tool.parameters if available.
    """
    if not isinstance(arguments, dict):
        # Bare string where dict expected — try parse
        if isinstance(arguments, str) and arguments.strip().startswith("{"):
            try:
                parsed = json.loads(arguments)
                if isinstance(parsed, dict):
                    return parsed, ["parsed stringified JSON arguments"]
            except Exception:
                pass
        return arguments if isinstance(arguments, dict) else {}, ["repaired: arguments not a dict"]

    repaired = dict(arguments)
    notes: list[str] = []

    # 1. Alias repair: map filePath -> path etc.
    # Build expected params from schema if available
    expected: set[str] = set()
    if schema and isinstance(schema.get("properties"), dict):
        expected = set(schema["properties"].keys())
    else:
        # Fallback known params per tool
        fallback = {
            "read_file": {"path"},
            "write_file": {"path", "content"},
            "edit_file": {"path", "old_string", "new_string"},
            "list_dir": {"path"},
            "run_command": {"command", "timeout"},
            "search_code": {"query"},
            "find_file": {"pattern"},
            "web_fetch": {"url", "max_chars"},
            "git_diff": set(),
            "git_reset": set(),
        }
        expected = fallback.get(tool_name, set())

    for key in list(repaired.keys()):
        canon = _ALIAS_REVERSE.get(key) or _ALIAS_REVERSE.get(key.lower())
        if canon and canon in expected:
            if canon not in repaired:
                repaired[canon] = repaired.pop(key)
                notes.append(f"aliased {key} -> {canon}")
            elif key != canon:
                # Remove stale alias when canonical already present
                repaired.pop(key, None)
                notes.append(f"removed stale alias {key} (kept {canon})")

    # 2. Null-for-optional: strip None values
    for k in list(repaired.keys()):
        if repaired[k] is None:
            del repaired[k]
            notes.append(f"stripped null {k}")

    # 3. PathString unwrap for any path-like value
    for k in list(repaired.keys()):
        v = repaired[k]
        if isinstance(v, str) and k in ("path", "file_path", "filepath", "pattern", "query", "url"):
            unwrapped = _unwrap_auto_link(v)
            if unwrapped != v:
                repaired[k] = unwrapped
                notes.append(f"unwrapped auto-link {k}")

    # 4. JSON-array-parse + bare-string-wrap (ordered: parse before wrap)
    if schema and isinstance(schema.get("properties"), dict):
        props = schema["properties"]
        for param, spec in props.items():
            if param not in repaired:
                continue
            val = repaired[param]
            ptype = spec.get("type")
            # Handle stringified JSON array/object
            if isinstance(val, str) and ptype in ("array", "object"):
                s = val.strip()
                if _is_json_array_string(s):
                    try:
                        parsed = json.loads(s)
                        # Only apply if parsed type matches expected
                        if (ptype == "array" and isinstance(parsed, list)) or (ptype == "object" and isinstance(parsed, dict)):
                            repaired[param] = parsed
                            notes.append(f"parsed JSON string {param}")
                            continue
                    except Exception:
                        pass
            # Bare-string-wrap: string where array expected
            if isinstance(val, str) and ptype == "array":
                repaired[param] = [val]
                notes.append(f"wrapped bare string {param} into array")
                continue
            # Empty placeholder: {} where array expected with path-like items
            if isinstance(val, dict) and not val and ptype == "array":
                # This is the empty placeholder case — needs real value, can't repair
                # Leave for validation to fail with proper error
                pass
            # Numeric string coercion via Number() semantics (not parseInt)
            if ptype in ("number", "integer") and isinstance(val, str):
                s = val.strip()
                coerced = _coerce_numeric_string(s, ptype)
                if coerced is not None:
                    repaired[param] = coerced
                    notes.append(f"coerced string {param} to {type(coerced).__name__}")

    # Generic string/number coercion for timeout/max_chars even without schema type
    for num_key in ("timeout", "max_chars", "limit", "offset"):
        if num_key in repaired and isinstance(repaired[num_key], str):
            s = repaired[num_key].strip()
            # Integer fields must not accept fractional values
            expect = "integer" if num_key in ("max_chars", "limit", "offset") else "number"
            coerced = _coerce_numeric_string(s, expect)
            if coerced is not None:
                repaired[num_key] = coerced
                notes.append(f"coerced string {num_key} to {type(coerced).__name__}")

    # Relational defaults: read_file offset/limit pair
    if tool_name == "read_file":
        has_offset = "offset" in repaired
        has_limit = "limit" in repaired
        if has_limit and not has_offset:
            repaired["offset"] = 0
            notes.append("defaulted offset to 0 for limit")
        elif has_offset and not has_limit:
            repaired["limit"] = 2000
            notes.append("defaulted limit to 2000 for offset")

    return repaired, notes

def validate_arguments(arguments: dict[str, Any], schema: dict[str, Any] | None) -> list[str]:
    """Return list of validation issues (empty if valid)."""
    if schema is None:
        return []
    issues = []
    props = schema.get("properties", {}) if isinstance(schema.get("properties"), dict) else {}
    required = schema.get("required", []) if isinstance(schema.get("required"), list) else []
    for req in required:
        if req not in arguments:
            issues.append(f"missing required field '{req}'")
    for k, v in arguments.items():
        if k not in props:
            continue
        spec = props[k]
        ptype = spec.get("type")
        if ptype == "string" and not isinstance(v, str):
            issues.append(f"field '{k}' expected string got {type(v).__name__}")
        elif ptype == "number" and not isinstance(v, (int, float)):
            issues.append(f"field '{k}' expected number got {type(v).__name__}")
        elif ptype == "integer" and not isinstance(v, int):
            issues.append(f"field '{k}' expected integer got {type(v).__name__}")
        elif ptype == "array" and not isinstance(v, list):
            issues.append(f"field '{k}' expected array got {type(v).__name__}")
        elif ptype == "object" and not isinstance(v, dict):
            issues.append(f"field '{k}' expected object got {type(v).__name__}")
    return issues
