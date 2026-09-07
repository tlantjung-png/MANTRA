"""Search tools: pure-Python workspace walk."""

from __future__ import annotations

import fnmatch
import os
import re
import shlex
from typing import Any

from core.types import Sandbox
from core.types import Tool

_SHELL_META_RE = re.compile(r"[;&|`$()<>]")

_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".mantra", ".pytest_cache", "dist", "build"}
_MAX_RESULTS = 50
_MAX_FILE_BYTES = 500_000
_SKIP_EXT = {".pyc", ".pyo", ".so", ".dll", ".exe", ".bin", ".zip", ".tar", ".gz", ".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf"}


def _load_ignore_matcher(root: str):
    """Return a matcher honoring .gitignore/.ignore at the workspace root.

    Minimal-but-correct: non-comment, non-negated patterns are matched by
    fnmatch against an entry's basename or its root-relative path. A
    trailing slash marks a directory-only pattern; a leading slash is
    stripped and the pattern then matches against the root-relative path.
    The fixed _SKIP_DIRS set keeps applying regardless. Returns None when
    no ignore file exists or it has no usable patterns.
    """
    entries: list[tuple[str, bool]] = []
    for ignore_name in (".gitignore", ".ignore"):
        try:
            with open(os.path.join(root, ignore_name), "r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    line = line.strip()
                    # Negated patterns (!) are ignored by design.
                    if not line or line.startswith("#") or line.startswith("!"):
                        continue
                    dir_only = line.endswith("/")
                    if dir_only:
                        line = line[:-1]
                    if line.startswith("/"):
                        line = line[1:]
                    if line:
                        entries.append((line, dir_only))
        except OSError:
            continue
    if not entries:
        return None

    def _ignored(rel: str, is_dir: bool) -> bool:
        name = rel.split("/")[-1]
        for pat, dir_only in entries:
            if dir_only and not is_dir:
                continue
            if fnmatch.fnmatch(name, pat) or fnmatch.fnmatch(rel, pat):
                return True
        return False

    return _ignored


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
            # -F keeps the query a literal substring instead of a regex;
            # -- ends option parsing so a dash-prefixed query is never
            # read as a flag. --exclude-dir flags must precede the --
            # terminator to stay options; they mirror the walk's fixed
            # skip set.
            quoted = shlex.quote(query)
            skip_flags = " ".join(f"--exclude-dir={d}" for d in sorted(_SKIP_DIRS))
            result = sandbox.exec(f"grep -Frn {skip_flags} -- {quoted} .")
            if result.exit_code != 0:
                return "(no matches)"
            return result.stdout[:20000] if result.stdout.strip() else "(no matches)"

        hits: list[str] = []
        real_root = os.path.realpath(root)
        ignored = _load_ignore_matcher(root)
        scanned = 0
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            # Prevent descending into symlinked dirs that escape workspace
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
            # Honor gitignored directories so their contents are never
            # scanned: files below are unreachable once the dir is pruned.
            if ignored is not None:
                rel_dir = os.path.relpath(dirpath, root).replace(os.sep, "/")
                prefix = "" if rel_dir == "." else rel_dir + "/"
                dirnames[:] = [d for d in dirnames if not ignored(prefix + d, True)]
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
                if ignored is not None and ignored(rel.replace(os.sep, "/"), False):
                    continue
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
            line_cap_hit = False
            with open(full, "r", encoding="utf-8", errors="replace") as handle:
                for lineno, line in enumerate(handle):
                    if query in line:
                        out.append(f"{rel}:{lineno + 1}: {line.rstrip()[:300]}")
                        if len(out) >= _MAX_RESULTS:
                            break
                    # Avoid scanning huge files line-by-line indefinitely
                    if lineno > 10000:
                        line_cap_hit = True
                        break
            if line_cap_hit:
                out.append(f"... [truncated — {rel} exceeds 10000 scanned lines; narrow the query]")
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
            # Sandbox without a direct file view: shell find, safely quoted.
            quoted = shlex.quote(f"*{pattern}*")
            result = sandbox.exec(
                f"find . -name {quoted} -not -path './.git/*'"
            )
            return result.stdout[:20000] if result.exit_code == 0 else "(no matches)"

        matches = []
        real_root = os.path.realpath(root)
        ignored = _load_ignore_matcher(root)
        scanned = 0
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
            if ignored is not None:
                rel_dir = os.path.relpath(dirpath, root).replace(os.sep, "/")
                prefix = "" if rel_dir == "." else rel_dir + "/"
                dirnames[:] = [d for d in dirnames if not ignored(prefix + d, True)]
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
                scanned += 1
                if scanned > 3000:
                    break
                if ignored is not None and ignored(os.path.relpath(full, root).replace(os.sep, "/"), False):
                    continue
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
