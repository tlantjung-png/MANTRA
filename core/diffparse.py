"""Structured parser for unified diff output.

Turns ``git diff --no-color`` (or any unified diff) into a list of
files, each with hunks of typed lines (context / added / removed) that
carry their old and new line numbers. The review surface consumes this;
nothing here touches the terminal or the filesystem.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# @@ -oldStart[,count] +newStart[,count] @@; counts are optional.
_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")


@dataclass
class DiffLine:
    """One line inside a hunk."""

    kind: str  # "ctx" | "add" | "del"
    old_no: int | None  # line number in the old file (None for added)
    new_no: int | None  # line number in the new file (None for removed)
    text: str  # content without the leading +/-/space marker


@dataclass
class Hunk:
    old_start: int
    new_start: int
    lines: list[DiffLine] = field(default_factory=list)


@dataclass
class FileDiff:
    path: str  # the new (b/) path, a/b prefix stripped
    hunks: list[Hunk] = field(default_factory=list)

    @property
    def added(self) -> int:
        return sum(1 for h in self.hunks for l in h.lines if l.kind == "add")

    @property
    def removed(self) -> int:
        return sum(1 for h in self.hunks for l in h.lines if l.kind == "del")


def _strip_prefix(path: str) -> str:
    if path.startswith("a/") or path.startswith("b/"):
        return path[2:]
    return path


def _path_from_diffgit(line: str) -> str:
    """Extract the new path from ``diff --git a/... b/...``.

    Paths with spaces arrive quoted, so a naive whitespace split would
    tear them apart.
    """
    rest = line[len("diff --git "):].strip()
    tokens: list[str] = []
    cur = ""
    quoted = False
    for ch in rest:
        if ch == '"':
            quoted = not quoted
        elif ch == " " and not quoted and cur:
            tokens.append(cur)
            cur = ""
        else:
            cur += ch
    if cur:
        tokens.append(cur)
    if len(tokens) >= 2:
        return _strip_prefix(tokens[1])
    return _strip_prefix(tokens[0]) if tokens else "?"


def parse_diff(text: str) -> list[FileDiff]:
    """Parse unified diff text into structured files and hunks."""
    files: list[FileDiff] = []
    current: FileDiff | None = None
    hunk: Hunk | None = None
    old_no = new_no = 0

    for raw in text.splitlines():
        line = raw
        if line.startswith("diff --git "):
            current = FileDiff(path=_path_from_diffgit(line))
            hunk = None
            files.append(current)
            continue
        if line.startswith("+++ ") or line.startswith("--- ") or line.startswith("index "):
            continue
        if line.startswith("@@ "):
            if current is None:
                continue
            m = _HUNK_RE.match(line)
            if not m:
                continue
            old_no = int(m.group(1))
            new_no = int(m.group(2))
            hunk = Hunk(old_start=old_no, new_start=new_no)
            current.hunks.append(hunk)
            continue
        if hunk is None:
            continue
        # Classify +/- lines first: only the spaced "+++ " / "--- " forms
        # are file headers; a content line like "++i" or "--x" is data.
        if line.startswith("+") and not line.startswith("+++ "):
            hunk.lines.append(DiffLine("add", None, new_no, line[1:]))
            new_no += 1
        elif line.startswith("-") and not line.startswith("--- "):
            hunk.lines.append(DiffLine("del", old_no, None, line[1:]))
            old_no += 1
        elif line.startswith(" ") or line == "":
            hunk.lines.append(DiffLine("ctx", old_no, new_no, line[1:] if line else ""))
            old_no += 1
            new_no += 1
        # Any other line is ignored inside a hunk (index headers, a
        # spaced "+++ "/"--- " header that somehow appears mid-hunk,
        # "\ No newline" markers).

    return [f for f in files if f.hunks]
