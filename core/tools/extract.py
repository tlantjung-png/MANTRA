"""Bounded workspace document extraction and tree/query tools.

Two additions inspired by unified text extraction and tree-pattern search
without adopting any external brand naming:

- extract_document: read a single file and return a bounded plain-text
  representation for common structured formats (HTML, JSON, XML, CSV,
  markdown, ini/toml-ish). It is intentionally narrow: formats that it
  cannot render are reported as a note, not silently dropped.

- query_tree: a small tree-pattern query layer for repository structure.
  It is not a full code-query engine; it supports literal path/glob
  selection plus a tiny brace-shaped syntax for exploring file trees and
  simple structural presence checks.
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
import shlex
import xml.etree.ElementTree as _stdlib_et  # noqa: F401 - reserved for the stdlib pretty-print path (no external entity expansion by default); defusedxml is the parser in use below
import defusedxml.ElementTree as ET
from csv import reader as csv_reader
from typing import Any

from core.types import Sandbox
from core.types import Tool

from core.tools.search import _SHELL_META_RE
from core.tools.search import _SKIP_DIRS
from core.tools.search import _load_ignore_matcher

_MAX_TEXT_BYTES = 500_000
_MAX_TEXT_CHARS = 80_000
_MAX_RESULTS = 50
_MAX_SCAN_FILES = 3000

EXTRACTABLE_MIME_HINTS = {
    ".htm": "html",
    ".html": "html",
    ".xml": "xml",
    ".json": "json",
    ".csv": "csv",
    ".md": "markdown",
    ".txt": "text",
    ".ini": "ini",
    ".toml": "toml-ish",
    ".cfg": "ini",
    ".conf": "text",
    ".css": "text",
    ".svg": "text",
    ".log": "text",
    ".yaml": "text",
    ".yml": "text",
    ".py": "text",
    ".pyw": "text",
    ".js": "text",
    ".mjs": "text",
    ".cjs": "text",
    ".ts": "text",
    ".sh": "text",
    ".bash": "text",
    ".zsh": "text",
    ".rs": "text",
    ".go": "text",
    ".rb": "text",
    ".pl": "text",
    ".lua": "text",
    ".r": "text",
    ".R": "text",
    ".sql": "text",
}


def _looks_extractable(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in EXTRACTABLE_MIME_HINTS


def _has_glob(text: str) -> bool:
    return "*" in text or "?" in text or ("[" in text and "]" in text)


class ExtractDocumentTool(Tool):
    name = "extract_document"
    description = (
        "Extract a bounded plain-text representation from a single workspace "
        "file. It normalizes HTML to readable text, pretty-prints JSON and "
        "XML, renders CSV rows, strips common markup from markdown, and gives "
        "a concise note for binary/archive formats it cannot render. Use it to "
        "read structured documents without streaming huge raw bodies into the "
        "conversation."
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Relative file path in the workspace"},
            "max_chars": {
                "type": "integer",
                "description": "Maximum returned characters (default 80000)",
            },
        },
        "required": ["path"],
    }

    def execute(self, sandbox: Sandbox, path: str, max_chars: int = 80000) -> str:
        if "\x00" in path or "\n" in path or "\r" in path:
            return "ERROR: path contains invalid characters"
        if len(path) > 1000:
            return "ERROR: path too long"
        try:
            budget = max(1, min(int(max_chars), _MAX_TEXT_CHARS))
        except (TypeError, ValueError):
            budget = _MAX_TEXT_CHARS

        root = getattr(sandbox, "root", None)
        if root is None:
            return self._shell_extract(sandbox, path, budget)

        full = os.path.join(root, path)
        try:
            real_root = os.path.realpath(root)
            real_full = os.path.realpath(full)
            if not (real_full == real_root or real_full.startswith(real_root + os.sep)):
                return "ERROR: path escapes workspace"
        except OSError as exc:
            return f"ERROR: cannot resolve {path!r}: {exc}"

        if not os.path.isfile(real_full):
            return f"ERROR: file not found: {path!r}"

        try:
            size = os.path.getsize(real_full)
        except OSError as exc:
            return f"ERROR: cannot stat {path!r}: {exc}"

        if size > _MAX_TEXT_BYTES:
            return f"Note: {path!r} is large ({size} bytes, cap {_MAX_TEXT_BYTES}). Use read_file with windows instead."

        if not _looks_extractable(real_full):
            return self._note_unsupported(path, size)

        try:
            with open(real_full, "rb") as handle:
                raw = handle.read(_MAX_TEXT_BYTES)
        except OSError as exc:
            return f"ERROR: cannot read {path!r}: {exc}"

        text = self._decode(raw)
        if not text:
            return f"Note: {path!r} is empty"

        rendered = self._render(path, text)
        if len(rendered) > budget:
            rendered = rendered[:budget].rstrip() + "\n... [truncated]"
        header = f"{path!r} ({size} bytes)"
        return f"{header}\n\n{rendered}"

    # ------------------------------------------------------------------
    # Shell fallback for sandboxes without a direct file view
    # ------------------------------------------------------------------

    def _shell_extract(self, sandbox: Sandbox, path: str, budget: int) -> str:
        # Very small bounded extraction for sandboxes without a file view.
        # Only the literal-path case is supported; globs are refused here.
        if any(c in path for c in ("*", "?", "[")):
            return "ERROR: extract_document does not support globs in this sandbox"
        quoted = shlex.quote(path)
        res = sandbox.exec(f"test -f {quoted} && wc -c < {quoted}")
        if res.exit_code != 0:
            return "ERROR: extract_document requires a file view sandbox for this path"
        try:
            size = int(res.stdout.strip().splitlines()[0])
        except (ValueError, IndexError):
            size = 0
        if size > _MAX_TEXT_BYTES:
            return f"Note: {path!r} is large ({size} bytes, cap {_MAX_TEXT_BYTES}). Use a file-view sandbox or read_file instead."
        res = sandbox.exec(f"head -c {_MAX_TEXT_BYTES} {quoted}")
        if res.exit_code != 0:
            return f"ERROR: cannot read {path!r}"
        raw = res.stdout.encode("utf-8", errors="replace")[:_MAX_TEXT_BYTES]
        text = self._decode(raw)
        if not text:
            return f"Note: {path!r} is empty"
        rendered = self._render(path, text)
        if len(rendered) > budget:
            rendered = rendered[:budget].rstrip() + "\n... [truncated]"
        header = f"{path!r} ({size} bytes)"
        return f"{header}\n\n{rendered}"

    # ------------------------------------------------------------------
    # Small shared helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _decode(raw: bytes) -> str:
        # latin-1 never fails, so the loop always returns on the last candidate.
        for candidate in ("utf-8-sig", "utf-8", "latin-1"):
            try:
                return raw.decode(candidate)
            except UnicodeDecodeError:
                continue
        return raw.decode("latin-1", errors="replace")

    @staticmethod
    def _note_unsupported(path: str, size: int) -> str:
        ext = os.path.splitext(path)[1].lower().lstrip(".")
        if not ext:
            return f"Note: {path!r} has no recognized text extension ({size} bytes) — not extracted"
        return f"Note: {path!r} is {ext} ({size} bytes) — not extracted by this tool"

    @staticmethod
    def _render(path: str, text: str) -> str:
        ext = os.path.splitext(path)[1].lower()
        if ext in (".html", ".htm"):
            return _html_to_text(text)
        if ext == ".json":
            return _pretty_json(text)
        if ext in (".xml", ".svg"):
            return _pretty_xml(text)
        if ext == ".csv":
            return _csv_rows(text)
        if ext == ".md":
            return _markdown_plain(text)
        if ext in (".ini", ".cfg"):
            return _ini_plain(text)
        if ext == ".toml":
            return _toml_ish_plain(text)
        # Default: treat as ordinary text
        return text.rstrip() or "(empty)"


def _html_to_text(html: str) -> str:
    from html.parser import HTMLParser

    class _MiniExtractor(HTMLParser):
        _SKIP = {"script", "style", "noscript", "template", "head", "iframe"}
        _BREAK = {
            "p", "div", "br", "li", "tr", "section", "article", "header",
            "footer", "nav", "table", "ul", "ol", "dl", "blockquote", "pre",
            "h1", "h2", "h3", "h4", "h5", "h6",
        }

        def __init__(self) -> None:
            super().__init__(convert_charrefs=True)
            self.parts: list[str] = []
            self._skip_stack: list[str] = []

        def handle_starttag(self, tag: str, attrs: Any) -> None:
            if tag in self._SKIP:
                self._skip_stack.append(tag)
            elif tag in self._BREAK:
                self.parts.append("\n")

        def handle_startendtag(self, tag: str, attrs: Any) -> None:
            if tag in self._BREAK:
                self.parts.append("\n")

        def handle_endtag(self, tag: str) -> None:
            if tag in self._SKIP:
                if self._skip_stack and self._skip_stack[-1] == tag:
                    self._skip_stack.pop()
            elif tag in self._BREAK:
                self.parts.append("\n")

        def handle_data(self, data: str) -> None:
            if not self._skip_stack:
                self.parts.append(data)

        def text(self) -> str:
            joined = "".join(self.parts)
            joined = joined.replace("\r\n", "\n").replace("\r", "\n")
            joined = re.sub(r"[ \t\f\v]+", " ", joined)
            joined = re.sub(r" *\n *", "\n", joined)
            joined = re.sub(r"\n{3,}", "\n\n", joined)
            return joined.strip()

    ex = _MiniExtractor()
    try:
        ex.feed(html)
        ex.close()
    except Exception:
        pass
    return ex.text()


def _pretty_json(text: str) -> str:
    try:
        obj = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return text.rstrip()[:8000]
    try:
        return json.dumps(obj, ensure_ascii=False, indent=2)
    except (TypeError, ValueError):
        return str(obj)


def _pretty_xml(text: str) -> str:
    try:
        root = ET.fromstring(text)
        ET.indent(root)
        return ET.tostring(root, encoding="unicode", short_empty_elements=False)
    except (ET.ParseError, ValueError):
        return text.rstrip()[:8000]


def _csv_rows(text: str) -> str:
    try:
        rows = list(csv_reader(text.splitlines()))
    except Exception:
        return text.rstrip()[:8000]
    if not rows:
        return "(empty csv)"
    parts = []
    for row in rows[:200]:
        parts.append(" | ".join(str(cell) for cell in row))
    if len(rows) > 200:
        parts.append(f"... [{len(rows) - 200} more rows omitted]")
    return "\n".join(parts)


def _markdown_plain(text: str) -> str:
    # Very small markdown normalization: drop fences and common link markup,
    # keep visible prose.
    t = re.sub(r"```[\s\S]*?```", "[code block omitted]", text)
    t = re.sub(r"`[^`]+`", "[code]", t)
    t = re.sub(r"!?\[([^\]]*)\]\([^)]+\)", r"\1", t)
    t = re.sub(r"#{1,6}\s+", "", t)
    t = re.sub(r"[*_~]{1,3}", "", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def _ini_plain(text: str) -> str:
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith((";", "#")):
            continue
        lines.append(stripped)
    return "\n".join(lines).strip() or "(empty)"


def _toml_ish_plain(text: str) -> str:
    # Not a real TOML parser; just drop blank lines and comments.
    lines = []
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        lines.append(s)
    return "\n".join(lines).strip() or "(empty)"


class QueryTreeTool(Tool):
    name = "query_tree"
    description = (
        "Explore workspace file-tree structure with a small pattern query. "
        "It supports literal path filtering, simple globs (* and **), and a "
        "tiny brace-style shape syntax for common structural presence checks "
        "like finding directories that contain files matching a name pattern. "
        "It is a navigation aid, not a full code-query engine."
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Tree query: path, glob, or small shape expression"},
            "root": {"type": "string", "description": "Optional relative directory to scope the query"},
        },
        "required": ["pattern"],
    }

    def execute(self, sandbox: Sandbox, pattern: str, root: str = ".") -> str:
        if "\x00" in pattern or "\n" in pattern or "\r" in pattern:
            return "ERROR: pattern contains invalid characters"
        if _SHELL_META_RE.search(pattern) or '"' in pattern or "'" in pattern:
            return "ERROR: pattern contains unsupported characters"
        if len(pattern) > 500:
            return "ERROR: pattern too long"
        if "\x00" in root or "\n" in root or "\r" in root:
            return "ERROR: root contains invalid characters"
        if len(root) > 500:
            return "ERROR: root too long"

        workspace_root = getattr(sandbox, "root", None)
        if workspace_root is None:
            return self._shell_query(sandbox, pattern, root)

        return self._in_process_query(workspace_root, pattern, root)

    # ------------------------------------------------------------------
    # Shell fallback
    # ------------------------------------------------------------------

    def _shell_query(self, sandbox: Sandbox, pattern: str, root: str) -> str:
        # Tiny shape syntax is not shell-mapped; fall back to find for
        # literal/glob patterns only.
        if any(c in pattern for c in ("{", "}", "(", ")")):
            return "ERROR: query_tree shape syntax requires a file-view sandbox"
        quoted_root = shlex.quote(root)
        quoted = shlex.quote(pattern)
        res = sandbox.exec(
            f"find {quoted_root} -name {quoted} -not -path '*/.git/*' "
            f"-not -path '*/node_modules/*' -not -path '*/__pycache__/*'"
        )
        if res.exit_code != 0:
            return "(no matches)"
        lines = [line for line in res.stdout.splitlines() if line.strip()]
        if not lines:
            return "(no matches)"
        return "\n".join(lines)[:20000]

    # ------------------------------------------------------------------
    # In-process query
    # ------------------------------------------------------------------

    def _in_process_query(self, workspace_root: str, pattern: str, root: str = ".") -> str:
        base = os.path.join(workspace_root, root)
        try:
            real_base = os.path.realpath(base)
            real_workspace = os.path.realpath(workspace_root)
            if not (real_base == real_workspace or real_base.startswith(real_workspace + os.sep)):
                return "ERROR: root escapes workspace"
        except OSError as exc:
            return f"ERROR: cannot resolve root {root!r}: {exc}"

        if not os.path.isdir(real_base):
            return f"ERROR: root is not a directory: {root!r}"

        if pattern.startswith("{"):
            return self._match_shape(real_base, pattern, workspace_root)

        return self._match_path(real_base, pattern, workspace_root)

    # ------------------------------------------------------------------
    # Path / glob matching
    # ------------------------------------------------------------------

    def _match_path(self, base: str, pattern: str, workspace_root: str) -> str:
        ignored = _load_ignore_matcher(workspace_root)
        results: list[str] = []
        scanned = 0
        real_workspace = os.path.realpath(workspace_root)
        for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
            if ignored is not None:
                rel_dir = os.path.relpath(dirpath, workspace_root).replace(os.sep, "/")
                prefix = "" if rel_dir == "." else rel_dir + "/"
                dirnames[:] = [d for d in dirnames if not ignored(prefix + d, True)]
            # Filter symlinked directories that point outside the workspace.
            kept: list[str] = []
            for d in dirnames:
                full = os.path.join(dirpath, d)
                if os.path.islink(full):
                    real = os.path.realpath(full)
                    if not (real == real_workspace or real.startswith(real_workspace + os.sep)):
                        continue
                kept.append(d)
            dirnames[:] = kept
            for filename in filenames:
                full = os.path.join(dirpath, filename)
                try:
                    if os.path.islink(full):
                        real = os.path.realpath(full)
                        if not (real == real_workspace or real.startswith(real_workspace + os.sep)):
                            continue
                except OSError:
                    continue
                scanned += 1
                if scanned > _MAX_SCAN_FILES:
                    break
                rel = os.path.relpath(full, base)
                if ignored is not None and ignored(rel.replace(os.sep, "/"), False):
                    continue
                if self._path_matches(rel, pattern):
                    results.append(rel.replace(os.sep, "/"))
                    if len(results) >= _MAX_RESULTS:
                        break
            if len(results) >= _MAX_RESULTS or scanned > _MAX_SCAN_FILES:
                break

        truncated = ""
        if scanned > _MAX_SCAN_FILES:
            truncated = f"\n... [truncated — scanned {_MAX_SCAN_FILES} files, ceiling reached]"
        if not results:
            return "(no matches)" + truncated
        result = "\n".join(results)
        if truncated:
            result += truncated
        if len(results) >= _MAX_RESULTS:
            result += f"\n... [hit ceiling {_MAX_RESULTS} reached]"
        return result

    @staticmethod
    def _path_matches(rel: str, pattern: str) -> bool:
        # Normalize pattern separators to forward slash
        pat = pattern.replace("\\", "/")
        rel_norm = rel.replace("\\", "/")
        if "**" in pat:
            return fnmatch.fnmatch(rel_norm, pat)
        if pat.startswith("./"):
            pat = pat[2:]
        if pat.startswith("/"):
            pat = pat[1:]
        # If pattern contains a slash, match against the full relative path,
        # otherwise match against the basename for convenience.
        if "/" in pat:
            return fnmatch.fnmatch(rel_norm, pat) or fnmatch.fnmatch(rel_norm, "*" + pat)
        return fnmatch.fnmatch(os.path.basename(rel_norm), pat)

    # ------------------------------------------------------------------
    # Tiny shape syntax
    # ------------------------------------------------------------------

    def _match_shape(self, base: str, pattern: str, workspace_root: str) -> str:
        # Supported forms:
        #   {dir}         - list immediate children of dir
        #   {dir/*}       - immediate children (files + dirs), with "/" markers
        #   {dir/*.ext}   - files under dir matching a glob
        #   {dir/**/pat}  - recursive glob under dir
        #   {pkg/deep}    - literal descent into a nested directory, then
        #                   list its children
        #   {pkg/deep/x.py} - literal descent, then glob/list at the tail
        # The syntax is deliberately tiny: it is a navigation helper, not
        # a general graph pattern language.
        body = pattern[1:-1].strip() if pattern.startswith("{") and pattern.endswith("}") else pattern

        if _SHELL_META_RE.search(body) or '"' in body or "'" in body:
            return "ERROR: shape body contains unsupported characters"
        if not body:
            return "ERROR: empty tree shape"

        # Split into literal directory segments and a trailing glob/empty tail.
        segments = [seg for seg in body.replace("\\", "/").split("/") if seg]
        tail: list[str] = []
        while segments and _has_glob(segments[-1]):
            tail.insert(0, segments.pop())
        literal_dirs = segments

        if not literal_dirs:
            return "ERROR: unsupported shape form"

        # Walk the literal directories one level at a time, confined to the
        # workspace and never following symlinked dirs that escape it.
        current = os.path.realpath(base)
        real_workspace = os.path.realpath(workspace_root)
        for seg in literal_dirs:
            target = os.path.join(current, seg)
            try:
                real = os.path.realpath(target)
            except OSError as exc:
                return f"ERROR: cannot resolve shape target {seg!r}: {exc}"
            if not (real == real_workspace or real.startswith(real_workspace + os.sep)):
                return "ERROR: shape target escapes workspace"
            if not os.path.isdir(real):
                return f"ERROR: shape target is not a directory: {seg!r}"
            current = real

        # {dir} -> list immediate children with "/" markers on directories.
        if not tail:
            return self._list_children(current)

        # One glob level: {dir/*} or {dir/*.ext}
        if len(tail) == 1:
            return self._list_children(current, glob_pat=tail[0])

        # Recursive glob: {dir/**/pattern}
        if tail[0] == "**":
            return self._match_path(current, "/".join(tail[1:]), workspace_root)

        # Deeper glob like {dir/a/*.py}: enter the literal segment, then match.
        return self._match_shape(os.path.join(current, tail[0]), "{" + "/".join(tail[1:]) + "}", workspace_root)

    @staticmethod
    def _list_children(directory: str, glob_pat: str | None = None) -> str:
        try:
            entries = sorted(os.listdir(directory))
        except OSError as exc:
            return f"ERROR: cannot list {directory!r}: {exc}"
        if glob_pat is None:
            out = []
            for name in entries:
                marker = "/" if os.path.isdir(os.path.join(directory, name)) else ""
                out.append(name + marker)
            if not out:
                return "(empty directory)"
            return "\n".join(out)
        out = []
        for name in entries:
            if not fnmatch.fnmatch(name, glob_pat):
                continue
            full = os.path.join(directory, name)
            if os.path.isdir(full):
                out.append(name + "/")
            else:
                out.append(name)
        if not out:
            return "(no matches)"
        return "\n".join(out)
