"""Tool call repairs — validate-then-repair for model tool inputs.

Four shape repairs cover most open-model failures:
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

# Aliases per canonical parameter; shared aliases resolve against the
# vocabulary of the tool being repaired.
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
    r"""Double single backslashes in JSON string literals so Windows paths survive parsing."""
    # Only escape-bearing text can need this repair; text without a backslash
    # is returned untouched, so a valid payload is never rewritten by this pass.
    if "\\" not in text:
        return text
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
                    # Trailing backslash with no next char: double it.
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


def canonical_command(arguments: dict[str, Any]) -> str:
    """The run_command payload under its canonical ``command`` key.

    Alias spellings (cmd, shellCommand, ...) resolve to the canonical
    value so dedup and session keys agree with the repair pass.
    """
    command = str(arguments.get("command") or "").strip()
    if command:
        return command
    for alias in ALIASES.get("command", ()):
        if alias != "command" and arguments.get(alias):
            return str(arguments[alias]).strip()
    return ""

def _is_json_array_string(s: str) -> bool:
    # The name is historical: the pattern also matches arrays, objects,
    # and quoted strings, and gates the object-string parse path below.
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
            except (json.JSONDecodeError, ValueError):
                pass  # not JSON after all; handled as a plain string below
        return arguments if isinstance(arguments, dict) else {}, ["repaired: arguments not a dict"]

    repaired = dict(arguments)
    notes: list[str] = []

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
            "extract_document": {"path", "max_chars"},
            "web_fetch": {"url", "max_chars"},
            "git_diff": set(),
            "git_reset": set(),
        }
        expected = fallback.get(tool_name, set())

    # 1. Alias repair: each expected parameter claims a missing value from
    # its own alias list, never a global map.
    moved: set[str] = set()
    for canon in sorted(expected):
        if canon in repaired:
            continue
        aliases = [a.lower() for a in ALIASES.get(canon, [])]
        for key in list(repaired.keys()):
            if key in moved:
                continue
            if key != canon and key.lower() in aliases:
                repaired[canon] = repaired.pop(key)
                # One claim per alias key, so a shared alias cannot be
                # taken by two canonical names in the same pass.
                moved.add(key)
                notes.append(f"aliased {key} -> {canon}")
                break
    # Remove stale aliases whose canonical name is already present.
    for key in list(repaired.keys()):
        if key in expected:
            continue
        canon = next(
            (c for c, lst in ALIASES.items() if key.lower() in [a.lower() for a in lst]),
            None,
        )
        if canon and canon != key and canon in repaired:
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

    # 4. JSON-array-parse + bare-string-wrap (ordered: parse before wrap).
    # Without a schema, only the generic numeric-key coercion below runs.
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
                    except (json.JSONDecodeError, ValueError):
                        pass  # fall through to bare-string wrapping
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

    # Relational defaults: a limit without an offset implies a full read
    # from the start; an offset without a limit reads one default window.
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
        elif ptype == "number" and (isinstance(v, bool) or not isinstance(v, (int, float))):
            # bool is a subclass of int: True/False must not pass as numbers.
            issues.append(f"field '{k}' expected number got {type(v).__name__}")
        elif ptype == "integer" and (isinstance(v, bool) or not isinstance(v, int)):
            issues.append(f"field '{k}' expected integer got {type(v).__name__}")
        elif ptype == "array" and not isinstance(v, list):
            issues.append(f"field '{k}' expected array got {type(v).__name__}")
        elif ptype == "object" and not isinstance(v, dict):
            issues.append(f"field '{k}' expected object got {type(v).__name__}")
    return issues
