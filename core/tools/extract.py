"""Bounded workspace document extraction.

One tool: extract_document reads a single file and returns a bounded
plain-text representation for common structured formats (HTML, JSON,
XML, CSV, markdown, ini/toml-ish). It is intentionally narrow: formats
that it cannot render are reported as a note, not silently dropped.
"""
from __future__ import annotations

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

from core.tools._htmltext import html_to_text as _shared_html_to_text
from core.tools.search import _SHELL_META_RE

_MAX_TEXT_BYTES = 500_000
_MAX_TEXT_CHARS = 80_000
_MAX_RESULTS = 50

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
        # Tiny bounded extraction for sandboxes with no file view.
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
    # Thin local alias over the shared reader so both extraction paths
    # cannot drift apart.
    return _shared_html_to_text(html)


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


