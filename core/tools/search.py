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


def iter_workspace_files(
    root: str,
    *,
    max_scan: int = 3000,
    skip_ext: frozenset[str] | set[str] = (),
    max_file_bytes: int | None = None,
    stats: dict | None = None,
):
    """Yield ``(rel, full)`` for workspace files under the shared confinement rules.

    One implementation of the rules every workspace-scanning tool needs:
    skip-dirs pruning, ignore-file matching, symlink-escape filtering, and
    the scan ceiling. Filters apply before the ceiling is counted, so a
    skipped file never consumes the budget - the same accounting the
    search and find tools have always used. ``stats``, when given,
    receives ``scanned`` (files yielded) and ``truncated`` (a further file
    existed past the ceiling); it is filled even if the caller stops
    early.
    """
    real_root = os.path.realpath(root)
    ignored = _load_ignore_matcher(root)
    scanned = 0
    truncated = False
    try:
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
            if ignored is not None:
                rel_dir = os.path.relpath(dirpath, root).replace(os.sep, "/")
                prefix = "" if rel_dir == "." else rel_dir + "/"
                dirnames[:] = [d for d in dirnames if not ignored(prefix + d, True)]
            # A symlinked directory that resolves outside the workspace is
            # never descended into.
            kept = []
            for d in dirnames:
                full_dir = os.path.join(dirpath, d)
                try:
                    if os.path.islink(full_dir):
                        real = os.path.realpath(full_dir)
                        if not (real == real_root or real.startswith(real_root + os.sep)):
                            continue
                except OSError:
                    continue
                kept.append(d)
            dirnames[:] = kept
            for filename in filenames:
                if skip_ext and os.path.splitext(filename)[1].lower() in skip_ext:
                    continue
                full = os.path.join(dirpath, filename)
                try:
                    if os.path.islink(full):
                        real = os.path.realpath(full)
                        if not (real == real_root or real.startswith(real_root + os.sep)):
                            continue
                    if max_file_bytes is not None and os.path.getsize(full) > max_file_bytes:
                        continue
                except OSError:
                    continue
                scanned += 1
                if scanned > max_scan:
                    truncated = True
                    break
                rel = os.path.relpath(full, root)
                if ignored is not None and ignored(rel.replace(os.sep, "/"), False):
                    continue
                yield rel, full
            if scanned > max_scan:
                break
    finally:
        if stats is not None:
            stats["scanned"] = scanned
            stats["truncated"] = truncated


def _normalize_grep_lines(lines: list[str]) -> list[str]:
    """Normalize shell-grep output into the tool's canonical path:line: text shape."""
    out: list[str] = []
    for raw in lines:
        if not raw:
            continue
        # Keep only lines that look like path:lineno: content markers.
        if ":" in raw:
            out.append(raw.rstrip())
    return out


class SearchCodeTool(Tool):
    name = "search_code"
    description = (
        "Search text files in the workspace for a literal substring and return "
        "matching lines with file path and line number. Use it for code search, "
        "symbol lookups, and quick text presence checks across the workspace."
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
            if result.exit_code == 1:
                # grep's conventional "no matches" status, not a failure.
                return "(no matches)"
            if result.exit_code != 0:
                detail = (result.stderr or "").strip()[:500] or "unknown error"
                return f"ERROR: search failed (exit {result.exit_code}): {detail}"
            return "\n".join(_normalize_grep_lines(result.stdout.strip().splitlines()))[:20000] if result.stdout.strip() else "(no matches)"

        hits: list[str] = []
        real_root = os.path.realpath(root)
        stats: dict = {}
        for rel, full in iter_workspace_files(
            root, skip_ext=_SKIP_EXT, max_file_bytes=_MAX_FILE_BYTES, stats=stats
        ):
            hits.extend(self._scan_file(full, rel, query, real_root))
            if len(hits) >= _MAX_RESULTS:
                break
        truncated_note = ""
        if stats.get("truncated"):
            truncated_note = (
                f"\n... [truncated — scanned {stats.get('scanned')} files, "
                "ceiling reached; narrow the query or directory]"
            )
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
                    # Stop scanning an enormous file line by line
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
            if result.exit_code != 0:
                detail = (result.stderr or "").strip()[:500] or "unknown error"
                return f"ERROR: find failed (exit {result.exit_code}): {detail}"
            return "\n".join(_normalize_grep_lines(result.stdout.strip().splitlines()))[:20000] if result.stdout.strip() else "(no matches)"

        matches = []
        stats: dict = {}
        for rel, full in iter_workspace_files(root, stats=stats):
            if pattern in os.path.basename(full):
                matches.append(rel)
                if len(matches) >= _MAX_RESULTS:
                    return "\n".join(matches) + f"\n... [hit ceiling {_MAX_RESULTS} reached]"
        truncated = ""
        if stats.get("truncated"):
            truncated = f"\n... [truncated — scanned {stats.get('scanned')} files, ceiling reached]"
        return ("\n".join(matches) or "(no matches)") + truncated
