"""Search tools: pure-Python workspace walk."""

from __future__ import annotations

import os
import re
import shlex
from typing import Any

from mantra.interfaces.sandbox import Sandbox
from mantra.interfaces.tool import Tool

_SHELL_META_RE = re.compile(r"[;&|`$()<>]")

_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".mantra", ".pytest_cache", "dist", "build"}
_MAX_RESULTS = 50
_MAX_FILE_BYTES = 500_000
_SKIP_EXT = {".pyc", ".pyo", ".so", ".dll", ".exe", ".bin", ".zip", ".tar", ".gz", ".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf"}


class SearchCodeTool(Tool):
    name = "search_code"
    description = (
        "Search for a literal substring across text files in the workspace. "
        "Returns matching lines prefixed with 'path:line'."
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Literal text to find"}
        },
        "required": ["query"],
    }

    def execute(self, sandbox: Sandbox, query: str) -> str:
        if "\x00" in query or "\n" in query or "\r" in query:
            return "ERROR: query contains invalid characters"
        if len(query) > 500:
            return "ERROR: query too long"
        root = getattr(sandbox, "root", None)
        if root is None:
            # Fall back to shell grep for sandboxes without a file view.
            quoted = shlex.quote(query)
            result = sandbox.exec(f"grep -rn {quoted} . --exclude-dir=.git")
            if result.exit_code != 0:
                return "(no matches)"
            return result.stdout[:20000] if result.stdout.strip() else "(no matches)"

        hits: list[str] = []
        real_root = os.path.realpath(root)
        scanned = 0
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            # Prevent descending into symlinked dirs that escape workspace
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
            # Filter symlinked dirs that point outside
            filtered = []
            for d in dirnames:
                full_dir = os.path.join(dirpath, d)
                try:
                    if os.path.islink(full_dir):
                        real = os.path.realpath(full_dir)
                        if not (real == real_root or real.startswith(real_root + os.sep)):
                            continue
                except OSError:
                    continue
                filtered.append(d)
            dirnames[:] = filtered
            for filename in filenames:
                if os.path.splitext(filename)[1].lower() in _SKIP_EXT:
                    continue
                full = os.path.join(dirpath, filename)
                try:
                    if os.path.islink(full):
                        real = os.path.realpath(full)
                        if not (real == real_root or real.startswith(real_root + os.sep)):
                            continue
                    if os.path.getsize(full) > _MAX_FILE_BYTES:
                        continue
                except OSError:
                    continue
                scanned += 1
                if scanned > 3000:
                    break
                rel = os.path.relpath(full, root)
                hits.extend(self._scan_file(full, rel, query, real_root))
                if len(hits) >= _MAX_RESULTS:
                    break
            if len(hits) >= _MAX_RESULTS or scanned > 3000:
                break
        truncated_note = ""
        if scanned > 3000:
            truncated_note = f"\n... [truncated — scanned {scanned} files, ceiling reached; narrow the query or directory]"
        if not hits:
            return "(no matches)" + truncated_note
        result = "\n".join(hits)
        if truncated_note:
            result += truncated_note
        # Also note if hit ceiling reached
        if len(hits) >= _MAX_RESULTS:
            result += f"\n... [hit ceiling {_MAX_RESULTS} reached; refine query]"
        return result

    @staticmethod
    def _scan_file(full: str, rel: str, query: str, real_root: str | None = None) -> list[str]:
        # If real_root provided, double-check file still inside after symlink check
        if real_root is not None:
            try:
                real = os.path.realpath(full)
                if not (real == real_root or real.startswith(real_root + os.sep)):
                    return []
            except OSError:
                return []
        try:
            out = []
            with open(full, "r", encoding="utf-8", errors="replace") as handle:
                for lineno, line in enumerate(handle):
                    if query in line:
                        out.append(f"{rel}:{lineno}: {line.rstrip()[:300]}")
                        if len(out) >= _MAX_RESULTS:
                            break
                    # Avoid scanning huge files line-by-line indefinitely
                    if lineno > 10000:
                        break
        except OSError:
            return []
        return out


class FindFileTool(Tool):
    name = "find_file"
    description = "Find files whose name contains the given substring."
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {"pattern": {"type": "string"}},
        "required": ["pattern"],
    }

    def execute(self, sandbox: Sandbox, pattern: str) -> str:
        if "\x00" in pattern or "\n" in pattern or "\r" in pattern:
            return "ERROR: pattern contains invalid characters"
        if _SHELL_META_RE.search(pattern) or '"' in pattern or "'" in pattern or "*" in pattern or "?" in pattern:
            return "ERROR: pattern contains unsupported characters"
        if len(pattern) > 200:
            return "ERROR: pattern too long"
        root = getattr(sandbox, "root", None)
        if root is None:
            quoted = shlex.quote(f"*{pattern}*")
            result = sandbox.exec(
                f"find . -name {quoted} -not -path './.git/*'"
            )
            return result.stdout[:20000] if result.exit_code == 0 else "(no matches)"

        matches = []
        real_root = os.path.realpath(root)
        scanned = 0
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
            filtered = []
            for d in dirnames:
                full_dir = os.path.join(dirpath, d)
                try:
                    if os.path.islink(full_dir):
                        real = os.path.realpath(full_dir)
                        if not (real == real_root or real.startswith(real_root + os.sep)):
                            continue
                except OSError:
                    continue
                filtered.append(d)
            dirnames[:] = filtered
            for filename in filenames:
                full = os.path.join(dirpath, filename)
                try:
                    if os.path.islink(full):
                        real = os.path.realpath(full)
                        if not (real == real_root or real.startswith(real_root + os.sep)):
                            continue
                except OSError:
                    continue
                try:
                    if os.path.getsize(full) > _MAX_FILE_BYTES:
                        continue
                except OSError:
                    continue
                scanned += 1
                if scanned > 3000:
                    break
                if pattern in filename:
                    matches.append(os.path.relpath(full, root))
                    if len(matches) >= _MAX_RESULTS:
                        return "\n".join(matches) + f"\n... [hit ceiling {_MAX_RESULTS} reached]"
            if scanned > 3000:
                break
        truncated = ""
        if scanned > 3000:
            truncated = f"\n... [truncated — scanned {scanned} files, ceiling reached]"
        return ("\n".join(matches) or "(no matches)") + truncated
