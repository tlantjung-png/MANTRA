"""Interactive console: session, commands, streaming. Stdlib only."""

from __future__ import annotations

import argparse
import difflib
import glob
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from typing import Any, Callable
from urllib.parse import urlparse

from core import theme
from core.config import REASONING_EFFORTS, load_config
from core.agent.loop import DEFAULT_SYSTEM_PROMPT, AgentLoop, RunResult
from core.agent.approvals import MODES, ApprovalPolicy
from core.agent.context import ContextManager
from core.agent.events import EventBus
from core.agent.exceptions import AbortError, ConfigError, HarnessError
from core.agent.keys import has_stored, mask, store as store_key, stored_keys
from core.agent.models import fetch_models, is_reasoning_model
import core.agent.sessions as sessions
import core.agent.skills as skills
import core.agent.workflows as workflows
from core.agent.settings import (
    active as get_active,
    add_endpoint,
    endpoint_name_for_url,
    endpoints as known_endpoints,
    models_for,
    remove_endpoint,
    set_active,
    set_models,
    set_skills_prefs,
    settings_path,
    skills_prefs,
    validate_endpoint,
)
from core.agent.knowledge import (
    append_memory,
    assemble_system_prompt,
    find_instructions_file,
    render_environment,
)
from core.evaluators import NullEvaluator
from core.logs import JsonlLogger
from core.sandbox import LocalSandbox
from core.tui.overlays import Option
from core.tui.composer import Completion
from core.registry import build_llm, build_tools

from core.term import (
    enable_vt,
    force_utf8_output,
    safe_write,
    selection_in_progress,
    term_size,
    visible_len,
)  # shared wide-aware impl
from core.tui.transcript import wrap_ansi  # styled-aware wrap for table cells

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _resolve_data_path(*parts: str) -> str:
    """Locate a bundled data file in a source tree or an installed wheel.

    Installed wheels place data-files under ``sys.prefix`` while the code
    sits in site-packages, so a single PROJECT_ROOT-relative path would
    miss them (installed console could not start with its default config).
    """
    candidates = [
        os.path.join(PROJECT_ROOT, *parts),
        os.path.join(os.path.dirname(PROJECT_ROOT), *parts),
        os.path.join(sys.prefix, *parts),
    ]
    for cand in candidates:
        if os.path.isfile(cand):
            return cand
    return candidates[0]


KNOWN_FAILURES_PATH = _resolve_data_path("knowledge", "known-failures.md")

HELP_TEXT = """Commands:
  /model                provider & model — add endpoint, pick a model
  /model key [name]     replace stored key
  /fix                  send the last failure to the agent for a fix
  /sessions             saved conversations — browse and resume
  /sessions <name>      resume that session directly
  /help                 show help
  /workspace            show workspace path + files
  /memory               show memory file
  /diff                 show uncommitted changes
  /undo                 discard changes (confirm)
  /approve              set approval mode (default|auto|yolo|plan)
  /cost                 show token usage
  /compact              summarise conversation
  /clear                clear conversation (/reset is an alias)
  /goal <text>          set session goal (/goal note, /goal done)
  /todo                 session checklist — /todo add|done|rm|clear
   /skills <name>        attach skill — /skills + space, Tab filter
  /workflow             run workflow (create|show|launch|remove)
  /verbose              toggle verbose
  /exit                 exit (Ctrl+C)
  /                     same as /help

Reference files with @ in any message:
  explain @src/app.py
  why is @tests/test_smoke.py failing?
  review @src/*.py
  what's in @docs/

@path attaches a file's contents, a directory listing, or every file a glob
matches. Paths are relative to the workspace and cannot escape it.

Anything else you type is sent to the agent as a task.
Ctrl+C once stops the current run; twice leaves the console.
Escape also stops the current run while it is streaming."""


# ---------------------------------------------------------------- ANSI styling

class Style:
    """ANSI wrappers; disabled entirely under --plain."""

    def __init__(self, enabled: bool = True) -> None:
        if enabled and os.name == "nt":
            enable_vt()
        self.enabled = enabled

    def _wrap(self, code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.enabled else text

    # SGR primitives still used directly (weight / strikethrough). Every
    # colour goes through the semantic palette below instead.
    def bold(self, t): return self._wrap("1", t)
    def dim(self, t): return self._wrap("2", t)
    def strike(self, t): return self._wrap("9", t)

    # Blood & Bone semantic palette — monochrome stone base, one muted
    # crimson accent. Only the wrappers the console actually calls are
    # kept; everything else goes through _wrap(theme.*) directly.
    def brand(self, t):    return self._wrap(theme.BLOOD_BOLD, t)  # identity: ENCHANTER, wordmark
    def selected(self, t): return self._wrap(theme.BLOOD_BOLD, t)  # menu / completion highlight
    def warn(self, t):     return self._wrap(theme.WARN, t)        # warnings — soft ochre
    def ember(self, t):    return self._wrap(theme.EMBER, t)       # errors — dusty red
    def bone(self, t):     return self._wrap(theme.BONE, t)
    def ash(self, t):      return self._wrap(theme.ASH, t)
    def hair(self, t):     return self._wrap(theme.HAIR, t)




# ANSI escape sequence pattern for sanitisation.
_ANSI_SANITIZE_RE = re.compile(r"\x1b(?:\[[0-9;?]*[ -/]*[@-~]|\][^\x07]*\x07|\].*?\x1b\\)")


def _sanitize_output(text: str) -> str:
    """Strip raw ANSI escape sequences from model output.

    Prevents cursor movement, color changes, or other terminal
    manipulation from untrusted model-generated text.
    """
    return _ANSI_SANITIZE_RE.sub("", text)


def _safe_stdout(text: str) -> None:
    """Write model-generated text that must not crash on a narrow console.

    Windows consoles default to a single-byte codepage (cp1252), and a
    model reply can legitimately contain characters it cannot encode
    (e.g. U+2192). The plain ``sys.stdout.write`` used in the streaming
    paths raises UnicodeEncodeError on such text and kills the whole
    console mid-turn, so the fallback re-encodes with ``errors="replace"``
    exactly like :meth:`ConsoleSession._print` does.
    """
    try:
        sys.stdout.write(text)
    except UnicodeEncodeError:
        enc = sys.stdout.encoding or "utf-8"
        sys.stdout.write(
            text.encode(enc, errors="replace").decode(enc, errors="replace")
        )


class StreamingRenderer:
    """Apply inline markdown formatting to streamed text fragments.

    Buffers incoming text until a newline arrives, then processes the
    complete line through the full markdown pipeline.  Code fences are
    tracked across pieces so content inside them stays literal.

    ``report_hook`` is an optional callback the session sets while a turn
    streams: called with each ``TODO ADD:`` / ``TODO DONE:`` line the
    agent emits (outside code fences) and expected to return the styled
    line to show instead of the raw protocol text. Without a hook the
    renderer behaves exactly as before.
    """

    # A report line is a whole line beginning with the marker, so a
    # mid-paragraph mention of "TODO ADD" in prose is never misread.
    _REPORT_RE = re.compile(r"^(TODO (?:ADD|DONE)):\s*(.*)$", re.IGNORECASE)

    def __init__(self, style: Style) -> None:
        self.style = style
        self._in_code_fence = False
        self._table_rows: list[str] = []
        self._buf: str = ""
        self.report_hook = None  # callable(line: str) -> styled replacement

    def reset(self) -> None:
        """Reset state for a new response."""
        self._in_code_fence = False
        self._table_rows = []
        self._buf = ""

    def _flush_table(self) -> str:
        """Render buffered table rows and clear the buffer."""
        lines = self._table_rows
        self._table_rows = []
        if not lines:
            return ""
        return _flush_table_lines(lines, self.style)

    def _line_out(self, line: str) -> str:
        """One complete line: TODO reports via the hook, everything else markdown."""
        if not self._in_code_fence and self.report_hook is not None:
            m = self._REPORT_RE.match(line.strip())
            if m:
                replaced = self.report_hook(line.strip())
                if replaced:
                    return replaced
        return _render_md_line(line, self.style, self)

    def render_piece(self, piece: str) -> str:
        """Render a text fragment with inline markdown."""
        self._buf += _sanitize_output(piece)
        # Bound single-line growth without newlines to avoid unbounded
        # memory. Render the chunk but do not inject a line break: a
        # forced break can split an inline-code span, a bold marker or a
        # partial fence opener and desync the fence state machine for the
        # rest of the stream. The caller clips overflow at flush time.
        if len(self._buf) > 30000 and "\n" not in self._buf:
            chunk = self._buf[:20000]
            self._buf = self._buf[20000:]
            return _render_md_line(chunk, self.style, self)
        if len(self._buf) > 50000:
            self._buf = self._buf[-50000:]
        out = ""
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            rendered = self._line_out(line)
            if rendered:
                out += rendered + "\n"
            elif not line.strip():
                # A genuinely blank source line keeps its blank line;
                # a buffered table row renders nothing until the block
                # completes, so it must not emit one.
                out += "\n"
        return out

    def flush(self) -> str:
        """Flush any remaining buffered text (called at end of stream)."""
        out = ""
        if self._table_rows:
            out = self._flush_table()
        if self._buf:
            leftover = self._buf
            self._buf = ""
            if out:
                out += "\n"
            return out + self._line_out(leftover)
        return out
# ------------------------------------------------------------- markdown-lite

_TABLE_COL_CAP = 40
_TABLE_LAST_COL_CAP = 64
_TABLE_ROW_CAP = 200


def _is_table_row(line: str) -> bool:
    """True for a markdown table row: '| a | b |'."""
    s = line.strip()
    return s.startswith("|") and s.endswith("|") and "|" in s[1:-1]


def _is_table_sep(line: str) -> bool:
    """True for the markdown table separator row: '|---|---|'."""
    s = line.strip()
    if not (s.startswith("|") and s.endswith("|")):
        return False
    return bool(re.fullmatch(r"[\s:\-|]+", s[1:-1])) and "-" in s


def _table_cells(line: str) -> list[str]:
    """Split a table row into trimmed cells."""
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _cell_is_sep(cell: str) -> bool:
    """True when a cell is a column rule like '---' or ':---:'."""
    return bool(re.fullmatch(r":?-{2,}:?", cell.strip()))


def _strip_inline_html(text: str) -> str:
    """Drop the inline HTML and entities the model emits in prose and cells."""
    text = re.sub(r"(?i)<\s*br\s*/?\s*>", " ", text)
    text = re.sub(
        r"(?i)</?(?:b|i|em|strong|code|pre|u|s|p|h[1-6]|span|div|ul|ol|li|"
        r"table|thead|tbody|tr|td|th|blockquote|a)\b[^>]*>",
        "",
        text,
    )
    return (
        text.replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&amp;", "&")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
    )


def _render_table(rows: list[list[str]], style: Style) -> str:
    """Render a markdown table as an aligned grid.

    The header is bone-bold with a hairline rule; body cells keep the
    default face. Cells wrap inside their column; the last column keeps
    more room because report tables carry their long text there.
    """
    ncols = max(len(r) for r in rows)
    has_rule = (
        len(rows) > 1
        and len(rows[1]) >= 2
        and all(_cell_is_sep(c) for c in rows[1])
    )
    header = rows[0]
    body = rows[2:] if has_rule else rows[1:]
    all_rows = [header] + body

    widths: list[int] = []
    for c in range(ncols):
        vals = [r[c] for r in all_rows if c < len(r) and r[c].strip()]
        # Measure the STYLED cell: **bold** and `code` markers are
        # consumed by _inline_md, so raw-cell widths inflate the column
        # and misalign continuation lines (the pipe column shifts).
        longest = max(
            [visible_len(_inline_md(_strip_inline_html(v), style)) for v in vals] or [0]
        )
        cap = _TABLE_LAST_COL_CAP if c == ncols - 1 else _TABLE_COL_CAP
        widths.append(max(1, min(cap, longest)))
    # Keep the grid inside the terminal (or a sane default when piped):
    # a wider row breaks at the hard wrap and destroys the alignment.
    try:
        term_cols = term_size()[0]
    except Exception:
        term_cols = 80
    budget = max(60, min(100, term_cols - 4)) - 3 * (ncols - 1)
    while sum(widths) > budget:
        widest = max(range(ncols), key=lambda c: widths[c])
        if widths[widest] <= 8:
            break
        widths[widest] -= 1

    def _row_lines(cells: list[str], bold: bool) -> list[str]:
        wrapped: list[list[str]] = []
        for c in range(ncols):
            v = cells[c] if c < len(cells) else ""
            # Wrap the STYLED cell (markers consumed) so **bold** spans
            # never split across rows and continuation lines re-carry
            # the open SGR.
            styled = _inline_md(_strip_inline_html(v), style)
            if bold and "\x1b[1" not in styled:
                styled = style._wrap(theme.BONE_BOLD, styled)
            wrapped.append(wrap_ansi(styled, widths[c]))
        height = max(len(w) for w in wrapped)
        out: list[str] = []
        for li in range(height):
            parts: list[str] = []
            for c in range(ncols):
                line = wrapped[c][li] if li < len(wrapped[c]) else ""
                parts.append(line + " " * (widths[c] - visible_len(line)))
            pipe = style._wrap(theme.HAIR, "│")
            out.append(parts[0] + "".join(" " + pipe + " " + p for p in parts[1:]))
        return out

    lines = _row_lines(header, bold=True)
    if has_rule:
        runs = [style._wrap(theme.HAIR, "─" * w) for w in widths]
        lines.append(runs[0] + "".join(
            style._wrap(theme.HAIR, "─┼─") + r for r in runs[1:]
        ))
    for row in body:
        lines.extend(_row_lines(row, bold=False))
    return "\n".join(lines)


def _flush_table_lines(lines: list[str], style: Style) -> str:
    """Render buffered table-looking lines: a grid when the shape holds,
    otherwise the plain paragraph lines they always were."""
    if len(lines) < 2 or not (
        _is_table_sep(lines[1])
        or len({len(_table_cells(l)) for l in lines}) == 1
    ):
        return "\n".join(_inline_md(l, style) for l in lines)
    return _render_table([_table_cells(l) for l in lines], style)


def _syntax_highlight(line: str, style: Style) -> str:
    """Generic syntax highlight for any language — Blood & Bone: muted sage
    strings, bold-bone keywords, stone numerals. Single-pass to avoid ANSI
    nesting; types stay in the default face."""
    import re as _re
    # Combined pattern with named groups — one pass, no re-highlight of inserted ANSI
    pattern = _re.compile(
        r'(?P<str>\"(?:\\.|[^\"\\])*\"|\'(?:\\.|[^\'\\])*\'|`(?:\\.|[^`\\])*`)|'
        r'(?P<cmt>#.*|//.*|--.*|/\*.*?\*/)|'
        r'(?P<num>\b\d+(?:\.\d+)?\b)|'
        r'(?P<kw>\b(?:import|from|as|def|class|return|if|elif|else|for|while|try|except|with|async|await|function|const|let|var|export|require|include|using|namespace|public|private|protected|static|void|int|string|bool|float|double|struct|enum|implements|extends|new|this|super|self)\b)|'
        r'(?P<typ>\b[A-Z][a-zA-Z0-9_]+\b)'
    )
    def _repl(m):
        if m.group('str'):
            return style._wrap(theme.SAGE, m.group('str'))
        if m.group('cmt'):
            return style._wrap(theme.FAINT, m.group('cmt'))
        if m.group('num'):
            return style._wrap(theme.ASH, m.group('num'))
        if m.group('kw'):
            return style._wrap(theme.BONE_BOLD, m.group('kw'))
        # CapWords types stay default: weight plus the tones above already
        # give code its structure, colouring every type would raise noise.
        return m.group(0)
    return pattern.sub(_repl, line)

def _render_md_line(line: str, style: Style, ctx: Any = None) -> str:
    """Render a single markdown line to ANSI-styled text.

    *ctx* is an optional StreamingRenderer used only for code-fence
    state tracking during streaming.
    """
    in_fence = getattr(ctx, "_in_code_fence", False)
    stripped = line.strip()

    # A table block ending right where a fence starts must flush before
    # the fence branches consume the line.
    if ctx is not None and ctx._table_rows and (in_fence or stripped.startswith("```")):
        out = ctx._flush_table()
        return (out + "\n" + _render_md_line(line, style, ctx)) if out else _render_md_line(line, style, ctx)

    if in_fence:
        if stripped.startswith("```"):
            if ctx is not None:
                ctx._in_code_fence = False
            return style._wrap(theme.HAIR, "│" + "─" * 4)
        return _syntax_highlight(line, style)
    if stripped.startswith("```"):
        if ctx is not None:
            ctx._in_code_fence = True
        return style._wrap(theme.HAIR, "│" + "─" * 4)

    # Consecutive table rows buffer until the block ends so the grid can
    # be aligned; the separator on line two confirms the shape.
    if ctx is not None and _is_table_row(line):
        if len(ctx._table_rows) >= _TABLE_ROW_CAP:
            out = ctx._flush_table()
            ctx._table_rows.append(line)
            return out
        ctx._table_rows.append(line)
        return ""
    if ctx is not None and ctx._table_rows:
        out = ctx._flush_table()
        return (out + "\n" + _render_md_line(line, style, ctx)) if out else _render_md_line(line, style, ctx)

    # headings: bone bold; hairline under H1 and H2, ash for H3
    if stripped.startswith("#"):
        level = len(stripped) - len(stripped.lstrip("#"))
        heading = stripped.lstrip("# ").rstrip()
        if level == 1:
            return style._wrap(theme.BONE_BOLD, heading) + chr(10) + style._wrap(theme.HAIR, chr(0x2500) * 40)
        if level == 2:
            return style._wrap(theme.BONE_BOLD, heading) + chr(10) + style._wrap(theme.HAIR, chr(0x2500) * 40)
        return style._wrap(theme.BONE_BOLD, heading)

    # horizontal rule: hairline
    if stripped in ("---", "***", "___") and len(stripped) >= 3:
        return style._wrap(theme.HAIR, chr(0x2500) * 40)

    # blockquote: hairline bar, ash text
    if stripped.startswith(">"):
        quote = stripped[1:].lstrip()
        return style._wrap(theme.HAIR, "│ ") + style._wrap(theme.ASH, quote)

    # plain code outside a fence still gets highlighted
    if re.match(r"^\s*(import\s|from\s|def\s|class\s|if\s|for\s|while\s|return\b|const\s|let\s|var\s|export\s|require\(|#include|using\s|public\s|private\s|protected\s)", line):
        return _syntax_highlight(line, style)

    # unordered list: ash markers
    m_list = re.match(r"^(\s*)[-*+]\s+(.*)", line)
    if m_list:
        indent, rest = m_list.group(1), m_list.group(2)
        return indent + style._wrap(theme.ASH, "* ") + _inline_md(rest, style)

    # ordered list: ash markers
    m_ord = re.match(r"^(\s*)(\d+)[.)]\s+(.*)", line)
    if m_ord:
        indent, num, rest = m_ord.group(1), m_ord.group(2), m_ord.group(3)
        return indent + style._wrap(theme.ASH, num + ". ") + _inline_md(rest, style)

    # normal paragraph
    return _inline_md(line, style)


def render_markdown(text: str, style: Style) -> str:
    """Render Markdown to styled terminal output — Blood & Bone palette."""
    out_lines = []
    in_fence = False
    table_buf: list[str] = []

    def _flush_table_buf() -> None:
        nonlocal table_buf
        if not table_buf:
            return
        lines = table_buf
        table_buf = []
        out_lines.append(_flush_table_lines(lines, style))

    for line in text.split("\n"):
        stripped = line.strip()
        # Code fence toggle — hairline border.
        if stripped.startswith("```"):
            _flush_table_buf()
            in_fence = not in_fence
            out_lines.append(style._wrap(theme.HAIR, "│" + "─" * 4))
            continue
        if in_fence:
            out_lines.append(_syntax_highlight(line, style))
            continue
        # Tables — buffered so the grid aligns when the block ends.
        if _is_table_row(line):
            table_buf.append(line)
            if len(table_buf) > _TABLE_ROW_CAP:
                _flush_table_buf()
            continue
        _flush_table_buf()
        # Headings — bone bold; hairline under H1 and H2.
        if stripped.startswith("#"):
            level = len(stripped) - len(stripped.lstrip("#"))
            heading = stripped.lstrip("# ").rstrip()
            out_lines.append(style._wrap(theme.BONE_BOLD, heading))
            if level <= 2:
                out_lines.append(style._wrap(theme.HAIR, chr(0x2500) * 40))
            continue
        # Horizontal rule — hairline.
        if stripped in ("---", "***", "___") and len(stripped) >= 3:
            out_lines.append(style._wrap(theme.HAIR, chr(0x2500) * 40))
            continue
        # Blockquote — hairline bar, ash text.
        if stripped.startswith(">"):
            quote = stripped[1:].lstrip()
            out_lines.append(style._wrap(theme.HAIR, "│ ") + style._wrap(theme.ASH, quote))
            continue
        # Unordered list — ash markers.
        m_list = re.match(r"^(\s*)[-*+]\s+(.*)", line)
        if m_list:
            indent, rest = m_list.group(1), m_list.group(2)
            out_lines.append(indent + style._wrap(theme.ASH, "* ") + _inline_md(rest, style))
            continue
        # Ordered list — ash markers.
        m_ord = re.match(r"^(\s*)(\d+)[.)]\s+(.*)", line)
        if m_ord:
            indent, num, rest = m_ord.group(1), m_ord.group(2), m_ord.group(3)
            out_lines.append(indent + style._wrap(theme.ASH, num + ". ") + _inline_md(rest, style))
            continue
        # Plain code outside a fence still gets highlighted
        if re.match(r"^\s*(import\s|from\s|def\s|class\s|if\s|for\s|while\s|return\b|const\s|let\s|var\s|export\s|require\(|#include|using\s|public\s|private\s|protected\s)", line):
            out_lines.append(_syntax_highlight(line, style))
            continue
        # Normal paragraph.
        out_lines.append(_inline_md(line, style))
    _flush_table_buf()
    return "\n".join(out_lines)


def _inline_md(line: str, style: Style) -> str:
    """Inline markdown: code, bold, italic, strikethrough, links — Blood & Bone."""
    import re as _re
    line = _strip_inline_html(line)
    # Sentinel tokens shield escaped backticks and asterisks from the
    # splitters below, and are restored afterwards.
    _ESC = "\x00ESC_BT\x00"
    _ESC_STAR = "\x00ESC_ST\x00"
    _CS = "\x00ESC_CS%d\x00"
    line_esc = line.replace("\\`", _ESC)
    parts = line_esc.split("`")
    # Protect inline-code spans with sentinels first so formatting
    # markers (bold, italic) can span across them - "**a `b` c**" must
    # render as bold with a styled code span, not leave literal **
    # behind. Each span is restored in sage afterwards.
    spans: list[str] = []
    rebuilt: list[str] = []
    for i, part in enumerate(parts):
        if i % 2 == 1:
            spans.append(part)
            rebuilt.append(_CS % len(spans))
        else:
            rebuilt.append(part)
    segment = "".join(rebuilt).replace(_ESC, "`")

    # Images collapse to their alt text: a badge cannot render in
    # a terminal, and leaving raw ![...](...) syntax makes badge
    # lines read as broken markdown (and would mangle the outer
    # link around an image badge).
    segment = _re.sub(
        r'!\[([^\]]*)\]\([^)]*\)',
        lambda m: m.group(1),
        segment,
    )
    # Links — the crimson accent (interactive affordance).
    segment = _re.sub(
        r'\[([^\]]+)\]\([^)]+\)',
        lambda m: style._wrap(theme.LINK, m.group(1).replace("\\[", "[").replace("\\]", "]")),
        segment,
    )
    # Bold — strong bone.
    segment = _re.sub(r'\*\*(.+?)\*\*', lambda m: style._wrap(theme.BONE_BOLD, m.group(1)), segment)
    # Italic — ash.
    segment_esc_star = segment.replace("\\*", _ESC_STAR)
    segment_esc_star = _re.sub(r'(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)', lambda m: style._wrap(theme.ASH_ITAL, m.group(1)), segment_esc_star)
    segment = segment_esc_star.replace(_ESC_STAR, "*")
    # Strikethrough.
    segment = _re.sub(r'~~(.+?)~~', lambda m: style.strike(m.group(1)), segment)
    # Restore the protected code spans in sage.
    for i, span in enumerate(spans):
        segment = segment.replace(_CS % (i + 1), style._wrap(theme.SAGE, span))
    return segment.replace(_ESC, "`")


# @name mentions: a bare word, path, or glob. The lookbehind keeps
# "user@example.com" and "a@b" from being read as file references.
# Trailing punctuation like .,;:!?) is stripped later so "@src/app.py." at
# the end of a sentence still resolves to the file.
MENTION_RE = re.compile(r"(?<![\w])@([A-Za-z0-9_][\w.:/\\\-*]*)")
# Characters that are often trailing punctuation after a mention and
# should not be considered part of the path.
_MENTION_TRIM = ".,;:!?)]'\"`"
MAX_ATTACH_CHARS = 20_000
MAX_TOTAL_ATTACH_CHARS = 60_000
MAX_GLOB_HITS = 20
MAX_LISTING_ENTRIES = 100


def _short(count: int) -> str:
    """1234 -> 1.2k. Token counts only ever need two significant figures."""
    if count < 1000:
        return str(count)
    if count < 10_000:
        return f"{count / 1000:.1f}k"
    return f"{round(count / 1000)}k"


def _safe_int(value: Any) -> int:
    """Total from a session file as an int; corrupt values read as zero.

    Session files are hand-editable JSON, so one non-numeric total must
    cost that counter, not the whole restore command.
    """
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _format_elapsed(seconds: float) -> str:
    """Format elapsed time: '1.6s', 'done in 1m23s', 'done in 1h05m'."""
    if seconds < 60:
        return f"{seconds:.1f}s"
    if seconds < 3600:
        m = int(seconds) // 60
        s = int(seconds) % 60
        return f"done in {m}m{s:02d}s"
    h = int(seconds) // 3600
    m = (int(seconds) % 3600) // 60
    return f"done in {h}h{m:02d}m"


def _short_endpoint(base_url: str) -> str:
    """Host and path, without the scheme - what a header has room for.

    ``https://api.openai.com/v1`` is too long to sit next to a model
    name, and the ``https://`` is the part that carries no information:
    every endpoint has one.
    """
    url = (base_url or "").strip()
    for prefix in ("https://", "http://"):
        if url.lower().startswith(prefix):
            url = url[len(prefix) :]
            break
    return url.rstrip("/").removesuffix("/v1")


def _transcript(messages: list[dict[str, Any]]) -> str:
    """Flatten history to plain text for the summariser."""
    lines = []
    for message in messages:
        role = message.get("role", "?")
        content = message.get("content") or ""
        if role == "system":
            continue
        if role == "assistant" and message.get("tool_calls"):
            names = ", ".join(
                (call.get("function") or {}).get("name", "?")
                for call in message["tool_calls"]
            )
            lines.append(f"assistant: [called {names}]")
            if content:
                lines.append(f"assistant: {content}")
            continue
        if role == "tool":
            body = content if len(content) <= 300 else content[:300] + " ..."
            lines.append(f"result of {message.get('name', 'tool')}: {body}")
            continue
        lines.append(f"{role}: {content}")
    return "\n".join(lines)


# ------------------------------------------------------------------- session
class ConsoleSession:
    """One REPL session over one persistent local workspace."""

    def __init__(
        self,
        config: dict,
        workspace: str,
        style: Style,
        llm: Any = None,
        ask: Any = None,
    ) -> None:
        self.config = config
        self.style = style
        self.workspace = workspace
        self.sandbox = LocalSandbox(workspace)
        self.sandbox.setup({})
        self._isolate_git(workspace)
        self.memory_path = os.path.join(workspace, ".mantra", "memory.md")
        self.instructions_path = find_instructions_file(workspace)

        ctx_cfg = config.get("context") or {}
        self.context = ContextManager(
            max_messages=int(ctx_cfg.get("max_messages", 200)),
            max_chars=int(ctx_cfg.get("max_chars", 240_000)),
        )
        env = render_environment(workspace)
        # Preload workspace map so model is not blind before user asks
        try:
            entries = os.listdir(workspace)[:50]
            files = []
            for e in sorted(entries)[:35]:
                p = os.path.join(workspace, e)
                files.append(f"{e}/" if os.path.isdir(p) else e)
            env += f"\n- workspace files: {', '.join(files) or '(empty)'}"
            for hint in ("README.md", "package.json", "pyproject.toml", "AGENTS.md", "main.py", "index.html"):
                if hint in entries:
                    env += f"\n- has {hint}"
            # Inject README head so model can answer "what does this project do" without tool call
            for readme in ("README.md", "readme.md"):
                rp = os.path.join(workspace, readme)
                if os.path.isfile(rp):
                    try:
                        with open(rp, "r", encoding="utf-8", errors="replace") as f:
                            head = f.read(2500).strip().replace("\r", "")
                        if head:
                            # Keep first ~400 chars of README as context
                            snippet = head[:500].replace("\n", " ")
                            env += f"\n- README: {snippet[:400]}"
                        break
                    except Exception:
                        pass
        except Exception:
            pass
        self.system_prompt = assemble_system_prompt(
            config.get("system_prompt") or DEFAULT_SYSTEM_PROMPT,
            known_failures_path=KNOWN_FAILURES_PATH,
            memory_path=self.memory_path,
            instructions_path=self.instructions_path,
            environment=env,
        )
        self.tools = build_tools(config["tools"])
        self.llm = llm if llm is not None else build_llm(config["llm"])
        self.approvals = ApprovalPolicy(
            mode=config.get("approvals", "default"),
            ask=ask or self._ask,
            note=self._note,
        )
        self.totals = {"tokens_in": 0, "tokens_out": 0, "turns": 0, "tool_errors": 0, "cache_hit": 0}
        self.reported_changes: set[str] = set()
        # Model ids discovered from the endpoint, so `/model <tab>` can
        # complete from what is actually served rather than a guess.
        self.known_models: list[str] = []
        # Last tool calls; retained for compatibility, not read by the UI.
        self.recent_tools: list[str] = []
        # Per-turn cache metrics for trend analysis.
        self.turn_history: list[dict] = []  # [{turn, tokens_in, tokens_out, cache_hit, cache_rate}]

        log_path = config["logging"].get("path", "logs/mantra-console.jsonl")
        if not os.path.isabs(log_path):
            log_path = os.path.join(PROJECT_ROOT, log_path)
        self.logger = JsonlLogger(log_path)
        self.bus = EventBus()
        self.bus.subscribe(self._on_event)

        self.message_count = 0
        self.verbose = bool(config.get("verbose", False))
        self.max_steps = int(config.get("max_steps", 30))
        # Set the first time autosave writes, so a session that never got
        # anywhere leaves no file behind. Adopted by /sessions so picking a
        # session up continues it instead of forking it.
        self.session_name = ""
        # The standing objective, if the operator set one. Injected into
        # every turn's system prompt, so an agent working across many
        # turns keeps aiming at the same thing instead of drifting to
        # whatever the last message asked for.
        self.goal = ""
        # Free-form notes the operator attached to the goal with
        # /goal note <text>: constraints found along the way, decisions
        # made. Shown with the goal so they are not re-litigated.
        self.goal_notes: list[str] = []
        # The session todo checklist (/todo). Discrete items the operator
        # wants done, each kept open or done. Injected into every turn's
        # system prompt like the goal, so an agent working over many
        # turns can see what remains rather than losing the thread of a
        # multi-part request.
        self.todos: list[dict] = []  # [{"text": str, "done": bool}]
        # Reports the agent emitted on the turn in flight, already applied
        # inline as its reply streamed. The end-of-turn pass skips them so
        # nothing is added, checked, or announced twice.
        self._turn_todo_reports: list[tuple[str, str]] = []  # [(kind, normalised_text)]
        # Skills attached with /skills use <name>. Their procedures ride
        # along in the system prompt so the agent follows them rather
        # than improvising, which is the whole point of a skill existing.
        self.active_skills: list[str] = []
        # Skills the router attached on its own for the turn in flight.
        # Detached the moment that turn ends, because routing reads one
        # request - leaving its guess attached would mean every later
        # turn inherits a procedure nobody asked for, and would stop the
        # router ever looking again.
        self.auto_attached: list[str] = []
        self.last_error: str | None = None  # most recent failure, for /fix
        # True while a bundle is running its steps. A bundle step is a
        # turn like any other, but it is one the router must keep its
        # hands off: the step already knows which skill it wants.
        self.in_bundle = False

        # The terminal application (set by main for interactive use);
        # every screen-facing decision delegates to it when present.
        self.ui: Any = None
        self._streamed_this_run = False
        self._stream_header_done = False
        self._turn_started: float = 0.0
        # Live token counter: approximate tokens received during streaming.
        self._stream_tokens: int = 0
        self._last_counter_update: float = 0.0
        # Timestamp of when the last prompt was sent (Enter pressed).
        self._prompt_sent_at: float = 0.0
        # Streaming markdown renderer for inline formatting during token streaming.
        self._stream_renderer = StreamingRenderer(self.style)
        self.frame = None  # legacy shim; the terminal application owns chrome
        self.layout: Any = None
        self._compact = True
        self._splash_visible = True
        self._abort = threading.Event()
        self._prev_sigint = None
        # Keys the turn-scoped scroll reader buffered while a task streamed
        # (non-scroll input). The prompt editor drains these before touching
        # the terminal, so typing during a turn is not lost.
        self._scroll_preload: list[str] = []
        # Rendered fragments deferred while the terminal host was performing
        # a native mouse selection (Windows). Flushed once the drag ends so
        # streaming repaints never disturb the selection.
        self._deferred_stream: list[str] = []
        # Pre-edit file snapshots keyed by workspace-relative path, taken
        # at tool_call time so tool_result can render a real before/after
        # diff of what the agent changed. None means the file did not
        # exist yet (a fresh write). Cleared at the end of every turn.
        self._edit_snapshots: dict[str, str | None] = {}
        self._last_edit_path: str | None = None
        # Last run_command text / shell task id, so the observation
        # boxes that follow each step can title themselves.
        self._last_command: str | None = None
        self._last_shell_task: str | None = None
        # Ctrl+O during a run toggles whether tool-output boxes print.
        self._show_tool_output = True
        # Tool output too big for one screenful is queued here (title,
        # remaining lines) by the box renderers; an empty Enter at the
        # prompt pages through it a screenful at a time. Holds reads,
        # commands, diffs and background-task logs alike.
        self._pending_pages: list[tuple[str, list[str]]] = []
        # Parallel to _pending_pages: True when the queued lines are
        # already fully styled (diff panes) instead of raw text that the
        # pager must run through _row().
        self._pending_styled: list[bool] = []

    # Compat shims: deprecated, no-op (compact is sole TUI).

    def open_frame(self) -> bool:
        return False

    def close_frame(self, label: str = "") -> None:
        pass

    def prompt_text(self, body: str = "") -> str:
        """Prompt label — fixed at bottom when layout active."""
        if not body:
            if self.style.enabled:
                body = self.style.hair("\u2502 ") + self.style.bone("MANTRA > ")
            else:
                body = "\u2502 MANTRA > "
        if self.layout is not None and self.layout.active:
            return self.layout.prompt_text(body)
        return body

    def close_prompt(self, column: int) -> None:
        pass

    def _frame_title(self) -> str:
        return ""
    def refresh_title(self) -> None:
        return

    def _isolate_git(self, workspace: str) -> None:
        """Give the workspace its own git repo if it does not have one."""
        if os.path.isdir(os.path.join(workspace, ".git")):
            return
        try:
            subprocess.run(
                ["git", "init"], cwd=workspace,
                capture_output=True, timeout=30, check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass

    # ---- output helpers (spinner-aware) ---------------------------------

    def _print(self, text: str = "") -> None:
        if self.layout is not None and self.layout.active:
            self.layout.write(text + "\n")
            return
        try:
            sys.stdout.write(text + "\n")
        except UnicodeEncodeError:
            sys.stdout.write((text + "\n").encode(sys.stdout.encoding or "utf-8", errors="replace").decode(sys.stdout.encoding or "utf-8", errors="replace"))
        sys.stdout.flush()

    def _set_busy(self, on: bool) -> None:
        """Tell the terminal application a turn is running (spinner state)."""
        if self.ui is not None:
            self.ui.set_busy(on, label="Chanting")

    def _format_diff(self, diff_text: str, max_lines: int = 60, title: str = "") -> str:
        """Colour a unified diff inside a small box, optionally titled."""
        if not diff_text:
            return ""
        lines = diff_text.splitlines()
        total = len(lines)
        if len(lines) > max_lines:
            lines = lines[:max_lines]
            lines.append(self.style.dim(f"... ({total - max_lines} more lines)"))
        if title:
            top = self.style._wrap(theme.HAIR, "┌ ") + self.style._wrap(theme.BONE, title)
        else:
            top = self.style._wrap(theme.HAIR, "┌" + "─" * 38)
        out = [top]
        for line in lines:
            if line.startswith("+"):
                out.append(self.style._wrap(theme.HAIR, "│ ") + self.style._wrap(theme.SAGE, line))
            elif line.startswith("-"):
                out.append(self.style._wrap(theme.HAIR, "│ ") + self.style._wrap(theme.EMBER, line))
            else:
                out.append(self.style._wrap(theme.HAIR, "│ ") + self.style._wrap(theme.FAINT, line))
        out.append(self.style._wrap(theme.HAIR, "└" + "─" * 38))
        return "\n".join(out)

    def _file_text(self, rel: str) -> str | None:
        """Full text of a workspace-relative file, or None when unreadable."""
        if not rel:
            return None
        full = os.path.join(self.workspace, rel)
        try:
            if not os.path.isfile(full):
                return None
            if os.path.getsize(full) > 1_000_000:
                return None
            with open(full, "r", encoding="utf-8", errors="replace") as fh:
                return fh.read()
        except Exception:
            return None

    def _render_file_change(self, tool: str, result: str) -> str:
        """Readable result of edit_file / write_file.

        edit_file gets a coloured before/after diff against the snapshot
        taken when the tool_call arrived; write_file shows a syntax
        preview of the new file. Falls back to the tool's own message
        when the file is missing or too large to snapshot, so the
        operator always sees the file that was just edited, capped and
        wrapped instead of dumped raw.
        """
        path = self._last_edit_path or ""
        new = self._file_text(path) if path else None
        if new is None:
            if isinstance(result, str) and result.strip():
                return self._format_diff(result.strip(), max_lines=20)
            return ""
        old = self._edit_snapshots.get(path)
        if tool == "write_file" and old is None:
            # A brand-new file: syntax preview, capped so a long file
            # stays readable.
            lines = new.splitlines()
            total = len(lines)
            if total > 120:
                lines = lines[:120]
            out = [self.style._wrap(theme.HAIR, "┌ ") + self.style._wrap(theme.BONE, f"wrote {path} ({total} lines)")]
            for line in lines:
                out.append(_syntax_highlight(line, self.style))
            if total > 120:
                out.append(self.style.dim(f"  ... {total - 120} more lines"))
            out.append(self.style._wrap(theme.HAIR, "└" + "─" * 38))
            return "\n".join(out)
        body = "\n".join(
            difflib.unified_diff(
                (old or "").splitlines(),
                new.splitlines(),
                fromfile=path + " (before)",
                tofile=path + " (after)",
                lineterm="",
                n=2,
            )
        )
        if body.strip():
            pane = self._diff_pane_rows(body, max_lines=220)
            if pane is not None:
                return self._box(f"edit {path}", pane)
            return self._format_diff(body, max_lines=220, title=f"edit {path}")
        if isinstance(result, str) and result.strip():
            return self._format_diff(result.strip(), max_lines=20)
        return ""

    # Tool-output boxes are budgeted in *viewport rows*, not raw lines: one
    # long line can wrap to several screen rows, and a huge read or log dump
    # must not flood the viewport mid-run. Roughly a screenful is shown and
    # the rest is noted - never dropped silently. Reads that overflow are
    # queued and can be paged through with an empty Enter at the prompt.
    _TOOL_OUTPUT_BUDGET_ROWS = 36
    _READ_PAGE_ROWS = 24
    _OUTPUT_COLS_FALLBACK = 80

    def toggle_tool_output(self) -> None:
        """Flip whether run/read/git boxes print while the agent works."""
        self._show_tool_output = not self._show_tool_output
        state = "on" if self._show_tool_output else "off"
        self._print(self.style.dim(f"  (tool output boxes {state} — ctrl+o to toggle)"))

    def _output_cols(self) -> int:
        """Current viewport width, or a sane default when not on a TUI."""
        try:
            layout = getattr(self, "layout", None)
            cols = getattr(layout, "_cols", 0)
            if layout is not None and getattr(layout, "active", False) and cols >= 30:
                return int(cols)
        except Exception:
            pass
        return self._OUTPUT_COLS_FALLBACK

    def _row_cost(self, line: str) -> int:
        """Approx wrapped screen rows one line will occupy inside a box."""
        cols = max(1, self._output_cols())
        width = visible_len(line) + 2  # '│ ' gutter
        return max(1, (width + cols - 1) // cols)

    def _rows_of(self, lines: list[str]) -> int:
        return sum(self._row_cost(ln) for ln in lines)

    def _head_lines(self, lines: list[str], budget: int) -> tuple[list[str], int]:
        """Take from the start of a stream until the row budget is spent."""
        shown: list[str] = []
        used = 0
        for ln in lines:
            cost = self._row_cost(ln)
            if used + cost > budget and shown:
                break
            shown.append(ln)
            used += cost
        return shown, len(lines) - len(shown)

    def _box(self, title: str, body: list[str]) -> str:
        """Assemble a titled box with '│ ' gutter rows."""
        out = [self.style._wrap(theme.HAIR, "┌ ") + self.style._wrap(theme.BONE, title)]
        out.extend(body)
        out.append(self.style._wrap(theme.HAIR, "└" + "─" * 38))
        return "\n".join(out)

    def _row(self, ln: str) -> str:
        """One styled '│ ' gutter row inside a tool-output box."""
        if ln.startswith("exit_code:"):
            code = -1
            try:
                code = int(ln.split(":", 1)[1].split()[0])
            except Exception:
                pass
            color = theme.SAGE if code == 0 else theme.EMBER
            return self.style._wrap(color, "│ " + ln)
        if ln.startswith(("stdout:", "stderr:", "log:", "Note:")):
            return self.style._wrap(theme.FAINT, "│ " + ln)
        if ln.startswith("+") and not ln.startswith("+++"):
            return self.style._wrap(theme.SAGE, "│ " + ln)
        if ln.startswith("-") and not ln.startswith("---"):
            return self.style._wrap(theme.EMBER, "│ " + ln)
        if ln.startswith(("@@", "index ", "diff --git", "--- ", "+++ ")):
            return self.style._wrap(theme.FAINT, "│ " + ln)
        return "│ " + ln

    # ---- before/after diff panes ---------------------------------------
    #
    # Structured unified diffs (agent edits, /diff, git-diff boxes) render
    # as two stacked panes per hunk instead of a +/- line stream: first
    # the before-state with removed lines in a soft dusty red, then the
    # after-state with added lines in a soft sage green - text colour
    # only, no backgrounds. A context line exists on both sides, so it is
    # shown only once - in the pane whose change sits nearest - which
    # keeps each pane anchored without doubling the code.

    def _pane_row(self, body: str, code: str | None) -> str:
        """One '│ ' gutter row; *code* colours the changed text softly."""
        gutter = self.style._wrap(theme.HAIR, "│ ")
        if not code:
            return gutter + body
        return gutter + self.style._wrap(code, body)

    def _pane_chip(self, label: str) -> str:
        """A small 'old' / 'new' marker row that opens a pane."""
        return self.style._wrap(theme.HAIR, "│ ") + self.style._wrap(theme.ASH, label)

    def _diff_pane_rows(self, diff_text: str, max_lines: int = 60) -> list[str] | None:
        """Styled old/new pane rows for a unified diff.

        Returns None when *diff_text* is not a parseable unified diff (no
        hunks, or foreign content before the first hunk), so callers can
        fall back to the plain diff renderer.
        """
        groups: list[tuple[str | None, str | None, list[list[str]]]] = []
        cur: tuple[str | None, str | None, list[list[str]]] | None = None
        hunk: list[str] | None = None
        seen_hunk = False
        for ln in diff_text.splitlines():
            if ln.startswith("--- "):
                cur = [ln[4:].strip(), None, []]
                groups.append(cur)
                hunk = None
            elif ln.startswith("+++ "):
                if cur is None:
                    cur = [None, None, []]
                    groups.append(cur)
                cur[1] = ln[4:].strip()
            elif ln.startswith("@@"):
                if cur is None:
                    cur = [None, None, []]
                    groups.append(cur)
                hunk = []
                cur[2].append(hunk)
                seen_hunk = True
            elif hunk is not None and ln[:1] in (" ", "-", "+"):
                hunk.append(ln)
            elif not seen_hunk and ln and not ln.startswith(
                ("diff ", "index ", "new file", "deleted file", "old mode", "new mode", "similarity ", "rename ", "Binary ")
            ):
                # Foreign content before any hunk (command output, notes):
                # this is not a diff we should pane-ify.
                return None
        if not any(hs for _, _, hs in groups):
            return None

        def _file_base(label: str | None) -> str | None:
            """'a/src/x.py' -> 'src/x.py'; 'x.py (after)' -> 'x.py'."""
            if not label:
                return None
            base = label.replace("\\", "/")
            if base.startswith(("a/", "b/")):
                base = base[2:]
            for suffix in (" (before)", " (after)"):
                if base.endswith(suffix):
                    base = base[: -len(suffix)]
            return base or None

        rows: list[str] = []
        for old_lbl, new_lbl, hunks in groups:
            # A per-file chip before a group's first hunk. Agent-edit diffs
            # already name the file in their box title (labels end in
            # "(before)"), so only git-style output gets the chip.
            file_base = _file_base(old_lbl or new_lbl)
            git_style = not ((old_lbl or "").endswith(" (before)") and (new_lbl or "").endswith(" (after)"))
            if file_base and git_style:
                rows.append(self._pane_chip(file_base))
            for hunk_lines in hunks:
                seq: list[tuple[str, str]] = []
                for ln in hunk_lines:
                    seq.append((ln[0], ln[1:]))
                minus_idx = [i for i, (kind, _) in enumerate(seq) if kind == "-"]
                plus_idx = [i for i, (kind, _) in enumerate(seq) if kind == "+"]
                old: list[tuple[str, bool]] = []
                new: list[tuple[str, bool]] = []
                for i, (kind, body) in enumerate(seq):
                    if kind == "-":
                        old.append((body, True))
                    elif kind == "+":
                        new.append((body, True))
                    else:
                        # Context is identical on both sides: show it once,
                        # in whichever pane holds the change nearest it.
                        d_old = min((abs(i - j) for j in minus_idx), default=10**9)
                        d_new = min((abs(i - j) for j in plus_idx), default=10**9)
                        if d_old <= d_new:
                            old.append((body, False))
                        else:
                            new.append((body, False))
                if old:
                    rows.append(self._pane_chip("old"))
                    rows.extend(self._pane_row(b, theme.DIFF_REMOVE if changed else None) for b, changed in old)
                if new:
                    rows.append(self._pane_chip("new"))
                    rows.extend(self._pane_row(b, theme.DIFF_ADD if changed else None) for b, changed in new)
        total = len(rows)
        if total > max_lines:
            rows = rows[:max_lines]
            rows.append(
                self.style._wrap(theme.HAIR, "│ ")
                + self.style._wrap(theme.FAINT, f"… {total - max_lines} more diff lines")
            )
        return rows

    def _render_diff_pages(self, title: str, rows: list[str], lead: list[str] | None = None) -> str:
        """Box for pre-styled diff-pane rows, paged like other tool boxes."""
        lead_rows = [self._row(ln) for ln in (lead or [])]
        if self._rows_of(lead_rows) + self._rows_of(rows) <= self._TOOL_OUTPUT_BUDGET_ROWS:
            return self._box(title, lead_rows + rows)
        preview, _ = self._head_lines(rows, self._READ_PAGE_ROWS)
        remaining = rows[len(preview):]
        if remaining:
            self._pending_pages.append((title, remaining))
            self._pending_styled.append(True)
        shown = lead_rows + preview
        shown.append(
            self.style.dim(
                f"│ … {len(remaining)} more rows — press Enter (empty prompt) to page through the diff"
            )
        )
        return self._box(title, shown)

    def _render_paged_box(self, title: str, content: list[str], lead: list[str] | None = None) -> str:
        """Box for any tool output that may exceed a screenful.

        Fits the budget → shown whole. Overflows → a compact first page
        and the remainder queued (title + lines) for the empty-Enter
        pager, so nothing is lost and the viewport is never flooded
        mid-run - reads, commands, diffs and background logs alike.
        """
        lead_rows = lead or []
        if self._rows_of(lead_rows) + self._rows_of(content) <= self._TOOL_OUTPUT_BUDGET_ROWS:
            rows = [self._row(ln) for ln in lead_rows] + [self._row(ln) for ln in content]
            return self._box(title, rows)
        preview, _ = self._head_lines(content, self._READ_PAGE_ROWS)
        remaining = content[len(preview):]
        if remaining:
            self._pending_pages.append((title, remaining))
            self._pending_styled.append(False)
        rows = [self._row(ln) for ln in lead_rows] + [self._row(ln) for ln in preview]
        rows.append(
            self.style.dim(
                f"│ … {len(remaining)} more lines — press Enter (empty prompt) to page through the output"
            )
        )
        return self._box(title, rows)

    def _capture_last_error(self, tool: str, observation: str) -> None:
        """Keep the most recent failed tool/command result for /fix."""
        text = observation.strip()
        if text.startswith("ERROR"):
            self.last_error = f"[{tool}] {text[:2000]}"
            return
        # run_command / shell_output observations begin with an exit_code
        # line; a nonzero code is a failure worth fixing (grep's "no
        # matches" and similar notes are explicitly not errors).
        m = re.match(r"exit_code:\s*(\d+)([^\n]*)", text)
        if m:
            code = int(m.group(1))
            if code != 0 and "not an error" not in m.group(2):
                self.last_error = f"[{tool}] {text[:2000]}"

    def _fix_prompt(self, hint: str = "") -> str | None:
        """The agent prompt for the most recent failure, or None."""
        if not self.last_error:
            return None
        prompt = (
            "A tool or command failed in this workspace. Here is the failure:\n"
            f"---\n{self.last_error}\n---\n"
            "Diagnose the root cause and suggest a fix. Do NOT run any "
            "command yourself - propose the exact command for the operator "
            "to approve and run."
        )
        if hint:
            prompt += f"\nAdditional hint from the operator: {hint}"
        return prompt

    def _attention(self) -> None:
        """A soft terminal bell so a failed turn or a denial is noticed."""
        try:
            sys.stdout.write("\x07")
            sys.stdout.flush()
        except OSError:
            pass

    def _on_tool_observation(self, tool: str, observation: str, step: int) -> None:
        """Show what a tool returned while the agent works.

        Event payloads and the run log deliberately avoid carrying file
        contents or command output, so the agent loop hands display-worthy
        observations straight to the console: the operator sees the command
        output, diffs and file contents the agent sees, not just the tool
        name. Ctrl+O (typed mid-run) hides or restores these boxes.
        """
        # Remember the most recent failure for /fix, before the display
        # filter: read_file errors matter as much as command failures.
        if isinstance(observation, str) and observation.strip():
            self._capture_last_error(tool, observation)
        if not self._show_tool_output:
            return
        if not isinstance(observation, str) or not observation.strip():
            return
        try:
            if tool in ("run_command", "git_diff", "git_reset"):
                kind = {"run_command": "run_command", "git_diff": "git diff", "git_reset": "git reset"}[tool]
                shown = self._render_command_result(observation, kind=kind)
            elif tool == "shell_output":
                shown = self._render_shell_output(observation)
            else:
                # read_file observations are deliberately not boxed: the
                # STEP line names the file, and dumping whole contents
                # floods the viewport.
                return
        except Exception:
            return
        if shown:
            self._print(shown)

    def _render_command_result(self, observation: str, kind: str = "run_command") -> str:
        """Boxed transcript view of a run_command / git_diff observation."""
        raw = _sanitize_output(observation).replace("\r", "").splitlines()
        lines = [ln for ln in raw if ln.strip() not in ("<<<UNTRUSTED_TASK_OUTPUT", ">>>")]
        if not lines:
            return ""
        # Exit line and any notes ride above the first output section.
        split = len(lines)
        for i, ln in enumerate(lines):
            if ln.startswith(("stdout:", "stderr:", "log:")):
                split = i
                break
        header = lines[:split]
        body = lines[split:]
        if split == len(lines):
            # No stdout:/stderr: sections: the whole observation is
            # content (e.g. a raw git diff). Color it like a diff.
            header = []
            body = lines
        # The command text is model-controlled, so the title must not echo
        # raw escape sequences back into the terminal.
        command = _sanitize_output(
            (getattr(self, "_last_command", None) or "").replace("\r", "").replace("\n", " ")
        )
        if kind == "run_command" and command:
            title = f"$ {command}"
        else:
            title = kind
        # git diff / git reset observations that are real unified diffs
        # render as old/new panes; anything else keeps the generic box.
        if kind in ("git diff", "git reset") and body:
            pane = self._diff_pane_rows("\n".join(body), max_lines=4000)
            if pane is not None:
                return self._render_diff_pages(title, pane, lead=header or None)
        return self._render_paged_box(title, body, lead=header or None)

    def _render_shell_output(self, observation: str) -> str:
        """Boxed view of a background-task log read (shell_output)."""
        raw = _sanitize_output(observation).replace("\r", "").splitlines()
        lines = [ln for ln in raw if ln.strip() not in ("<<<UNTRUSTED_TASK_OUTPUT", ">>>")]
        if not lines:
            return ""
        task = _sanitize_output(getattr(self, "_last_shell_task", None) or "")
        title = f"shell_output {task}" if task else "shell_output"
        return self._render_paged_box(title, lines)

    def _pending_pages_snapshot(self) -> list[list]:
        """JSON-safe copy of the pager queue (title + remaining lines).

        Persisted so an interrupted paging session can resume where it
        left off. Content is raw observation text or pre-styled diff
        rows - either way it is the same data already carried in the
        session's tool messages, so no new content is written to disk.
        """
        return [[title, list(lines)] for title, lines in self._pending_pages]

    def _restore_pending_pages(self, data: dict) -> int:
        """Rebuild the pager queue from a session payload.

        Returns the number of boxes restored so the caller can surface a
        hint. Garbage entries are dropped defensively; old session files
        without the field simply leave the queue empty. Pre-styled rows
        (diff panes, detected by their ANSI escapes) restore as styled so
        the pager prints them verbatim instead of re-colouring them.
        """
        raw = data.get("pending_pages")
        restored: list[tuple[str, list[str]]] = []
        styled_flags: list[bool] = []
        if isinstance(raw, list):
            for entry in raw[:100]:
                if not isinstance(entry, (list, tuple)) or len(entry) != 2:
                    continue
                title, lines = entry
                if not isinstance(title, str) or not isinstance(lines, list):
                    continue
                content = [ln for ln in lines if isinstance(ln, str)]
                if content:
                    restored.append((title, content))
                    styled_flags.append(any(ln.startswith("\x1b") for ln in content))
        self._pending_pages = restored
        self._pending_styled = styled_flags
        return len(restored)

    def page_next(self) -> bool:
        """Print the next screenful of a queued capped box.

        Called by the REPL when Enter is pressed with an empty prompt. The
        queue holds any tool output - reads, commands, diffs, background
        logs - that overflowed a screenful during the run. Returns True
        while more pages remain.
        """
        if not self._pending_pages:
            return False
        title, content = self._pending_pages[0]
        styled = bool(self._pending_styled[0]) if self._pending_styled else False
        if not content:
            self._pending_pages.pop(0)
            if self._pending_styled:
                self._pending_styled.pop(0)
            if not self._pending_pages:
                self._print(self.style.dim(f"  (end of {title})"))
            return bool(self._pending_pages)
        chunk, _ = self._head_lines(content, self._READ_PAGE_ROWS)
        del content[: len(chunk)]
        # Diff panes are queued fully styled; raw boxes run through _row.
        rows = list(chunk) if styled else [self._row(ln) for ln in chunk]
        if content:
            rows.append(
                self.style.dim(
                    f"│ … {len(content)} more lines — Enter (empty prompt) for the next page"
                )
            )
        self._print(self._box(f"{title} (continued)", rows))
        if not content:
            # Last page of this box consumed: drop it and surface the end.
            self._pending_pages.pop(0)
            if self._pending_styled:
                self._pending_styled.pop(0)
            if not self._pending_pages:
                self._print(self.style.dim(f"  (end of {title})"))
        return bool(self._pending_pages)

    def _note(self, text: str) -> None:
        self._print(f"  {self.style.dim(text)}")

    def _on_event(self, name: str, payload: dict) -> None:
        if name == "tool_call":
            step = payload.get("step")
            tool = payload.get("tool")
            args = payload.get("args") or {}
            # Show file path for file-operation tools so the operator can
            # follow along without waiting for the full reply.
            detail = ""
            if tool in ("write_file", "edit_file", "read_file", "list_dir"):
                path = args.get("path") or args.get("directory") or ""
                if path:
                    detail = f" {self.style.dim('>')} {self.style.dim(_sanitize_output(path.upper()))}"
                    if tool in ("write_file", "edit_file"):
                        # Snapshot the file before the edit lands so the
                        # tool_result can paint a real before/after diff.
                        self._last_edit_path = path
                        self._edit_snapshots[path] = self._file_text(path)
            elif tool == "run_command":
                cmd = args.get("command") or ""
                self._last_command = cmd
                if cmd:
                    detail = f" {self.style.dim('>')} {self.style.dim(_sanitize_output(cmd[:60].upper()))}{'...' if len(cmd) > 60 else ''}"
            elif tool == "shell_output":
                task_id = args.get("task_id") or ""
                if task_id:
                    self._last_shell_task = str(task_id)
                    detail = f" {self.style.dim('>')} {self.style.dim(_sanitize_output(str(task_id).upper()))}"
            # Show the tool step line while the spinner keeps running.
            msg = f"  {self.style.dim(f'* STEP {step}')} {self.style._wrap(theme.BONE_BOLD, tool.upper())}{detail}"
            if self.layout is not None and self.layout.active:
                try:
                    self.layout.write(msg + "\n")
                    try:
                        self.layout.draw_prompt("")
                    except Exception:
                        pass
                except Exception:
                    self._print(msg)
            else:
                self._print(msg)
        elif name == "tool_denied":
            self._print(f"  {self.style._wrap(theme.EMBER, 'DENIED')} {self.style.dim(payload.get('tool','').upper())}")
        elif name == "run_error":
            self._print(f"  {self.style._wrap(theme.EMBER, '!! ' + str(payload.get('error')))}")
        elif name == "tool_result":
            tool = payload.get("tool")
            result = payload.get("result")
            ok = payload.get("ok")
            seconds = payload.get("seconds")
            # Show what changed on disk for file-edit tools: a coloured
            # before/after diff for edits, a syntax preview for newly
            # written files — the operator sees the file being edited as
            # the agent works instead of a bare OK line.
            if tool in ("edit_file", "write_file"):
                if ok and isinstance(result, str) and result.strip():
                    shown = self._render_file_change(tool, result)
                    if shown:
                        self._print(shown)
                elif self.verbose:
                    detail = self.style.dim("OK" if ok else "FAILED")
                    self._print(f"    {detail} {seconds}s")
            elif self.verbose:
                detail = self.style.dim("OK" if ok else "FAILED")
                self._print(f"    {detail} {seconds}s")

    def _on_delta(self, piece: str) -> None:
        """Streamed content fragment from the LLM client."""
        if self._abort.is_set():
            raise AbortError("interrupted by operator")
        # Approximate token count: ~4 chars per token.
        self._stream_tokens += max(1, len(piece) // 4)
        rendered = self._stream_renderer.render_piece(piece)
        if self.layout is not None and self.layout.active:
            # The terminal host owns the mouse during streaming: while a
            # native click-drag selection is in progress (Windows, app
            # -owned viewport), every repaint would disturb the drag, so
            # defer the fragment and flush everything once the selection
            # completes. The native-scrollback layout only ever repaints
            # its bottom box, so selection is never at risk there.
            if getattr(self.layout, "is_inline", False) is not True and selection_in_progress():
                self._deferred_stream.append(rendered)
                return
            self._flush_deferred_stream()
            self._stream_write(rendered)
            # Live token counter; the throttled fragment is flushed at
            # the handle() tail.
            self._update_live_counter()
        else:
            if not self._stream_header_done:
                sys.stdout.write(f"{self.style.brand('ENCHANTER')} ")
                self._stream_header_done = True
            _safe_stdout(rendered)
            sys.stdout.flush()
        self._streamed_this_run = True

    def _stream_write(self, text: str) -> None:
        """Route one rendered fragment into the compact viewport."""
        if not self._stream_header_done:
            self.layout.write(f"{self.style.brand('ENCHANTER')} ")
            self._stream_header_done = True
        self.layout.write(text)

    def _flush_deferred_stream(self) -> None:
        """Flush fragments buffered while a native selection was active."""
        if not self._deferred_stream:
            return
        buffered = self._deferred_stream
        self._deferred_stream = []
        for text in buffered:
            self._stream_write(text)
        self._streamed_this_run = True

    def _update_live_counter(self) -> None:
        """Update the live token counter in the bottom prompt row."""
        if self.layout is None or not self.layout.active:
            return
        # Throttle: only update every 100ms to avoid flicker.
        now = time.monotonic()
        if now - self._last_counter_update < 0.1:
            return
        self._last_counter_update = now
        # Short form (1.2k); the label stays bone while the live counter
        # sits faint to its right, followed by the stream rate.
        tok_str = _short(self._stream_tokens)
        elapsed = max(0.1, now - (self._turn_started or now))
        rate_str = _short(round(self._stream_tokens / elapsed))
        if self.style.enabled:
            counter = self.style._wrap(theme.FAINT, f" {tok_str} tok · {rate_str} tok/s")
            body = self.style.hair("\u2502 ") + self.style.bone("MANTRA >") + counter
        else:
            body = "\u2502 MANTRA >" + f" {tok_str} tok · {rate_str} tok/s"
        self.layout.draw_prompt(body=body)

    # ---- approvals -------------------------------------------------------

    def _ask(self, prompt: str) -> str:
        """Terminal prompt for the approval policy. Returns y / n / a."""
        self._print("")
        self._print(f"  {self.style.bold('allow?')} {prompt}")
        self._print(self.style.dim("  [y]es   [n]o   [a]lways for this session"))
        try:
            if self.frame is not None:
                # Leave the caret on an open row so the answer is typed
                # inside the frame rather than beside it.
                self.frame.prompt("  allow> ")
                answer = input().strip().lower()
            else:
                answer = input("  allow> ").strip().lower()
        except (KeyboardInterrupt, EOFError):
            return "n"
        finally:
            if self.frame is not None:
                # input() consumed the newline, so the row it was typed on
                # is already gone and cannot be closed - only forgotten.
                self.frame.abandon_row()
        if answer in ("a", "always"):
            return "a"
        if answer in ("y", "yes"):
            return "y"
        return "n"

    # ---- @ mentions -------------------------------------------------------

    def expand_mentions(self, text: str) -> tuple[str, list[str]]:
        """Turn ``@path`` tokens into real context the model can see.

        Keeps the operator's wording intact and appends an "Attached
        context" block, which is what every mainstream agent CLI does and
        what the model already understands. Unknown references are left
        alone and reported rather than silently dropped.
        """
        # Normalize full-width variants before matching so ＠ and ／ work
        text_norm = text.replace("＠", "@").replace("／", "/")
        tokens = MENTION_RE.findall(text_norm)
        if not tokens:
            # Also try finding full-width mentions directly if normal found none
            tokens = MENTION_RE.findall(text)
            if not tokens:
                return text, []

        # Normalize: a root given with forward slashes compares unequal to
        # normpath output on Windows, which made every mention "no match".
        # Realpath (not just abspath) so the containment checks below and
        # in _resolve_mention measure against the same canonical root.
        root = os.path.realpath(os.path.abspath(self.sandbox.root))
        blocks: list[str] = []
        attached: list[str] = []
        total = 0
        seen: set[str] = set()
        budget_note_shown = False

        for token in tokens:
            # Strip surrounding quotes and trailing punctuation that is
            # sentence punctuation, not part of the path.
            raw = token.strip().strip("'\"`")
            trimmed = raw.rstrip(_MENTION_TRIM)
            # Also handle "@\"src/app.py\"" style where quotes were part of token
            trimmed = trimmed.strip("'\"`")
            if not trimmed:
                continue
            if total >= MAX_TOTAL_ATTACH_CHARS:
                # Say it once, then stop scanning: one note per skipped
                # mention just spams the transcript.
                if not budget_note_shown:
                    budget_note_shown = True
                    self._note("attachment budget reached - remaining mentions skipped")
                break
            dedup_key = trimmed.lower() if os.name == "nt" else trimmed
            if dedup_key in seen:
                continue
            seen.add(dedup_key)
            # Try several variations to be forgiving
            candidates_to_try = [trimmed]
            if trimmed != token:
                candidates_to_try.append(token.strip("'\"`").rstrip(_MENTION_TRIM))
            # Also try without leading ./ if present
            if trimmed.startswith("./"):
                candidates_to_try.append(trimmed[2:])
            if trimmed.startswith(".\\"):
                candidates_to_try.append(trimmed[2:])
            paths: list[str] = []
            for cand in candidates_to_try:
                paths = self._resolve_mention(cand, root)
                if paths:
                    break
            if not paths:
                # Show the trimmed form in the note so the operator sees
                # what was actually tried, not the raw token with punctuation.
                self._note(f"no match for @{trimmed} (tried {candidates_to_try[0]!r})")
                continue
            for rel in paths:
                if total >= MAX_TOTAL_ATTACH_CHARS:
                    self._note("attachment budget reached - remaining mentions skipped")
                    break
                full = os.path.join(root, rel)
                # Re-validate at read time: the path could have been
                # swapped for a symlink since the mention was resolved.
                # Resolve again so the containment check sits as close
                # to the open as possible, then render the canonical
                # path itself.
                real = os.path.realpath(full)
                if real != root and not real.startswith(root + os.sep):
                    continue
                block = (
                    self._render_listing(rel, real)
                    if os.path.isdir(real)
                    else self._render_file(rel, real)
                )
                if not block:
                    continue
                blocks.append(block)
                attached.append(rel)
                total += len(block)

        if not blocks:
            return text, []
        return text + "\n\nAttached context:\n\n" + "\n\n".join(blocks), attached

    def _resolve_mention(self, token: str, root: str) -> list[str]:
        """Resolve one mention to workspace-relative paths. Escapes refused."""
        root = os.path.realpath(os.path.abspath(root))
        # Robust trimming: quotes and trailing punctuation, and leading ./
        token = token.strip().strip("'\"`").rstrip(_MENTION_TRIM).strip("'\"`")
        if not token:
            return []
        # Normalize separators to the host's convention before probing.
        candidate = token.replace("/", os.sep).replace("\\", os.sep)
        if "*" in token:
            # Normalize pattern for glob: use forward slashes for root_dir glob
            # which expects POSIX-style patterns on all platforms.
            cand_posix = token.replace("\\", "/").lstrip("/")
            hits = sorted(glob.glob(cand_posix, root_dir=root, recursive=True))
            valid: list[str] = []
            for hit in hits:
                full_hit = os.path.realpath(os.path.join(root, hit))
                if not (full_hit == root or full_hit.startswith(root + os.sep)):
                    continue
                if os.path.isfile(os.path.join(root, hit)):
                    valid.append(hit)
                if len(valid) >= MAX_GLOB_HITS:
                    break
            return valid
        full = os.path.realpath(os.path.join(root, candidate))
        # Never read outside the workspace, however the path was written.
        if full != root and not full.startswith(root + os.sep):
            return []
        return [os.path.relpath(full, root)] if os.path.exists(full) else []

    @staticmethod
    def _render_file(rel: str, full: str) -> str:
        try:
            with open(full, "r", encoding="utf-8", errors="replace") as handle:
                content = handle.read(MAX_ATTACH_CHARS + 1)
        except OSError:
            return ""
        truncated = len(content) > MAX_ATTACH_CHARS
        body = content[:MAX_ATTACH_CHARS].rstrip()
        if truncated:
            body += "\n* [truncated]"
        return f"* @{rel.upper()} *\n{body}"

    @staticmethod
    def _render_listing(rel: str, full: str) -> str:
        try:
            entries = sorted(os.listdir(full))[:MAX_LISTING_ENTRIES]
        except OSError:
            return ""
        lines = [f"* @{rel.upper()} ({len(entries)} entries) *"]
        for entry in entries:
            kind = "DIR " if os.path.isdir(os.path.join(full, entry)) else "FILE"
            lines.append(f"{kind} {entry}")
        return "\n".join(lines)

    # ---- interrupt handling ----------------------------------------------

    def _install_sigint(self) -> None:
        def handler(signum, frame):
            if self._abort.is_set():
                raise KeyboardInterrupt
            self._abort.set()
            self._print(self.style.dim("  (stopping after this step - ctrl+c again to quit)"))

        self._prev_sigint = signal.getsignal(signal.SIGINT)
        try:
            signal.signal(signal.SIGINT, handler)
        except (ValueError, OSError):
            self._prev_sigint = None  # not on the main thread

    def _restore_sigint(self) -> None:
        if self._prev_sigint is not None:
            try:
                signal.signal(signal.SIGINT, self._prev_sigint)
            except (ValueError, OSError):
                pass

    # ---- message handling ------------------------------------------------

    def _effective_system_prompt(self) -> str:
        """The base prompt plus whatever the session is aiming at.

        Rebuilt per turn rather than frozen at startup, because the goal
        is set and cleared while the session is running. Appended rather
        than spliced into the base so the standing instructions stay
        intact when the goal changes. Re-applies total cap after additions.
        """
        prompt = self.system_prompt
        for name in self.active_skills:
            skill = skills.get(name)
            if skill is None or not skill.body.strip():
                continue
            prompt += (
                "\n\n## Skill in force: " + skill.name
                + "\nFollow this procedure where it applies to the current task.\n\n"
                + skill.body.strip()
            )
            if skill.resources:
                prompt += "\n\nBundled with this skill: " + ", ".join(skill.resources)
        if not self.goal and not self.todos:
            # Re-apply cap even when only skills were added
            TOTAL_CAP = 20000
            if len(prompt) > TOTAL_CAP:
                prompt = prompt[:TOTAL_CAP] + "\n... [truncated — system prompt exceeded cap]"
            return prompt
        lines: list[str] = []
        if self.goal:
            lines += [
                "",
                "## Standing goal",
                "The operator set this goal for the session. It outlives any",
                "single message: work toward it on every turn, and treat the",
                "current request as a step within it rather than a replacement.",
                "",
                f"Goal: {self.goal}",
            ]
            if self.goal_notes:
                lines.append("")
                lines.append("Notes recorded while working toward it:")
                for note in self.goal_notes:
                    lines.append(f"- {note}")
            lines.append("")
            lines.append(
                "When the goal is fully met, say so plainly in your final "
                "message and start it with GOAL COMPLETE so the operator can "
                "clear it without checking by hand. Do not claim it is "
                "complete until it actually is."
            )
        if self.todos:
            lines += self._todos_prompt_lines()
        prompt = prompt + "\n" + "\n".join(lines)
        TOTAL_CAP = 20000
        if len(prompt) > TOTAL_CAP:
            prompt = prompt[:TOTAL_CAP] + "\n... [truncated — prompt exceeded cap after goal/todo injection]"
        return prompt

    def _todos_prompt_lines(self) -> list[str]:
        """The session checklist as prompt lines - every item, with its state.

        Completed items stay listed (struck through) rather than being
        pruned, because the checklist is the shared record: an agent that
        has seen an item all session should not suddenly find it gone.
        """
        open_count = sum(1 for t in self.todos if not t["done"])
        state = "all done" if not open_count else f"{open_count} open"
        lines = ["", "## Session todo list", f"The operator keeps a checklist of {state} item{'s' if open_count != 1 else ''} for this session:"]
        for item in self.todos:
            mark = " " if not item["done"] else "x"
            lines.append(f"- [{mark}] {item['text']}")
        lines += [
            "",
            "Work through the open items; they outlive any single request.",
            "Keep the list current as you work: when a task turns up real",
            "follow-up work, add it yourself by putting a line in your",
            "final message reading TODO ADD: <the follow-up, stated as a",
            "concrete task>. Only add work that genuinely remains - not",
            "steps you are about to do anyway, and not busywork.",
            "When you finish an item, report it on its own line as",
            "TODO DONE: <the item's exact text> so it is checked off.",
            "Never claim an item is done until it actually is, and never",
            "remove or edit items - the operator owns the list.",
        ]
        return lines

    def set_goal(self, text: str) -> None:
        self.goal = text.strip()
        if not self.goal:
            return
        self._print(self.style.dim(f"  goal set: {self.goal}"))
        self._print(self.style.dim("  /goal to check it · /goal done to clear it"))

    def show_goal(self) -> None:
        if not self.goal:
            self._print(self.style.dim("  no goal set - /goal <what you want done>"))
            return
        self._print(f"  {self.style.bold('goal')} {self.goal}")
        for note in self.goal_notes:
            self._print(self.style.dim(f"    · {note}"))
        if not self.goal_notes:
            self._print(self.style.dim("    (no notes - /goal note <text> to add one)"))

    def clear_goal(self, reason: str = "") -> None:
        if not self.goal:
            self._print(self.style.dim("  no goal set"))
            return
        finished = self.goal
        self.goal = ""
        self.goal_notes = []
        self._print(self.style.dim(f"  goal cleared: {finished}"))
        if reason:
            self._print(self.style.dim(f"  {reason}"))

    def add_goal_note(self, text: str) -> None:
        if not self.goal:
            self._print(self.style.dim("  set a goal first: /goal <what you want done>"))
            return
        self.goal_notes.append(text.strip())
        self._print(self.style.dim(f"  noted ({len(self.goal_notes)} on this goal)"))

    def _check_goal_completion(self, result: "RunResult | None") -> None:
        """Notice an agent that declared the goal met.

        The agent cannot clear the goal itself - only report - so a wrong
        claim costs nothing but a line the operator can ignore.
        """
        if not self.goal or result is None or not result.final_message:
            return
        if "GOAL COMPLETE" not in result.final_message.upper():
            return
        self._print(
            self.style.dim("  the agent reports the goal is met - /goal done to clear it")
        )

    # ---- todos ----------------------------------------------------------

    def _normalise_todo(self, text: str) -> str:
        return re.sub(r"\s+", " ", text.strip()).casefold()

    def _find_todo(self, query: str, open_only: bool = True) -> int | None:
        """Resolve a numbered item (1-based) or a text match to an index.

        Text must match an item's whole text (after normalising
        whitespace and case), never a fragment: a phrase that merely
        sits inside a longer item would check the wrong thing off. To
        pick among similar items or reach a done one, use the number
        shown by /todo. With ``open_only`` (the default) done items are
        never auto-selected - the operator explicitly re-numbers an item
        to reopen it. Removal passes ``open_only=False`` because a done
        item still needs to be findable to drop.
        """
        query = query.strip()
        if query.isdigit():
            index = int(query) - 1
            return index if 0 <= index < len(self.todos) else None
        target = self._normalise_todo(query)
        for index, item in enumerate(self.todos):
            if (not open_only or not item["done"]) and self._normalise_todo(item["text"]) == target:
                return index
        return None

    def add_todo(self, text: str) -> None:
        text = text.strip()
        if not text:
            self._print(self.style.dim("  usage: /todo add <what needs doing>"))
            return
        self.todos.append({"text": text, "done": False})
        self._print(f"  {self.style.brand(str(len(self.todos)) + '.')} {text}")
        self._print(self.style.dim("  /todo to see the list · /todo done <n> when it's done"))

    def show_todos(self) -> None:
        if not self.todos:
            self._print(self.style.dim("  no todos - /todo add <what needs doing>"))
            return
        for index, item in enumerate(self.todos, 1):
            marker = self.style.ash("[ ]") if not item["done"] else self.style.hair("[x]")
            body = item["text"] if not item["done"] else self.style.strike(item["text"])
            self._print(f"  {index:>2} {marker} {body}")
        open_count = sum(1 for t in self.todos if not t["done"])
        if open_count:
            self._print("")
            self._print(self.style.dim(f"  {open_count} open · /todo done <n> to check one off · /todo rm <n> to drop one"))

    def mark_todo_done(self, query: str) -> bool:
        """Mark an item done by 1-based number or text match. Returns True when found."""
        index = self._find_todo(query, open_only=True)
        if index is None:
            # Distinguish "not in the list at all" from "already done".
            existing = self._find_todo(query, open_only=False)
            if existing is not None:
                self._print(self.style.dim(f"  already done: {self.todos[existing]['text']}"))
                return False
            self._print(self.style.dim("  no open todo matches - /todo lists them"))
            return False
        item = self.todos[index]
        if item["done"]:
            self._print(self.style.dim(f"  already done: {item['text']}"))
            return False
        item["done"] = True
        self._print(self.style.dim(f"  done: {item['text']}"))
        remaining = sum(1 for t in self.todos if not t["done"])
        if not remaining:
            self._print(self.style.dim("  all todos done - /todo clear to drop the list"))
        return True

    def rm_todo(self, query: str) -> bool:
        """Drop an item by 1-based number or text match. Returns True when found."""
        index = self._find_todo(query, open_only=False)
        if index is None:
            self._print(self.style.dim("  no todo matches - /todo lists them"))
            return False
        removed = self.todos.pop(index)["text"]
        self._print(self.style.dim(f"  removed: {removed}"))
        return True

    def clear_todos(self) -> None:
        if not self.todos:
            self._print(self.style.dim("  no todos to clear"))
            return
        count = len(self.todos)
        self.todos = []
        self._print(self.style.dim(f"  cleared {count} todos"))

    def _todo_status_snippet(self) -> str:
        """Styled open-item count for the border row while a turn runs.

        Empty string when nothing is open, so the spinner row only gains
        the ``[ ] N open`` readout when the checklist actually has work
        left - and it drains live as the agent checks items off.
        """
        open_count = sum(1 for t in self.todos if not t["done"])
        if not open_count:
            return ""
        plural = "" if open_count == 1 else "s"
        return self.style.dim(f"[ ] {open_count} open item{plural}")

    def _check_todo_completion(self, result: "RunResult | None") -> None:
        """Apply the agent's TODO reports from its final message.

        Two reports, each on its own line:

        - ``TODO DONE: <text>`` checks an open item off. Matching is
          exact after normalising whitespace, so the agent echoing an
          item's text is the only thing that checks it off; a paraphrase
          does nothing and the operator can mark it with /todo done <n>.
        - ``TODO ADD: <text>`` appends a new item the agent discovered
          along the way (follow-up work a task turned up). Deduplicated
          against the list verbatim so repeated reports do not stack.

        The agent can add and complete items, but never remove or edit
        them - the operator owns the list.

        Reports already applied inline by the streaming hook are skipped
        here: their state change happened as the reply streamed and their
        note is already on screen, so the end-of-turn pass only handles
        what the stream never saw (non-streamed replies, reports whose
        line fell outside the stream path).
        """
        if result is None or not result.final_message:
            return
        # Two passes, adds first: an item added and completed in the same
        # message must check off regardless of which line came first.
        reports = []
        for line in result.final_message.splitlines():
            head, _, rest = line.partition(":")
            head = head.strip().upper()
            reported = self._normalise_todo(rest)
            if not reported:
                continue
            if head in ("TODO DONE", "TODO ADD"):
                reports.append((head, rest.strip(), reported))
        # Adds first, then completions; reports already applied inline
        # by the streaming hook are skipped inside _apply_todo_report.
        for head, text, reported in reports:
            if head != "TODO ADD":
                continue
            if self._apply_todo_report(head, text, reported):
                self._print(self.style.dim(f"  todo added ({len(self.todos)}): {text}"))
        for head, text, reported in reports:
            if head != "TODO DONE":
                continue
            if self._apply_todo_report(head, text, reported):
                self._print(self.style.dim(f"  checked off: {text}"))

    def _handle_stream_todo_report(self, line: str) -> str:
        """Stream hook: apply a TODO report the moment its line arrives.

        The raw ``TODO ADD: …`` protocol text never reaches the screen.
        Instead the change is applied to the live list and a quiet note is
        returned in its place, so the operator watches the checklist grow
        and drain inside the streamed reply rather than reading a marker
        line or waiting for the turn to end.
        """
        head, _, text = line.partition(":")
        reported = self._normalise_todo(text)
        if not reported:
            return ""
        applied = self._apply_todo_report(head.strip(), text.strip(), reported)
        if not applied:
            # Already applied inline earlier in this stream (or a done
            # item re-reported): nothing to show, swallow the line.
            return ""
        # ASCII checkboxes, dimmed: an open item carries the [ ] marker
        # (the same mark /todo shows), a finished one the [x] with
        # the text struck through - so a note reads exactly like a row of
        # the checklist, only quieter than the reply around it.
        if head.strip().upper() == "TODO DONE":
            return self.style.dim(
                self.style.hair("[x]") + " " + self.style.strike(text.strip())
            )
        open_count = sum(1 for t in self.todos if not t["done"])
        plural = "" if open_count == 1 else "s"
        return self.style.dim(
            "[ ] " + text.strip()
            + f" ({open_count} open item{plural})"
        )


    def _apply_todo_report(self, head: str, text: str, reported: str) -> bool:
        """Apply one TODO ADD / TODO DONE report. Shared by both paths.

        Returns True when a change was applied (an item added or checked
        off). Callers decide how to announce it - the stream path returns
        a styled note inline, the end-of-turn path prints after the turn.
        Either way the state change happens once: reports already applied
        inline are recorded in ``_turn_todo_reports`` so the end-of-turn
        pass never re-adds, re-checks, or re-announces them.
        """
        if (head, reported) in self._turn_todo_reports:
            return False  # already applied inline while the reply streamed
        if head == "TODO ADD":
            if any(self._normalise_todo(t["text"]) == reported for t in self.todos):
                return False  # already tracked - do not stack duplicates
            self.todos.append({"text": text, "done": False})
            self._turn_todo_reports.append(("TODO ADD", reported))
            return True
        if head == "TODO DONE":
            for item in self.todos:
                if item["done"]:
                    continue
                if self._normalise_todo(item["text"]) == reported:
                    item["done"] = True
                    self._turn_todo_reports.append(("TODO DONE", reported))
                    return True
        return False

    def auto_route(self, text: str) -> str | None:
        """Attach the skill this request is asking for, without being asked.

        Returns the name of a bundle that fits the request, or None. The
        skill is attached for this turn only; the bundle is handed back
        rather than launched, because a bundle is several turns and the
        caller has to decide whether to spend them.

        Declines when a skill is already attached: that means the
        operator chose one, or a bundle step chose one, and either way a
        deliberate choice outranks a guess.
        """
        # config.json sets the baseline; the preferences the operator
        # switched with /skills auto are stored beside their endpoints and
        # win over it, because a choice made out loud outranks a file.
        prefs = dict(self.config.get("skills") or {})
        prefs.update(skills_prefs())
        if not prefs.get("auto", True) or self.in_bundle or self.active_skills:
            return None
        if not skills.list_skills():
            # Nothing to route to, so do not pay for a scan of a directory
            # the operator has not populated on every single turn.
            return None
        found, bundle = skills.recommend(text)
        if found is not None:
            key = found.name.lower()
            self.active_skills.append(key)
            self.auto_attached.append(key)
            summary = " ".join(str(found.description).split())
            if len(summary) > 56:
                summary = summary[:53].rstrip() + "..."
            self._note(f"skill auto-attached: {found.name} — {summary}")
            _warn_untrusted_skill(self, found)
        if bundle is None:
            return None
        if prefs.get("auto_bundle", False):
            # The bundle attaches its own skill per step, so the single
            # skill routed a moment ago would only crowd it out.
            self._detach_auto()
            return bundle
        # Left as a hint rather than launched: a bundle is several turns
        # of work and starting that on a guess is not a favour.
        self._note(f"bundle '{bundle}' covers this end to end - /skills launch {bundle}")
        return None

    def _detach_auto(self) -> None:
        """Drop whatever the router attached, once the turn is over."""
        if not self.auto_attached:
            return
        self.active_skills = [s for s in self.active_skills if s not in self.auto_attached]
        self.auto_attached = []

    def handle(self, text: str) -> RunResult | None:
        self.last_error = None  # a fresh turn starts clean for /fix
        # Startup card disappears on first real work. Splash rows count as
        # no content: they live in the same viewport buffer, and counting
        # them made the very first turn look like "resumed content" — the
        # card then never left the buffer, resurfacing above the prompt
        # mid-stream and re-appearing misplaced after a resize.
        has_resumed_content = (
            self.layout is not None
            and self.layout.active
            and len(self.layout.lines) > 0
            and not getattr(self.layout, "_splash_visible", False)
        )
        if getattr(self, "_splash_visible", False) and self.layout is not None and not has_resumed_content:
            try:
                if getattr(self.layout, "_splash_visible", False):
                    self.layout.hide_splash()
            except Exception:
                pass
            self._splash_visible = False
        self.message_count += 1
        self._abort.clear()
        # The untouched request: the router must score what the operator
        # said, not the message with attached file dumps appended, and a
        # launched bundle must still see the original wording.
        request_text = text
        text, attached = self.expand_mentions(text)
        if attached:
            shown = attached[:8]
            extra = "" if len(attached) <= 8 else f" (+{len(attached) - 8} more)"
            self._note("attached: " + ", ".join(shown) + extra)
        # Routed before the turn runs, so the chosen procedure is in the
        # system prompt the agent actually reads rather than in the next
        # one, which may never come.
        bundle = self.auto_route(request_text)
        if bundle:
            return _skills_launch(self, bundle, initial_text=text)
        task = {"task_id": f"console-{self.message_count}", "problem_statement": text}

        self._auto_compact()

        loop = AgentLoop(
            llm=self.llm,
            sandbox=self.sandbox,
            tools=self.tools,
            evaluator=NullEvaluator(),
            logger=self.logger,
            events=self.bus,
            system_prompt=self._effective_system_prompt(),
            max_steps=self.max_steps,
            on_delta=self._on_delta,
            context=self.context,
            abort=self._abort,
            approver=self.approvals,
            on_tool_result=self._on_tool_observation,
        )
        self._streamed_this_run = False
        self._stream_header_done = False
        self._stream_tokens = 0
        self._last_counter_update = 0.0
        self._stream_renderer.reset()
        # Reports the agent emits this turn are applied and announced as
        # its reply streams; the list is cleared so the end-of-turn pass
        # knows what it still has to handle.
        self._turn_todo_reports = []
        self._stream_renderer.report_hook = self._handle_stream_todo_report

        self._turn_started = time.monotonic()
        # The terminal application runs this turn on its worker thread
        # and owns all input and screen updates; the session only marks
        # the busy state so the status row can show it.
        self._set_busy(True)
        result = None
        self._install_sigint()
        self._start_dashboard_refresh()
        try:
            result = loop.run(task)
        except (KeyboardInterrupt, AbortError):
            self._abort.set()
            self._print(self.style.ember("  interrupted"))
        except HarnessError as exc:
            self._print(self.style.ember(f"  !! {exc}"))
        else:
            # Flush any remaining buffered text from the streaming renderer.
            # Fragments deferred while a native selection was active must
            # land before the renderer tail or they would be lost.
            try:
                self._flush_deferred_stream()
            except Exception:
                pass
            if self._streamed_this_run:
                tail = self._stream_renderer.flush()
                if tail:
                    if self.layout is not None and self.layout.active:
                        self.layout.write(tail + "\n")
                        # Ensure throttled viewport actually renders the final tail
                        try:
                            self.layout.flush()
                        except Exception:
                            pass
                    elif self.frame is not None:
                        self.frame.write(tail)
                        self.frame.flush()
                    else:
                        _safe_stdout(tail)
                        sys.stdout.flush()
                # Streamed in full, so the reply is already on screen - do
                # not print it again. A second copy would sit below the
                # first, and because the streaming path emits raw text
                # while render_markdown would strip its marks, the two
                # would disagree with each other line for line.
                if self.frame is None:
                    if self.layout is not None and self.layout.active:
                        self.layout.write("\n")
                        try:
                            self.layout.flush()
                        except Exception:
                            pass
                    else:
                        sys.stdout.write("\n")
                        sys.stdout.flush()
                else:
                    # Ensure viewport flush even when tail was empty
                    if self.layout is not None and self.layout.active:
                        try:
                            self.layout.flush()
                        except Exception:
                            pass
            elif result is not None and result.final_message:
                # Same sanitization contract as the streaming path: model
                # text must never drive the terminal, whether it arrives
                # one fragment at a time or all at once.
                body = render_markdown(_sanitize_output(result.final_message), self.style)
                if self.frame is not None:
                    self.frame.row(body)
                else:
                    self._print(f"{self.style.brand('ENCHANTER')} {body}")
            if result is not None:
                self._record_usage(result)
                self._record_memory(task, result)
                self._report_changes()
                self._check_goal_completion(result)
                self._check_todo_completion(result)
                # Attention: a failed turn or a denied approval is the
                # one thing that must not pass silently.
                if result.stopped_reason == "error" or int(result.metrics.get("denied", 0)) > 0:
                    self._attention()
                # After the turn is fully reported, so a session saved
                # mid-turn cannot be missing the assistant's last answer.
                self.autosave()
        finally:
            self._stop_dashboard_refresh()
            self._restore_sigint()
            self._set_busy(False)
            # Last, so the skill the router chose is in force for the whole
            # turn - including the divider row - and not a moment less.
            self._detach_auto()
            # Respect a deliberate scroll: if the operator detached to
            # read earlier output, the final reply and footer are still
            # in the transcript - never yank the viewport back to the
            # tail, or scrolling after a task looks broken.
            if self.layout is not None and self.layout.active:
                try:
                    if self.layout.following:
                        self.layout.scroll_to_bottom()
                except Exception:
                    pass
            self._edit_snapshots.clear()
            self._last_edit_path = None
            self._last_command = None
            self._last_shell_task = None
            # Tell the operator capped output can be paged through with an
            # empty Enter (the queue survives until the next turn).
            if self._pending_pages:
                self._print(self.style.dim("  (capped output above — press Enter with an empty prompt to page through it)"))
            self._end_turn(result)
            # Non-streamed turns print their last lines inside the same
            # throttle window, so force one render here — without it the
            # usage footer can sit buffered and invisible until the next
            # turn happens to repaint.
            if self.layout is not None and self.layout.active:
                try:
                    self.layout.flush()
                except Exception:
                    pass
        return result

    def _end_turn(self, result: "RunResult | None") -> None:
        """Print the usage footer for the turn that just ended."""
        # Clear the live token counter so the next prompt is clean.
        self._stream_tokens = 0
        if self.frame is not None:
            if result is None:
                self.frame.divider("no result")
            else:
                self.frame.divider(self._footer(result))
            return
        if result is not None:
            self._print(f"  {self.style.dim(self._usage_line(result))}")

    def _footer(self, result: "RunResult | None") -> str:
        """One compact line describing how the turn ended."""
        if result is None:
            return "no result"
        tin = int(result.metrics.get("tokens_in", 0))
        tout = int(result.metrics.get("tokens_out", 0))
        cache = int(result.metrics.get("cache_hit", 0))
        usage = (
            f"{_short(tin)} in / {_short(tout)} out"
            if (tin or tout)
            else f"~{_short(self.context.tokens)} in context"
        )
        steps = result.steps_used
        steps_label = f"{steps} step" if steps == 1 else f"{steps} steps"
        cache_bit = f" · {_short(cache)} cached" if cache else ""
        return f"{result.stopped_reason} · {steps_label} · {usage}{cache_bit}"

    # ---- auto-refresh dashboard ------------------------------------------

    def _start_dashboard_refresh(self) -> None:
        pass
    def _stop_dashboard_refresh(self) -> None:
        pass
    def _refresh_dashboard_in_place(self) -> None:
        pass

    def _record_usage(self, result: RunResult) -> None:
        self.totals["turns"] += 1
        tin = int(result.metrics.get("tokens_in", 0))
        tout = int(result.metrics.get("tokens_out", 0))
        cache = int(result.metrics.get("cache_hit", 0))
        # Fallback to estimated tokens when provider gave no usage or all zeros.
        # This keeps /cost and the top-bar CACHE useful even when the
        # endpoint does not return usage (e.g. after stream_options downgrade).
        if tin == 0 and tout == 0:
            est_in = self.context.tokens
            est_out = max(1, len(result.final_message) // 4) if result.final_message else 0
            # If cache was reported but prompt was 0, use cache as at least part of input
            if cache > 0 and est_in == 0:
                est_in = cache
            # Only estimate if we have something to estimate from
            if est_in > 0 or est_out > 0:
                if tin == 0:
                    tin = est_in
                    result.metrics["tokens_in"] = tin
                if tout == 0:
                    tout = est_out
                    result.metrics["tokens_out"] = tout
                result.metrics["usage_estimated"] = 1
        # If cache exceeds prompt (e.g. cached report without prompt), use cache
        if cache > tin:
            tin = cache
            result.metrics["tokens_in"] = tin
        self.totals["tokens_in"] += tin
        self.totals["tokens_out"] += tout
        self.totals["tool_errors"] += int(result.metrics.get("tool_errors", 0))
        self.totals["cache_hit"] += cache
        # Record per-turn metrics for trend analysis.
        rate = min(100, cache * 100 // tin) if tin > 0 else 0
        self.turn_history.append({
            "turn": self.totals["turns"],
            "tokens_in": tin,
            "tokens_out": tout,
            "cache_hit": cache,
            "cache_rate": rate,
        })
        # Wire CACHE instantly — top bar shows hit rate without waiting for next chrome redraw
        if self.layout is not None and getattr(self.layout, "active", False):
            try:
                self.layout.draw_chrome()
            except Exception:
                pass

    def _usage_line(self, result: RunResult) -> str:
        tin = int(result.metrics.get("tokens_in", 0))
        tout = int(result.metrics.get("tokens_out", 0))
        cache = int(result.metrics.get("cache_hit", 0))
        steps = result.steps_used
        elapsed = time.monotonic() - self._turn_started
        elapsed_str = _format_elapsed(elapsed)
        cache_bit = f" · {_short(cache)} CACHED" if cache else ""
        if not tin and not tout:
            return f"{elapsed_str} · {steps} STEP{cache_bit} · CTX {_short(self.context.tokens)}"
        return (
            f"{elapsed_str} · {steps} STEP · "
            f"I/O {_short(tin)} / {_short(tout)}{cache_bit} · CTX {_short(self.context.tokens)}"
        )

    def _record_memory(self, task: dict, result: RunResult) -> None:
        final = (result.final_message or "").strip().replace("\n", " ")[:300]
        ok = append_memory(
            self.memory_path,
            f"- {time.strftime('%Y-%m-%d %H:%M')} | {task['task_id']} | "
            f"{result.stopped_reason}: {final}",
        )
        if not ok:
            self._print("(memory write skipped: store busy or unwritable)")

    def _report_changes(self) -> None:
        """Announce files the agent touched, newest first, once each."""
        changed = getattr(self.sandbox, "changed", set()) or set()
        fresh = sorted(changed - self.reported_changes)
        if not fresh:
            return
        self.reported_changes.update(fresh)
        shown = fresh[:8]
        more = "" if len(fresh) <= 8 else f" (+{len(fresh) - 8} more)"
        self._print(f"  {self.style.dim('changed: ' + ', '.join(shown) + more)}")

    # ---- context management ----------------------------------------------

    def _auto_compact(self) -> None:
        # merge_defaults rejects non-integer values, but stay defensive:
        # a bad value must disable compaction, not crash the first turn.
        raw = self.config.get("auto_compact_tokens", 0) or 0
        limit = raw if isinstance(raw, int) and not isinstance(raw, bool) and raw > 0 else 0
        if limit and self.context.tokens > limit:
            self._print(self.style.dim(f"  (context ~{self.context.tokens} tokens, compacting)"))
            self.compact()

    def compact(self) -> bool:
        """Summarise the conversation, keeping the system prompt and summary."""
        if len(self.context.messages) <= 3:
            return False
        transcript = _transcript(self.context.messages)
        request = (
            "Summarise this coding session so work can continue without the "
            "original transcript. Cover: the goal, every file created or "
            "modified and why, commands that were run and their outcome, any "
            "error encountered and how it was resolved, and the exact state "
            "left off at. Be dense and concrete; no preamble.\n\n"
            f"{transcript}"
        )
        try:
            response = self.llm.chat(
                [{"role": "user", "content": request}], tools=None, on_delta=None
            )
        except HarnessError as exc:
            self._print(self.style.ember(f"  compaction failed: {exc}"))
            return False
        summary = (response.content or "").strip()
        if not summary:
            return False
        before = self.context.tokens
        self.context.replace_body(
            [
                {
                    "role": "user",
                    "content": "Earlier in this session (compressed summary):\n" + summary,
                }
            ]
        )
        self._print(
            self.style.dim(f"  compacted: ~{before} -> ~{self.context.tokens} tokens")
        )
        return True

    # ---- session persistence ---------------------------------------------

    def save_session(self, path: str) -> bool:
        # Validate path is inside allowed directories to prevent arbitrary write
        if not _is_safe_session_path(path, self.workspace):
            self._print(self.style.warn(f"  refusing to save outside allowed dirs: {path}"))
            self._print(self.style.dim(f"  allowed: workspace, {sessions.sessions_dir()}, temp"))
            return False
        # Enforce size cap on file path length and payload
        if len(path) > 500:
            self._print(self.style.ember("  save failed: path too long"))
            return False
        payload = {
            "version": 1,
            "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "workspace": self.workspace,
            "model": self.config.get("llm", {}).get("model", "?"),
            "totals": self.totals,
            "messages": self.context.messages,
            "show_tool_output": self._show_tool_output,
            "pending_pages": self._pending_pages_snapshot(),
        }
        # Cap file size via payload size check. A failure here (e.g. the
        # payload contains something json can't serialize) means the write
        # below will fail the same way, so surface it now instead of
        # silently skipping the check and hitting an uncaught error later.
        try:
            data = json.dumps(payload, ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            self._print(self.style.ember(f"  save failed: {exc}"))
            return False
        if len(data) > 10_000_000:
            self._print(self.style.ember("  save failed: session too large"))
            return False
        try:
            # Ensure parent dir exists with restricted perms
            parent = os.path.dirname(os.path.abspath(path))
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(path, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
        except (OSError, TypeError, ValueError) as exc:
            self._print(self.style.ember(f"  save failed: {exc}"))
            return False
        self._print(self.style.dim(f"  saved {len(self.context.messages)} messages to {path}"))
        return True

    def load_session(self, path: str) -> bool:
        if not _is_safe_session_path(path, self.workspace):
            self._print(self.style.warn(f"  refusing to load outside allowed dirs: {path}"))
            return False
        try:
            # Size check before load
            try:
                if os.path.getsize(path) > 10_000_000:
                    self._print(self.style.ember("  load failed: file too large"))
                    return False
            except OSError:
                pass
            with open(path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            self._print(self.style.ember(f"  load failed: {exc}"))
            return False
        if not isinstance(payload, dict):
            # A top-level list/string would crash the payload.get below.
            self._print(self.style.ember("  load failed: session file must contain a JSON object"))
            return False
        messages = payload.get("messages")
        if not isinstance(messages, list) or not messages:
            self._print(self.style.ember("  load failed: no messages in file"))
            return False
        if len(messages) > 500:
            # Cap restored sessions so a huge transcript cannot blow the budget.
            self._print(self.style.warn(f"  warning: large session {len(messages)} messages, truncating"))
            messages = messages[-500:]
        self.context.messages = list(messages)
        self.context.enforce_budget()
        totals = payload.get("totals")
        if isinstance(totals, dict):
            self.totals.update({k: _safe_int(v) for k, v in totals.items() if k in self.totals})
        output_pref = payload.get("show_tool_output")
        if isinstance(output_pref, bool):
            self._show_tool_output = output_pref
        remaining_pages = self._restore_pending_pages(payload)
        self._print(
            self.style.dim(f"  restored {len(messages)} messages (~{self.context.tokens} tokens)")
        )
        if remaining_pages:
            self._print(
                self.style.dim(
                    "  (capped outputs from the saved run remain — press Enter with an empty prompt to page through them)"
                )
            )
        return True

    # ---- resumable sessions ----------------------------------------------

    def autosave(self) -> None:
        """Keep the session resumable without being asked.

        Silent by design: the only time the operator learns the file
        exists is when /sessions lists it. Nothing is written until there
        is a real conversation.
        """
        if len(self.context.messages) < 2:
            return
        if not self.session_name:
            self.session_name = sessions.derive_name(self.workspace, self.model_name())
        sessions.save(
            self.session_name,
            {
                "workspace": self.workspace,
                "model": self.model_name(),
                "summary": self._session_summary(),
                "totals": self.totals,
                "goal": self.goal,
                "goal_notes": self.goal_notes,
                "todos": self.todos,
                "messages": self.context.messages,
                "show_tool_output": self._show_tool_output,
                "pending_pages": self._pending_pages_snapshot(),
            },
        )

    def model_name(self) -> str:
        return str(self.config.get("llm", {}).get("model", "") or "")

    def _session_summary(self) -> str:
        """The first thing the operator said - the only useful label."""
        for message in self.context.messages:
            if isinstance(message, dict) and message.get("role") == "user":
                content = message.get("content")
                if isinstance(content, str) and content.strip():
                    return re.sub(r"\s+", " ", content).strip()[:70]
        return ""

    def resume_session(self, name: str) -> bool:
        """Restore a saved session by name."""
        data = sessions.load(name)
        if data is None:
            known = sessions.list_sessions()
            if not known:
                self._print(self.style.dim("  no saved sessions yet"))
                return False
            self._print(self.style.ember(f"  no session named '{name}'"))
            self._print(self.style.dim("  /sessions lists them"))
            return False
        # Workspace guard: a session saved in one workspace is not
        # resumed in another.
        saved_ws = data.get("workspace") or ""
        if saved_ws and not self._is_same_workspace(saved_ws):
            self._print(self.style.warn(f"  session '{name}' belongs to workspace {saved_ws}"))
            self._print(self.style.dim(f"  current workspace is {self.workspace} — switch workspace or use /sessions list to see this workspace's sessions"))
            return False
        messages = data.get("messages")
        if not isinstance(messages, list) or not messages:
            self._print(self.style.ember(f"  session '{name}' has no conversation"))
            return False
        self.context.messages = list(messages)
        self.context.enforce_budget()
        totals = data.get("totals")
        if isinstance(totals, dict):
            self.totals.update({k: _safe_int(v) for k, v in totals.items() if k in self.totals})
        self.message_count = sum(
            1 for m in messages if isinstance(m, dict) and m.get("role") == "user"
        )
        # The goal travels with the conversation: resuming a session to
        # finish something and finding the objective gone defeats the
        # point of resuming it.
        self.goal = str(data.get("goal") or "")
        notes = data.get("goal_notes")
        self.goal_notes = [str(n) for n in notes] if isinstance(notes, list) else []
        # The todo checklist travels with the conversation too: resuming
        # a session to finish a multi-part task and finding its list
        # wiped would scatter the remaining work.
        saved_todos = data.get("todos")
        self.todos = []
        if isinstance(saved_todos, list):
            for entry in saved_todos:
                if isinstance(entry, dict) and "text" in entry:
                    self.todos.append({"text": str(entry["text"]), "done": bool(entry.get("done"))})
        # Adopt the name, so the next autosave continues this session
        # rather than starting a second file beside it.
        self.session_name = name
        # The operator's display preference travels with the session, so
        # resuming one where the tool-output boxes were hidden stays that
        # way (Ctrl+O flips it back live).
        output_pref = data.get("show_tool_output")
        if isinstance(output_pref, bool):
            self._show_tool_output = output_pref
        # An interrupted paging session resumes where it left off: any
        # capped tool output not yet paged through comes back with the
        # conversation, ready for the empty-Enter pager.
        remaining_pages = self._restore_pending_pages(data)
        # Hide splash so full history is visible immediately — splash otherwise
        # covers viewport until next resize/handle hides it.
        if self.layout is not None and getattr(self.layout, "_splash_visible", False):
            try:
                self.layout.hide_splash()
            except Exception:
                pass
            self._splash_visible = False
        if not self._show_tool_output:
            self._print(self.style.dim("  (tool output boxes are hidden in this session — ctrl+o to show)"))
        self._print(
            self.style.dim(
                f"  resumed '{name}' - {len(messages)} messages "
                f"(~{self.context.tokens} tokens)"
            )
        )
        if remaining_pages:
            self._print(
                self.style.dim(
                    "  (capped outputs from the interrupted run remain — press Enter with an empty prompt to page through them)"
                )
            )
        # Replay conversation history in the viewport.
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            role = msg.get("role", "")
            content = msg.get("content", "")
            # Handle multimodal/list content
            if isinstance(content, list):
                try:
                    content = " ".join(part.get("text","") for part in content if isinstance(part, dict) and part.get("type")=="text") or str(content)
                except Exception:
                    content = str(content)
            if role == "user":
                text = content if isinstance(content, str) else str(content or "")
                if text.strip():
                    self._print(f"{self.style.ash('you')} {_sanitize_output(text)}")
                else:
                    self._print(f"{self.style.ash('you')} (empty)")
            elif role == "assistant":
                # Assistant may have content null + tool_calls — render markdown for body so colors show.
                # Model-controlled text is sanitized exactly like the live
                # reply paths: saved output must not drive the terminal.
                if isinstance(content, str) and content.strip():
                    self._print(f"{self.style.brand('ENCHANTER')}")
                    self._print(render_markdown(_sanitize_output(content), self.style))
                elif msg.get("tool_calls"):
                    calls = ", ".join((c.get("function") or {}).get("name","?") for c in msg.get("tool_calls") or [])
                    self._print(f"{self.style.brand('ENCHANTER')} [called {calls}]")
                    if isinstance(content, str) and content.strip():
                        self._print(render_markdown(_sanitize_output(content), self.style))
                elif isinstance(content, str):
                    self._print(f"{self.style.brand('ENCHANTER')} {_sanitize_output(content)}")
            elif role == "tool":
                text = content if isinstance(content, str) else str(content or "")
                if text.strip():
                    # Keep the replay compact: 300 chars per tool result.
                    self._print(f"{self.style.dim('tool')} {self.style.dim(_sanitize_output(text)[:300])}")
                else:
                    self._print(f"{self.style.dim('tool')} (no output)")
        summary = data.get("summary") or ""
        if summary:
            self._print(self.style.dim(f"  started with: {summary}"))
        # Flush viewport so history appears immediately — without this the
        # throttled writes in the terminal application stay buffered until flush.
        if self.layout is not None and getattr(self.layout, "active", False):
            try:
                self.layout.flush()
            except Exception:
                pass
        return True

    def _is_same_workspace(self, saved_ws: str) -> bool:
        try:
            cur = os.path.realpath(os.path.abspath(self.workspace or ""))
            saved = os.path.realpath(os.path.abspath(saved_ws or ""))
            if os.name == "nt":
                return cur.lower() == saved.lower()
            return cur == saved
        except Exception:
            return (saved_ws or "") == (self.workspace or "")

    def show_sessions(self) -> None:
        """List what can be resumed — filtered to current workspace."""
        all_known = sessions.list_sessions()
        known = [item for item in all_known if self._is_same_workspace(item.get("workspace") or "")]
        if not known:
            if all_known:
                self._print(self.style.dim(f"  no saved sessions for this workspace ({self.workspace})"))
                self._print(self.style.dim(f"  {len(all_known)} session(s) exist for other workspaces — switch workspace to see them"))
            else:
                self._print(self.style.dim("  no saved sessions yet - they are saved as you go"))
            return
        self._print(self.style.bold(f"  saved sessions for {self.workspace}"))
        for item in known:
            when = item["saved_at"] or "unknown time"
            turns = item["turns"]
            label = f"{turns} turn" if turns == 1 else f"{turns} turns"
            head = f"  {item['name']}"
            if item["name"] == self.session_name:
                head += self.style.dim(" (current)")
            self._print(head)
            extra = ""
            if item.get("show_tool_output") is False:
                extra = " · output boxes hidden"
            self._print(self.style.dim(f"      {when} · {label} · {item['model'] or '?'}{extra}"))
            if item["summary"]:
                self._print(self.style.dim(f"      {item['summary']}"))
        self._print("")
        self._print(self.style.dim("  /sessions <name> to pick one up"))

    def pick_session(self) -> bool:
        """Resume from a menu — filtered to current workspace. False when nothing was chosen."""
        all_known = sessions.list_sessions()
        known = [item for item in all_known if self._is_same_workspace(item.get("workspace") or "")]
        if not known:
            if all_known:
                self._print(self.style.dim(f"  no saved sessions for this workspace ({self.workspace})"))
            else:
                self._print(self.style.dim("  no saved sessions yet - they are saved as you go"))
            return False
        options = []
        for item in known:
            summary = item["summary"] or "no summary"
            detail = f"{item['saved_at']} · {item['turns']} turns · {summary}"
            if item.get("show_tool_output") is False:
                detail += " · output boxes hidden"
            options.append(Option(item["name"], item["name"], detail))
        chosen = _menu(
            self,
            "Resume a session",
            options,
            hint="up/down move · Enter resume · Esc cancel",
        )
        if not chosen:
            return False
        return self.resume_session(chosen)

    # ---- inspection commands ---------------------------------------------

    def _git(self, *args: str) -> str:
        try:
            completed = subprocess.run(
                ["git", *args], cwd=self.workspace,
                capture_output=True, text=True, timeout=30,
            )
        except (OSError, subprocess.SubprocessError):
            return ""
        return completed.stdout if completed.returncode == 0 else ""

    def _git_ok(self, *args: str) -> bool:
        """Run git and report success; stdout alone cannot distinguish it."""
        try:
            completed = subprocess.run(
                ["git", *args], cwd=self.workspace,
                capture_output=True, text=True, timeout=30,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return completed.returncode == 0

    def show_workspace(self) -> None:
        root = self.sandbox.root
        self._print(f"workspace: {root}")
        try:
            entries = sorted(os.listdir(root))[:50] if os.path.isdir(root) else []
        except OSError as exc:
            self._print(self.style.ember(f"  cannot list workspace: {exc}"))
            return
        for entry in entries:
            try:
                full = os.path.join(root, entry)
                self._print(("> " if os.path.isdir(full) else "") + entry)
            except OSError:
                self._print(entry)
        if not entries:
            self._print("(empty)")

    def show_diff(self) -> None:
        stat = self._git("diff", "--stat")
        status = self._git("status", "--short")
        if not stat and not status:
            self._print("(no uncommitted changes)")
            return
        if status:
            self._print(status.rstrip())
        if stat:
            self._print(stat.rstrip())
        diff = self._git("diff")
        if diff:
            # Same before/after panes as the live edit previews and the
            # agent's git-diff boxes: removed lines on tomato, added on
            # lime, with a file chip per group.
            rows = self._diff_pane_rows(diff, max_lines=400)
            if rows is not None:
                self._print(self._box("git diff", rows))
                return
            lines = diff.splitlines()
            cap = 400  # plain-text fallback: same cap as the boxed panes
            self._print("\n".join(lines[:cap]))
            if len(lines) > cap:
                self._print(f"... ({len(lines) - cap} more lines)")

    def undo_changes(self) -> None:
        status = self._git("status", "--porcelain")
        if not status:
            self._print("(nothing to undo - working tree is clean)")
            return
        self._print(f"{len(status.strip().splitlines())} file(s) would be reverted:")
        self._print(status.rstrip())
        try:
            if self.ui is not None:
                answer = self.ui.ask_line('type "yes" to revert tracked changes').strip()
            else:
                answer = input('type "yes" to revert tracked changes: ').strip()
        except (KeyboardInterrupt, EOFError):
            self._print("cancelled")
            return
        if answer.lower() != "yes":
            self._print("cancelled")
            return
        self._print("reverted" if self._git_ok("checkout", "--", ".") else "revert failed")

    def show_cost(self, compact: bool = False, as_json: bool = False) -> None:
        t = self.totals
        tokens_in = t['tokens_in']
        tokens_out = t['tokens_out']
        cache_hit = t['cache_hit']
        context_tokens = self.context.tokens
        context_chars = self.context.chars

        # Derived metrics.
        cache_rate = (cache_hit * 100 // tokens_in) if tokens_in > 0 else 0
        cache_saved = cache_hit // 2  # ~50% discount

        if as_json:
            import json
            payload = {
                "turns": t['turns'],
                "tokens_in": tokens_in,
                "tokens_out": tokens_out,
                "cache_hit": cache_hit,
                "cache_rate": cache_rate,
                "cache_saved": cache_saved,
                "tool_errors": t['tool_errors'],
                "context_tokens": context_tokens,
                "context_chars": context_chars,
            }
            if self.turn_history:
                payload["turn_history"] = self.turn_history[-10:]
            self._print(json.dumps(payload, indent=2))
            return

        self._print(f"turns        {t['turns']}")
        self._print(f"tokens in    {tokens_in}")
        self._print(f"tokens out   {tokens_out}")
        # Cache analytics: show hit rate and estimated savings.
        if tokens_in > 0 and cache_hit > 0:
            self._print(f"cache hit    {cache_hit} ({cache_rate}% of prompt)")
            self._print(f"cache saved  {cache_saved} tokens (~50% discount)")
        else:
            self._print(f"cache hit    {cache_hit}")
        self._print(f"tool errors  {t['tool_errors']}")
        self._print(f"context      ~{context_tokens} tokens ({context_chars} chars)")
        # Per-turn cache trend (last 5 turns) — skipped in compact mode.
        if not compact and self.turn_history:
            recent = self.turn_history[-5:]
            self._print("")
            self._print("  turn  in      out     cached  rate")
            self._print("  " + "─" * 40)
            for entry in recent:
                tin = entry['tokens_in']
                tout = entry['tokens_out']
                ch = entry['cache_hit']
                cr = entry['cache_rate']
                self._print(
                    f"  {entry['turn']:<6}{tin:<8}{tout:<8}{ch:<8}{cr}%"
                )

    def set_model(self, name: str, quiet: bool = False) -> None:
        self.config.setdefault("llm", {})["model"] = name
        try:
            self.llm = build_llm(self.config["llm"])
        except HarnessError as exc:
            self._print(self.style.ember(f"  could not switch model: {exc}"))
            return
        if not quiet:
            self._print(self.style.dim(f"  model is now {name}"))
        self.refresh_title()
        self._warn_if_key_missing()
        # Wire top info instantly so MODEL shows new name without waiting for next turn
        if self.layout is not None and getattr(self.layout, "active", False):
            try:
                self.layout.draw_chrome()
            except Exception:
                pass

    def set_reasoning(self, level: str, quiet: bool = False) -> None:
        """Set the thinking budget for the current model.

        Not every endpoint understands the field; the client sheds it on a
        400 and the reply simply arrives without the extra thinking.
        """
        wanted = level.strip().lower()
        if wanted in ("off", "none", ""):
            wanted = None
        elif wanted not in REASONING_EFFORTS:
            self._print(
                self.style.warn(f"  reasoning must be one of {', '.join(REASONING_EFFORTS)} or off")
            )
            return
        llm = self.config.setdefault("llm", {})
        llm["reasoning_effort"] = wanted
        try:
            self.llm = build_llm(llm)
        except HarnessError as exc:
            self._print(self.style.ember(f"  could not set reasoning: {exc}"))
            return
        if not quiet:
            self._print(
                self.style.dim(f"  reasoning is now {wanted}" if wanted else "  reasoning off")
            )
        self.refresh_title()
        if self.layout is not None and getattr(self.layout, "active", False):
            try:
                self.layout.draw_chrome()
            except Exception:
                pass

    def show_reasoning(self) -> None:
        effort = self.config.get("llm", {}).get("reasoning_effort")
        current = effort or "off"
        options = " ".join(
            f"[{e}]" if e == effort else e for e in REASONING_EFFORTS
        )
        self._print(f"  reasoning  {current}   {self.style.dim(options + '  off')}")
        self._print(
            self.style.dim(
                "  higher means more thorough and slower; ignored by models "
                "that do not reason"
            )
        )

    @property
    def endpoint_name(self) -> str:
        """Which saved endpoint the current base URL belongs to, if any."""
        llm = self.config.get("llm", {})
        return endpoint_name_for_url(llm.get("base_url", "")) or ""

    def use_endpoint(self, name: str, model: str | None = None) -> bool:
        """Point the agent at a saved endpoint. True on success."""
        entry = known_endpoints().get(name.lower())
        if entry is None:
            self._print(self.style.warn(f"  no endpoint named '{name}'"))
            self._print(self.style.dim("  add one with /model, or list them: /model"))
            return False
        llm = self.config.setdefault("llm", {})
        llm["base_url"] = entry["base_url"]
        llm["api_key_env"] = entry.get("api_key_env") or ""
        # A model name rarely survives a move between endpoints, so take
        # the first one this endpoint offers unless one was asked for.
        llm["model"] = model or (entry.get("models") or [""])[0] or llm.get("model", "")
        try:
            self.llm = build_llm(llm)
        except HarnessError as exc:
            self._print(self.style.ember(f"  could not switch endpoint: {exc}"))
            return False
        set_active(endpoint=name.lower(), model=llm.get("model", ""))
        self._print(self.style.dim(f"  endpoint is now {entry['base_url']}"))
        self.refresh_title()
        self._warn_if_key_missing()
        if self.layout is not None and getattr(self.layout, "active", False):
            try:
                self.layout.draw_chrome()
            except Exception:
                pass
        return True

    def _warn_if_key_missing(self) -> None:
        """Say so up front when the key variable is unset.

        Silence here turns into a confusing 401 three steps into a task.
        """
        llm = self.config.get("llm", {})
        key_env = llm.get("api_key_env") or ""
        if not provider_needs_key(llm.get("base_url", ""), key_env):
            return
        if os.environ.get(key_env) or has_stored(key_env):
            return
        self._print(self.style.warn(f"  warning: no key for ${key_env}"))
        self._print(
            self.style.dim(
                f"  store one with /model, or edit {settings_path()}"
            )
        )

    def show_endpoints(self) -> None:
        """List what the user has configured, and where the file is."""
        llm = self.config.get("llm", {})
        current = (llm.get("base_url") or "").rstrip("/")
        known = known_endpoints()
        if not known:
            self._print(self.style.dim("  no endpoints yet - add one with /model"))
            # Name the file even here: an empty list is exactly when
            # somebody is most likely to want to type one in by hand.
            self._print(self.style.dim(f"  or add one to {settings_path()}"))
            return
        self._print(self.style.bold("  endpoints"))
        for name in sorted(known):
            entry = known[name]
            marker = "*" if entry["base_url"] == current else " "
            key_env = entry.get("api_key_env") or ""
            if not provider_needs_key(entry["base_url"], key_env):
                key_state = "no key needed"
            elif os.environ.get(key_env):
                key_state = "key in env"
            elif has_stored(key_env):
                key_state = f"stored {mask(stored_keys().get(key_env))}"
            else:
                key_state = "no key"
            count = len(entry.get("models") or [])
            model_bit = f"{count} model" + ("" if count == 1 else "s")
            tail = " · ".join(p for p in (key_state, model_bit) if p)
            self._print(
                f"  {marker} {name:<12} {entry['base_url']:<38}"
                f" {self.style.dim(tail)}"
            )
        self._print(self.style.dim("  * = current. add or switch: /model"))
        self._print(self.style.dim(f"  or edit by hand: {settings_path()}"))

    def banner(self) -> None:
        s = self.style
        art = [
            "╔══════════════════════════════╗",
            "║  M A N T R A                 ║",
            "║  coding agent harness        ║",
            "╚══════════════════════════════╝",
        ]
        for line in art:
            self._print(s.brand(line))
        llm_cfg = self.config.get("llm", {})
        self._print(f"model      {s.bold(llm_cfg.get('model', '?'))}")
        self._print(f"endpoint   {s.dim(llm_cfg.get('base_url', '?'))}")
        self._print(f"workspace  {self.sandbox.root}")
        branch = self._git("rev-parse", "--abbrev-ref", "HEAD").strip()
        if branch:
            dirty = self._git("status", "--porcelain")
            state = "clean" if not dirty else f"{len(dirty.strip().splitlines())} uncommitted"
            self._print(f"git        {branch} ({state})")
        self._print(f"approvals  {self.approvals.mode}  {s.dim('/'.join(MODES))}")
        self._print(f"tools      {len(self.tools)} loaded, step limit {self.max_steps}")
        if self.instructions_path:
            self._print(s.dim(f"instructions loaded from {os.path.basename(self.instructions_path)}"))
        try:
            if os.path.isfile(KNOWN_FAILURES_PATH):
                # Skip the "## KF-N" template line: only numbered entries count.
                with open(KNOWN_FAILURES_PATH, encoding="utf-8", errors="replace") as kf:
                    count = sum(
                        1 for ln in kf
                        if ln.startswith("## KF-") and ln[6:7].isdigit()
                    )
                self._print(s.dim(f"known-failure registry: {count} classes"))
        except OSError:
            # A race or permission error on a piped run must not crash the banner.
            pass
        self._print(s.dim("type /help for commands"))


# There are no built-in endpoints. Everything MANTRA knows about lives
# in the user's own settings file, which is hand-editable and is
# written by /model. See core/settings.py for the shape.

# Endpoints reached over localhost that accept any key or none at all.
KEYLESS_HOSTS = ("localhost", "127.0.0.1", "0.0.0.0")


def provider_needs_key(base_url: str, api_key_env: str) -> bool:
    """False for local endpoints, which simply do not check a key."""
    if not api_key_env:
        return False
    # Compare the parsed hostname exactly, never a substring of the whole
    # URL: a substring match would treat https://evil-localhost.proxy.com
    # as keyless. A dotted-suffix .localhost name resolves to the loopback
    # interface (RFC 6761) and is keyless too.
    hostname = (urlparse(base_url or "").hostname or "").lower()
    if hostname in KEYLESS_HOSTS or hostname.endswith(".localhost"):
        return False
    return True


SLASH_COMMANDS = [
    ("/model", "provider & model — add endpoint, pick a model"),
    ("/model key", "replace stored key"),
    ("/fix", "send the last failure to the agent for a fix"),
    ("/sessions", "saved conversations — browse and resume"),
    ("/help", "show help"),
    ("/workspace", "show workspace"),
    ("/memory", "show memory"),
    ("/diff", "show changes"),
    ("/undo", "discard changes"),
    ("/approve", "set approve mode"),
    ("/cost", "show usage"),
    ("/compact", "summarise chat"),
    ("/clear", "clear chat"),
    ("/goal", "set goal"),
    ("/todo", "session checklist"),
    ("/workflow", "run workflow"),
    ("/skills", "attach skill"),
    ("/verbose", "toggle verbose"),
    ("/exit", "exit"),
]

_SKIP_DIRS = {
    ".git", "__pycache__", "node_modules", ".venv", "venv",
    ".mypy_cache", ".pytest_cache", ".tox", "dist", "build", ".idea", ".vscode",
}
MAX_INDEX_ENTRIES = 4000


class ConsoleCompleter:
    """Suggests slash commands after ``/`` and workspace paths after ``@``."""

    def __init__(self, session: "ConsoleSession") -> None:
        self.session = session
        self._entries: list[str] = []
        self._indexed = False
        self._cache_root = ""
        self._cache_time = 0.0

    def begin(self) -> None:
        """Re-index the workspace once per prompt, not once per keystroke."""
        root = os.path.abspath(self.session.sandbox.root)
        now = time.monotonic()
        if self._indexed and root == self._cache_root and (now - self._cache_time) < 0.5:
            return
        entries: list[str] = []
        try:
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS)
                relative_dir = os.path.relpath(dirpath, root)
                base = "" if relative_dir == "." else relative_dir.replace(os.sep, "/")
                for name in sorted(dirnames):
                    entries.append(f"{base}/{name}/" if base else f"{name}/")
                for name in sorted(filenames):
                    entries.append(f"{base}/{name}" if base else name)
                if len(entries) >= MAX_INDEX_ENTRIES:
                    break
        except OSError:
            # Silent fallback: keep previous entries instead of clearing.
            # Clearing would make @ completion appear broken for 0.5s after
            # a transient walk error.
            if not self._entries:
                entries = []
            else:
                return
        self._entries = entries[:MAX_INDEX_ENTRIES]
        self._indexed = True
        self._cache_root = root
        self._cache_time = time.monotonic()

    def complete(self, buffer: str, cursor: int):
        # The editor primes this once per prompt, but a caller using the
        # completer directly should not get silence for forgetting to.
        if not self._indexed:
            self.begin()
        # A cursor past the end must not index off the string; clamp
        # rather than trusting every caller to pass a sane position.
        cursor = max(0, min(cursor, len(buffer)))
        start = cursor
        # Invisible characters (BOM, zero-width, NBSP, ideographic space)
        # never start a token; strip them while scanning for the boundary.
        _ZW = "\ufeff\u200b\u200c\u200d\u00a0\u3000"
        while start > 0 and not buffer[start - 1].isspace() and buffer[start - 1] not in _ZW:
            start -= 1
        # Skip leading zero-width that may have been left at token start
        while start < cursor and buffer[start] in _ZW:
            start += 1
        token = buffer[start:cursor]
        # Strip trailing punctuation from the token for completion purposes
        # so "@src/app.py," still completes to the file.
        trim_token = token.rstrip(_MENTION_TRIM)
        # If trimming changed the token, adjust end position accordingly
        trim_end = cursor - (len(token) - len(trim_token)) if trim_token != token else cursor
        # Also handle full-width variants that some keyboards produce
        if trim_token.startswith("＠"):
            return self._complete_path(start, trim_end, trim_token[1:])
        if trim_token.startswith("@"):
            return self._complete_path(start, trim_end, trim_token[1:])
        # Slash commands: allow leading whitespace and invisible chars, but only when the slash
        # token is the first non-space token (so "hello /help" does not
        # trigger, but "  /help" does). Strip trailing punctuation so
        # "/help," still offers completions.
        # Handle both normal and full-width slash, and be forgiving of invisible leading chars
        slash_token = trim_token
        if slash_token.startswith("／") or slash_token.startswith("\uff0f"):
            slash_token = "/" + slash_token[1:]
        if slash_token.startswith("/") and _strip_leading_invisible(buffer[:start]).strip() == "":
            return self._complete_command(trim_end, slash_token, start)
        # Sub-command completions: use lstrip so leading spaces don't break
        # them (operator may indent). Keep start-based token for replacement.
        stripped = buffer.lstrip()
        if stripped.startswith("/model "):
            return self._complete_model(cursor, token)
        if stripped.startswith("/skills") or stripped.startswith("/skill "):
            c = self._complete_skills(buffer, start, cursor, token)
            if c:
                return c
        if stripped.strip() in ("/skills", "/skill"):
            c = self._complete_skills(buffer, start, cursor, token)
            if c:
                return c
        if stripped.startswith("/workflow"):
            c = self._complete_workflow(buffer, start, cursor, token)
            if c:
                return c
        return None

    def _complete_model(self, cursor: int, token: str):
        known = getattr(self.session, "known_models", None) or []
        lowered = token.lower()
        matches = [m for m in known if m.lower().startswith(lowered)]
        if not matches:
            # /model also manages providers: offer the subcommands and
            # the saved endpoint names alongside model names.
            sub = [s for s in ("list", "remove", "key") if s.startswith(lowered)]
            eps = [n for n in sorted(known_endpoints().keys()) if n.startswith(lowered)]
            matches = sub + eps
            if not matches:
                return None
            return Completion(items=matches, start=cursor - len(token), end=cursor)
        matches = matches[:50]
        labels = [m + ("   reasons" if is_reasoning_model(m) else "") for m in matches]
        return Completion(
            items=matches, start=cursor - len(token), end=cursor, labels=labels
        )

    def _complete_command(self, cursor: int, token: str, start: int = 0):
        matches = [name for name, _ in SLASH_COMMANDS if name.startswith(token)]
        if not matches:
            return None
        labels = []
        for name in matches:
            description = next((d for n, d in SLASH_COMMANDS if n == name), "")
            labels.append(f"{name}  {description}")
        return Completion(items=matches, start=start, end=cursor, labels=labels)

    def _complete_path(self, start: int, cursor: int, query: str):
        if not self._entries:
            return None
        needle = query.lower().replace("\\", "/")
        prefix = [e for e in self._entries if e.lower().startswith(needle)]
        inside = [e for e in self._entries if needle and needle in e.lower()]
        ordered = prefix + [e for e in inside if e not in prefix]
        matches = ordered[:50]
        if not matches:
            return None
        return Completion(
            items=["@" + m for m in matches], start=start, end=cursor, labels=matches
        )

    def _complete_skills(self, buffer: str, start: int, cursor: int, token: str):
        # EASY: /skills space [anything] → shows matching skills, filtered by name/type
        # No other commands, no menu — just skills.
        prefix = buffer[:start]
        parts = prefix.strip().split()
        if not parts or parts[0] not in ("/skills", "/skill"):
            return None
        # Only the first argument after /skills is completed; ignore deeper args
        if len(parts) > 1:
            return None
        known = skills.list_skills()
        if not known:
            return None
        lowered = token.lower()
        if not lowered:
            # "/skills " + Tab → show all skills
            items = sorted(s.name for s in known)[:50]
            labels = []
            for n in items:
                sk = skills.get(n)
                desc = (sk.description or "")[:40]
                labels.append(f"{n}  {desc}" if desc else n)
            return Completion(items=items, start=start, end=cursor, labels=labels)
        # Filter by name OR description/type (case-insensitive substring)
        # For very short queries (<3 chars) only match name to avoid noisy description hits
        matches: list[str] = []
        for sk in known:
            if lowered in sk.name.lower():
                matches.append(sk.name)
            elif len(lowered) >= 3 and lowered in (sk.description or "").lower():
                matches.append(sk.name)
        if not matches:
            return None
        # Prefix matches first, then others — both alphabetically
        prefix_hits = [n for n in matches if n.lower().startswith(lowered)]
        other_hits = [n for n in matches if n not in prefix_hits]
        ordered = sorted(prefix_hits) + sorted(other_hits)
        ordered = ordered[:50]
        labels = []
        for n in ordered:
            sk = skills.get(n)
            desc = (sk.description or "")[:40] if sk else ""
            labels.append(f"{n}  {desc}" if desc else n)
        return Completion(items=ordered, start=start, end=cursor, labels=labels)

    def _complete_workflow(self, buffer: str, start: int, cursor: int, token: str):
        prefix = buffer[:start]
        parts = prefix.strip().split()
        if not parts or parts[0] != "/workflow":
            return None
        subcommands = ["list", "show", "create", "launch", "run", "start", "remove", "delete", "rm"]
        workflow_names = [w["name"] for w in workflows.list_workflows()]
        if len(parts) == 1:
            candidates = subcommands + workflow_names
            lowered = token.lower()
            matches = [c for c in candidates if c.lower().startswith(lowered)]
            if not matches and lowered:
                matches = [c for c in candidates if lowered in c.lower()]
            if not matches:
                return None
            return Completion(items=matches[:50], start=start, end=cursor, labels=matches[:50])
        elif len(parts) == 2:
            sub = parts[1].lower()
            if sub in ("show", "launch", "run", "start", "remove", "delete", "rm"):
                lowered = token.lower()
                matches = [n for n in workflow_names if n.lower().startswith(lowered)]
                if not matches and lowered:
                    matches = [n for n in workflow_names if lowered in n.lower()]
                if not matches:
                    return None
                return Completion(items=matches[:50], start=start, end=cursor, labels=matches[:50])
        return None


_SAFE_HOME = os.path.expanduser("~")


def _infer_workspace() -> str:
    """Current directory becomes the workspace, like a real agent CLI.

    Guards: skip MANTRA itself (uses its own sandbox) and refuse to turn
    filesystem roots or the user home into a workspace - git-isolating
    ``C:\\Users\\<name>`` would be destructive nonsense.
    """
    cwd = os.getcwd()
    if cwd == PROJECT_ROOT:
        return os.path.join(PROJECT_ROOT, "workspace")
    protected = {
        os.path.dirname(_SAFE_HOME.rstrip("\\/")) or _SAFE_HOME,
        _SAFE_HOME,
        os.path.splitdrive(cwd)[0] + "\\",  # drive root, e.g. C:\
    }
    normalized = cwd.rstrip("\\/")
    if any(normalized.lower() == p.lower().rstrip("\\/") for p in protected):
        return os.path.join(PROJECT_ROOT, "workspace")
    return cwd


def _is_safe_session_path(path: str, workspace: str) -> bool:
    """Check if a session file path is inside allowed directories."""
    try:
        real = os.path.realpath(os.path.abspath(path))
        # Allow workspace and its .mantra, sessions dir, temp dir, and home/.mantra
        allowed = [
            os.path.realpath(workspace),
            os.path.realpath(os.path.join(workspace, ".mantra")),
            os.path.realpath(sessions.sessions_dir()),
            os.path.realpath(tempfile.gettempdir()),
            os.path.realpath(os.path.expanduser("~/.mantra")),
        ]
        # Also allow current project workspace default
        try:
            allowed.append(os.path.realpath(os.path.join(PROJECT_ROOT, "workspace")))
        except Exception:
            pass
        for base in allowed:
            if real == base or real.startswith(base + os.sep):
                return True
        return False
    except Exception:
        return False


# ------------------------------------------------------------------ commands

def _read_multiline(session: "ConsoleSession") -> str:
    """Read several lines, ended by a lone dot.

    Each line goes through the editor so that, inside the frame, they
    are drawn as frame rows rather than spilling out past the border.
    """
    session._print("  (paste your message; finish with a line containing only .)")
    lines = []
    try:
        while True:
            line = _read_choice(session, "")
            if line.strip() == ".":
                break
            lines.append(line)
    except (KeyboardInterrupt, EOFError):
        pass
    return "\n".join(lines).strip()


def _read_choice(session: "ConsoleSession", prompt_text: str) -> str:
    """Read a line from the operator; empty when there is no terminal.

    Inside the terminal application the answer is typed into a card and
    the caller's thread blocks until it is submitted. Every caller must
    tolerate an empty answer, because a piped run has nobody to answer.
    """
    if session.ui is not None:
        try:
            return session.ui.ask_line(prompt_text).strip()
        except Exception:
            return ""
    if not sys.stdin.isatty():
        return ""
    try:
        return input(prompt_text).strip()
    except (KeyboardInterrupt, EOFError):
        return ""


def _skills(session: "ConsoleSession", argument: str) -> None:
    """/skills — EASY: type "/skills " + Tab shows all, type to filter by name/type.

    Usage:
      /skills              — list all
      /skills <name>       — attach skill in 1 step (e.g. /skills tdd)
      /skill is alias for /skills
    """
    parts = argument.split() if argument else []
    raw_head = parts[0] if parts else ""
    head = raw_head.lower() if parts else ""
    rest = " ".join(parts[1:]).strip()
    # Friendly aliases so manual is forgiving
    alias = {"attach": "use", "detach": "clear", "ls": "list", "info": "show", "cat": "show", "rm": "clear", "search": "find", "route": "find", "run": "launch", "apply": "use", "on": "use"}
    head = alias.get(head, head)

    if head in ("help", "?", "-h", "--help", "manual", "h"):
        _skills_help(session)
    elif head == "":
        _skills_dashboard(session)
    elif head == "list":
        _skills_list(session)
    elif head == "show":
        _skills_show(session, rest)
    elif head == "use":
        # Support "use all" / "use --all" for one-step bulk attach
        if rest.lower() in ("all", "--all", "autoload", "autoloadall", "allskills", "autoloadallskill"):
            _skills_use_all(session)
        else:
            _skills_use(session, rest)
    elif head in ("bundles", "bundle"):
        _skills_bundles(session)
    elif head == "launch":
        _skills_launch(session, rest)
    elif head == "find":
        _skills_find(session, rest)
    elif head == "auto":
        _skills_auto(session, rest)
    elif head in ("all", "autoload", "autoloadall", "allskills", "autoloadallskill"):
        # Direct one-step: /skills all → attach all
        _skills_use_all(session)
    elif head in ("clear", "off", "drop"):
        if session.active_skills:
            session._print(session.style.dim("  skills detached: " + ", ".join(session.active_skills)))
            session.active_skills = []
        else:
            session._print(session.style.dim("  no skills attached"))
    else:
        # EASY: /skills <name> — attach in 1 step, no "use" needed
        # e.g. /skills tdd  →  same as /skills use tdd
        if skills.get(argument):
            _skills_use(session, argument)
            return
        if skills.get(head):
            # The first token is an exact skill name: attach it (bare),
            # or run it once when a reference follows. The full argument
            # carries the reference through to the one-shot path.
            _skills_use(session, argument)
            return
        # Try find — if single hit, use it; else show options
        hits = skills.find(argument, limit=5)
        if len(hits) == 1:
            _skills_use(session, hits[0].name)
            return
        elif hits:
            _skills_find(session, argument)
            return
        else:
            _skills_show(session, argument)
            return


def _skills_help(session: "ConsoleSession") -> None:
    s = session.style
    session._print(s.bold("  /skills — EASY"))
    session._print(s.dim("  Skills are reusable procedures that ride along with a turn."))
    session._print("")
    session._print("    /skills              — list all")
    session._print("    /skills <name>       — attach in 1 step  (e.g. /skills tdd)")
    session._print("")
    session._print(s.dim("  type /skills + space, Tab shows all, type to filter by name/type"))


def _skills_dashboard(session: "ConsoleSession") -> None:
    # EASY: no menu, just list.  "/skills space" completion does the filtering.
    _skills_list(session)


def _skills_list(session: "ConsoleSession") -> None:
    known = skills.list_skills()
    if not known:
        session._print(session.style.dim("  no skills found"))
        session._print(session.style.dim(f"  looked in: {', '.join(str(r) for r in skills.roots())}"))
        session._print(session.style.dim(f"  set {skills._OVERRIDE_ENV} to point at a skills directory"))
        return
    index = skills.routing_table()
    session._print(session.style.bold(f"  skills ({len(known)})"))
    for skill in known:
        entry = index.get(skill.name.lower(), {})
        function = entry.get("function") or skill.description
        function = " ".join(str(function).split())
        if len(function) > 68:
            function = function[:65].rstrip() + "..."
        mark = "*" if skill.name.lower() in session.active_skills else " "
        session._print(f" {mark} {skill.name:<18} {session.style.dim(function)}")
    session._print("")
    session._print(session.style.dim("  EASY: /skills <name> to attach  ·  type /skills + space, Tab to filter"))


def _skills_show(session: "ConsoleSession", name: str) -> None:
    if not name:
        _skills_list(session)
        return
    found = skills.get(name)
    if found is None:
        cands = skills.find(name, limit=5)
        session._print(session.style.ember(f"  no skill named '{name}'"))
        if cands:
            session._print(session.style.dim("  did you mean: " + ", ".join(c.name for c in cands)))
        return
    session._print(session.style.bold(f"  {found.name}"))
    if found.description:
        session._print(session.style.dim(f"  {found.description}"))
    meta = []
    if found.version:
        meta.append(f"v{found.version}")
    if found.resources:
        meta.append("bundles " + ", ".join(found.resources))
    if meta:
        session._print(session.style.dim("  " + " · ".join(meta)))
    body = found.body.strip()
    if not body:
        session._print(session.style.dim("  (empty)"))
        return
    session._print("")
    # Indented so the procedure reads as a block inside the frame
    # rather than as more console output.
    for line in body.split("\n"):
        session._print("  " + line.rstrip())
    session._print("")
    session._print(session.style.dim(f"  /skills use {found.name} to attach it"))


# Skills already flagged as coming from an external (non-bundled) root;
# each is warned about once per process, not on every attachment.
_UNTRUSTED_SKILL_WARNED: set[str] = set()


def _warn_untrusted_skill(session: "ConsoleSession", skill) -> None:
    """Warn once per external skill: its procedure is prompt input."""
    if skills.is_bundled(skill):
        return
    key = skill.name.lower()
    if key in _UNTRUSTED_SKILL_WARNED:
        return
    _UNTRUSTED_SKILL_WARNED.add(key)
    root = str(skill.root) if skill.root else "an external root"
    session._print(
        session.style.warn(
            f"  '{skill.name}' comes from {root} - its procedure is injected "
            "into the model prompt as instructions. Treat it as untrusted input."
        )
    )


def _skills_use(session: "ConsoleSession", name: str) -> None:
    if not name:
        session._print(session.style.dim("  usage: /skills <name>"))
        return
    found = skills.get(name)
    rest = ""
    if found is None:
        # Tolerate a trailing reference ("use code-review @flappy.py"):
        # the first token being an exact skill name is what matters.
        head = name.split()[0].lower()
        if head != name.lower():
            found = skills.get(head)
            rest = name.split(maxsplit=1)[1].strip()
    if found is None:
        cands = skills.find(name, limit=5)
        session._print(session.style.ember(f"  no skill named '{name}'"))
        if cands:
            session._print(session.style.dim("  did you mean: " + ", ".join(c.name for c in cands)))
        return
    if rest:
        # A trailing reference ("/skills code-review @flappy.py") is a
        # one-shot: run the skill once on those files, then detach - not
        # an every-turn attachment.
        _skills_one_shot(session, found, rest)
        return
    key = found.name.lower()
    if key in session.active_skills:
        session._print(session.style.dim(f"  '{found.name}' is already attached"))
        return
    _warn_untrusted_skill(session, found)
    session.active_skills.append(key)
    session._print(session.style.dim(f"  attached '{found.name}' - it now rides along with every turn"))
    session._print(session.style.dim("  /skills clear to detach"))


def _skills_one_shot(session: "ConsoleSession", found, reference: str) -> None:
    """Run one skill once against the given reference, then detach.

    "/skills code-review @flappy.py" runs code-review on the file for a
    single turn instead of attaching it to every turn. The skill is
    marked auto-attached so the turn's end detaches it again.
    """
    key = found.name.lower()
    if key not in session.active_skills:
        session.active_skills.append(key)
        session.auto_attached.append(key)
    _warn_untrusted_skill(session, found)
    session._print(session.style.dim(f"  running '{found.name}' once on: {reference}"))
    session.handle(f"Apply the {found.name} skill to: {reference}")
    # handle detaches auto-attached skills at turn end; this guards the
    # path where the turn aborts before its own cleanup runs.
    session._detach_auto()
    session._print(session.style.dim(f"  '{found.name}' ran once and is detached"))


def _skills_use_all(session: "ConsoleSession") -> None:
    """One-step bulk attach: /skills all  (also autoload, by type)"""
    known = skills.list_skills()
    if not known:
        session._print(session.style.dim("  no skills found"))
        session._print(session.style.dim(f"  looked in: {', '.join(str(r) for r in skills.roots())}"))
        return
    # If already all attached, say so
    all_keys = [s.name.lower() for s in known]
    new = [k for k in all_keys if k not in session.active_skills]
    if not new:
        session._print(session.style.dim(f"  all {len(known)} skills already attached"))
        return
    for k in new:
        _warn_untrusted_skill(session, skills.get(k))
        session.active_skills.append(k)
    session._print(session.style.dim(f"  attached all {len(new)} skills: " + ", ".join(new)))
    session._print(session.style.dim("  bundle auto is kept — /skills auto bundle on|off to change"))
    session._print(session.style.dim("  /skills clear to detach all"))


def _skills_bundles(session: "ConsoleSession") -> None:
    bundles = skills.load_bundles()
    if not bundles:
        session._print(session.style.dim("  no bundles found (no BUNDLES.md in any skills root)"))
        return
    session._print(session.style.bold(f"  bundles ({len(bundles)})"))
    for name, steps in sorted(bundles.items()):
        session._print(f"  {name:<16} {session.style.dim(' > '.join(steps))}")
    session._print("")
    session._print(session.style.dim("  /skills launch <bundle> to run one in order"))


def _skills_launch(
    session: "ConsoleSession", name: str, initial_text: str = ""
) -> RunResult | None:
    """Run a bundle as ordered steps, attaching each skill in turn.

    ``initial_text`` is the operator's own request when the bundle was
    launched on its behalf: the first step then works on that request
    instead of a generic "apply the skill" line that names no subject —
    dropping the request would silently replace what the operator asked
    for with an instruction the model cannot act on.
    """
    if not name:
        bundles = skills.load_bundles()
        if not bundles:
            session._print(session.style.dim("  no bundles found"))
            return None
        choice = _menu(session, "Launch bundle", [Option(value=n, label=n, hint=" > ".join(v[:2])) for n, v in sorted(bundles.items())])
        if not choice:
            session._print(session.style.dim("  usage: /skills launch <bundle>"))
            return None
        name = choice
    steps = skills.get_bundle(name)
    if steps is None:
        bundles = skills.load_bundles()
        if bundles:
            choice = _menu(session, f"Bundle '{name}' not found", [Option(value=n, label=n, hint=" > ".join(v[:2])) for n, v in sorted(bundles.items())])
            if choice:
                return _skills_launch(session, choice)
        session._print(session.style.ember(f"  no bundle named '{name}'"))
        return None
    known = skills.load_all()
    missing = [s for s in steps if s.lower() not in known]
    if missing:
        session._print(session.style.warn(f"  bundle names skills that are not installed: {', '.join(missing)}"))
        return None
    count = len(steps)
    label = f"{count} step" if count == 1 else f"{count} steps"
    session._print(session.style.bold(f"  launching bundle '{name}' ({label})"))
    previous = list(session.active_skills)
    last: RunResult | None = None
    was_in_bundle = session.in_bundle
    # Steps are turns, and the router would otherwise re-read each step's
    # boilerplate and attach something of its own over the top.
    session.in_bundle = True
    try:
        for position, step in enumerate(steps, 1):
            skill = known[step.lower()]
            session.active_skills = [skill.name.lower()]
            session._print("")
            session._print(
                session.style.dim(f"  step {position} of {count}: {skill.name} — {skill.description}")
            )
            try:
                if position == 1 and initial_text.strip():
                    prompt = initial_text
                else:
                    prompt = f"Apply the {skill.name} skill to the current work."
                result = session.handle(prompt)
            except KeyboardInterrupt:
                session._print(session.style.warn("  bundle stopped"))
                return last
            if result is None:
                session._print(session.style.warn("  bundle stopped: the step did not complete"))
                return last
            last = result
    finally:
        session.in_bundle = was_in_bundle
        # Whatever the previous attachment was, put it back - a bundle
        # borrowing the slot must not silently drop what was there.
        session.active_skills = previous
    session._print("")
    session._print(session.style.dim(f"  bundle '{name}' finished"))
    return last


def _skills_auto(session: "ConsoleSession", argument: str) -> None:
    """/skills auto [on|off|bundle on|off]: routing without being asked."""
    parts = argument.split() if argument else []
    head = parts[0].lower() if parts else ""
    rest = " ".join(parts[1:]).strip().lower()
    stored = skills_prefs()
    auto = bool(stored.get("auto", (session.config.get("skills") or {}).get("auto", True)))
    bundles = bool(
        stored.get("auto_bundle", (session.config.get("skills") or {}).get("auto_bundle", False))
    )

    if not head:
        session._print(session.style.bold("  skill auto-routing"))
        session._print(f"  {'auto':<14} {'on' if auto else 'off'}")
        session._print(f"  {'auto bundle':<14} {'on' if bundles else 'off'}")
        session._print("")
        session._print(
            session.style.dim(
                "  on - a matching skill attaches itself to a plain prompt, for that turn only"
            )
        )
        session._print(
            session.style.dim("  off - skills are used only when you name them with /skills use")
        )
        session._print("")
        session._print(session.style.dim("  /skills auto on|off · /skills auto bundle on|off"))
        return

    if head == "bundle":
        if rest not in ("on", "off"):
            session._print(session.style.dim("  usage: /skills auto bundle on|off"))
            return
        bundles = rest == "on"
        set_skills_prefs(auto_bundle=bundles)
        session._print(session.style.dim(f"  bundle auto-launch {'on' if bundles else 'off'}"))
        if bundles:
            session._print(
                session.style.warn(
                    "  a bundle runs several turns - it will start on its own when one fits"
                )
            )
        return

    if head not in ("on", "off"):
        session._print(session.style.dim("  usage: /skills auto [on|off|bundle on|off]"))
        return
    auto = head == "on"
    set_skills_prefs(auto=auto)
    # Turning routing off is also the way to say "stop guessing at this
    # conversation", so an attachment already in force goes with it.
    if not auto:
        session._detach_auto()
    session._print(session.style.dim(f"  skill auto-routing {'on' if auto else 'off'}"))


def _skills_find(session: "ConsoleSession", query: str) -> None:
    if not query:
        session._print(session.style.dim("  usage: /skills <name>  — Tab shows all, type to filter"))
        return
    hits = skills.find(query)
    if not hits:
        session._print(session.style.dim(f"  nothing matches '{query}'"))
        return
    session._print(session.style.bold(f"  skills for: {query}"))
    index = skills.routing_table()
    for found in hits:
        function = index.get(found.name.lower(), {}).get("function") or found.description
        function = " ".join(str(function).split())
        if len(function) > 60:
            function = function[:57].rstrip() + "..."
        session._print(f"  {found.name:<18} {session.style.dim(function)}")
    session._print("")
    session._print(session.style.dim("  EASY: /skills <name> to attach"))


def _goal(session: "ConsoleSession", argument: str) -> None:
    """/goal: the standing objective the whole session is working toward.

    Subcommands are checked before the free-text form, because
    ``/goal note`` is a lot more likely to be meant as a subcommand than
    as a goal whose entire text is the word "note".
    """
    argument = argument.strip()
    if not argument:
        session.show_goal()
        return
    head, _, rest = argument.partition(" ")
    head = head.lower()
    rest = rest.strip()
    if head in ("done", "clear", "drop"):
        session.clear_goal(rest or "cleared by the operator")
    elif head in ("note", "add"):
        if not rest:
            session._print(session.style.dim("  usage: /goal note <text>"))
        else:
            session.add_goal_note(rest)
    elif head in ("show", "check", "status"):
        session.show_goal()
    else:
        # Not a subcommand, so the whole line is the goal.
        session.set_goal(argument)


def _todo(session: "ConsoleSession", argument: str) -> None:
    """/todo: the session checklist the agent works through.

    Bare /todo shows the list. Subcommands: add <text>, done <n|text>
    (number, or text matching an item), rm <n|text>, clear. done and rm
    accept a space- or comma-separated list. A bare /todo done opens a
    picker over the open items; bare /todo rm removes the done ones.
    """
    argument = argument.strip()
    if not argument:
        session.show_todos()
        return
    head, _, rest = argument.partition(" ")
    head = head.lower()
    rest = rest.strip()
    if head in ("add", "new"):
        session.add_todo(rest)
    elif head in ("done", "check", "rm", "remove", "delete", "drop"):
        mark = head in ("done", "check")
        # Bare /todo done opens a picker over the open items; bare
        # /todo rm drops the done ones. Both are friendlier than a
        # usage line and make the command discoverable.
        if not rest:
            if mark:
                picks = [Option(value=str(i + 1), label=item["text"]) for i, item in enumerate(session.todos) if not item["done"]]
                if not picks:
                    session._print(session.style.dim("  nothing open to check - /todo add <what needs doing>"))
                    return
                chosen = _menu(session, "Check off", picks)
                if chosen:
                    session.mark_todo_done(chosen)
            else:
                done = [item["text"] for item in session.todos if item["done"]]
                if not done:
                    session._print(session.style.dim("  no done items to remove - /todo lists them"))
                    return
                session.todos = [item for item in session.todos if not item["done"]]
                session._print(session.style.dim(f"  removed {len(done)} done todo(s)"))
            return
        # Resolve each target: a phrase that matches an item whole wins
        # (so "fix the header" is one item, not four tokens), otherwise
        # each space/comma-separated token is its own item number.
        targets = [rest]
        if session._find_todo(rest, open_only=mark) is None:
            targets = [t for t in re.split(r"[,\s]+", rest) if t]
        for target in targets:
            if mark:
                session.mark_todo_done(target)
            else:
                session.rm_todo(target)
    elif head in ("clear", "reset"):
        session.clear_todos()
    elif head in ("show", "list", "status"):
        session.show_todos()
    else:
        session._print(session.style.dim("  usage: /todo add <text> · done <n> · rm <n> · clear"))


def _workflow(session: "ConsoleSession", argument: str) -> None:
    """/workflow create | show | launch | remove."""
    parts = argument.split() if argument else []
    head = parts[0].lower() if parts else ""
    rest = " ".join(parts[1:]).strip()

    if head in ("", "list", "show"):
        _workflow_show(session, rest)
    elif head == "create":
        _workflow_create(session, rest)
    elif head in ("launch", "run", "start"):
        _workflow_launch(session, rest)
    elif head in ("remove", "delete", "rm"):
        _workflow_remove(session, rest)
    else:
        session._print(session.style.dim("  usage: /workflow create|show|launch|remove <name>"))


def _workflow_show(session: "ConsoleSession", name: str) -> None:
    if name:
        found = workflows.get(name)
        if found is None:
            known = workflows.list_workflows()
            if known:
                choice = _menu(session, f"Workflow '{workflows.slug(name)}' not found", [Option(value=w["name"], label=w["name"], hint=f"{len(w['steps'])} steps") for w in known])
                if choice:
                    _workflow_show(session, choice)
                    return
            session._print(session.style.ember(f"  no workflow named '{workflows.slug(name)}'"))
            return
        steps = found["steps"]
        session._print(f"  {session.style.bold(found['name'])}")
        for index, step in enumerate(steps, 1):
            session._print(f"    {index}. {step}")
        session._print(session.style.dim(f"  /workflow launch {found['name']}"))
        return

    known = workflows.list_workflows()
    if not known:
        session._print(session.style.dim("  no workflows yet"))
        session._print(session.style.dim("  /workflow create <name>, then type one step per line"))
        return
    choice = _menu(session, "Show workflow", [Option(value=w["name"], label=w["name"], hint=f"{len(w['steps'])} steps") for w in known])
    if choice:
        _workflow_show(session, choice)
        return
    session._print(session.style.bold("  workflows"))
    for item in known:
        count = len(item["steps"])
        label = f"{count} step" if count == 1 else f"{count} steps"
        session._print(f"  {item['name']}  {session.style.dim(label)}")
    session._print("")
    session._print(session.style.dim("  /workflow show <name> · /workflow launch <name>"))


def _workflow_create(session: "ConsoleSession", name: str) -> None:
    if not name:
        session._print(session.style.dim("  usage: /workflow create <name>"))
        return
    session._print(
        session.style.dim(f"  steps for '{workflows.slug(name)}', one per line, . to finish:")
    )
    raw = _read_multiline(session)
    steps = [line.strip() for line in raw.split("\n") if line.strip()]
    ok, message = workflows.create(name, steps)
    colour = session.style.dim if ok else session.style.ember
    session._print(f"  {colour(message)}")


def _workflow_launch(session: "ConsoleSession", name: str) -> None:
    if not name:
        known = workflows.list_workflows()
        if not known:
            session._print(session.style.dim("  no workflows to launch"))
            session._print(session.style.dim("  usage: /workflow launch <name>"))
            return
        choice = _menu(session, "Launch workflow", [Option(value=w["name"], label=w["name"], hint=f"{len(w['steps'])} steps") for w in known])
        if not choice:
            session._print(session.style.dim("  usage: /workflow launch <name>"))
            return
        name = choice
    found = workflows.get(name)
    if found is None:
        known = workflows.list_workflows()
        if known:
            choice = _menu(session, f"Workflow '{workflows.slug(name)}' not found", [Option(value=w["name"], label=w["name"], hint=f"{len(w['steps'])} steps") for w in known])
            if choice:
                _workflow_launch(session, choice)
                return
        session._print(session.style.ember(f"  no workflow named '{workflows.slug(name)}'"))
        return
    steps = found["steps"]
    label = f"{len(steps)} step" if len(steps) == 1 else f"{len(steps)} steps"
    session._print(session.style.bold(f"  launching '{found['name']}' ({label})"))
    for index, step in enumerate(steps, 1):
        session._print("")
        session._print(session.style.dim(f"  step {index} of {len(steps)}: {step}"))
        try:
            result = session.handle(step)
        except KeyboardInterrupt:
            session._print(session.style.warn("  workflow stopped"))
            return
        if result is None:
            session._print(session.style.warn("  workflow stopped: the step did not complete"))
            return
    session._print("")
    session._print(session.style.dim(f"  workflow '{found['name']}' finished"))


def _workflow_remove(session: "ConsoleSession", name: str) -> None:
    if not name:
        known = workflows.list_workflows()
        if not known:
            session._print(session.style.dim("  no workflows to remove"))
            return
        choice = _menu(session, "Remove workflow", [Option(value=w["name"], label=w["name"], hint=f"{len(w['steps'])} steps") for w in known])
        if not choice:
            session._print(session.style.dim("  usage: /workflow remove <name>"))
            return
        name = choice
    if workflows.delete(name):
        session._print(session.style.dim(f"  removed '{workflows.slug(name)}'"))
    else:
        known = workflows.list_workflows()
        if known:
            choice = _menu(session, f"Workflow '{workflows.slug(name)}' not found", [Option(value=w["name"], label=w["name"], hint=f"{len(w['steps'])} steps") for w in known])
            if choice:
                _workflow_remove(session, choice)
                return
        session._print(session.style.ember(f"  no workflow named '{workflows.slug(name)}'"))


def _menu(
    session: "ConsoleSession",
    title: str,
    options: list[Any],
    hint: str = "",
    allow_filter: bool = True,
    cursor: int = 0,
    allow_delete: bool = False,
    on_delete: Any = None,
) -> str | None:
    """Open a selectable menu and return the chosen value.

    Inside the terminal application this is an overlay card; the caller's
    thread blocks until the operator picks or cancels. Returns None when
    cancelled or when there is nothing to show; callers treat None as
    "no change".
    """
    if not options:
        return None
    if session.ui is not None:
        return session.ui.choose(
            title,
            options,
            allow_filter=allow_filter,
            allow_delete=allow_delete,
            on_delete=on_delete,
        )
    # No application (piped runs): menus need a terminal.
    return None


def _model_options(session: "ConsoleSession", models: list[str]) -> list[Option]:
    """Model rows, marking the ones that think before answering."""
    current = session.config.get("llm", {}).get("model", "")
    options = []
    for name in models:
        hint = "thinks" if is_reasoning_model(name) else ""
        if name == current:
            hint = (hint + " · current").strip(" ·")
        options.append(Option(value=name, hint=hint))
    return options


def _effort_options(current: str | None) -> list[Option]:
    """Thinking levels, with the one in force marked.

    "off" leads because it is the safe answer for a model that does not
    reason, and it is what most models should be left on.
    """
    levels = ("off", *REASONING_EFFORTS)
    options = []
    for level in levels:
        hints = []
        if level == current:
            hints.append("current")
        if level == "off":
            hints.append("send no effort field")
        elif level == "high":
            hints.append("most thorough, slowest")
        options.append(Option(value=level, hint=" · ".join(hints)))
    return options


def _pick_effort(session: "ConsoleSession", model: str) -> str | None:
    """Offer a thinking level for the model just chosen.

    Always offered: gating on the model name was wrong often enough to
    be worse than asking.
    """
    current = session.config.get("llm", {}).get("reasoning_effort") or "off"
    options = _effort_options(current)
    index = next((i for i, o in enumerate(options) if o.value == current), 0)
    title = f"reasoning effort for {model}"
    if not is_reasoning_model(model):
        title += session.style.dim(" (this model may ignore it)")
    chosen = _menu(session, title, options, allow_filter=False, cursor=index)
    return chosen or None


def _apply_model(session: "ConsoleSession", model: str, effort: str | None = None) -> None:
    """Set a model and settle reasoning as a property of that model.

    "off" is the default rather than an inherited level, so switching to
    a model that does not reason stops sending a field chosen for
    something else instead of leaving it behind.
    """
    session.set_model(model, quiet=True)
    session.set_reasoning(effort or "off", quiet=True)
    # One line for both, because model and effort are one choice here.
    effort_now = session.config["llm"].get("reasoning_effort") or "off"
    session._print(
        session.style.dim(
            f"  model is now {model} · reasoning {effort_now}"
        )
    )
    # Remember the pairing so the next /model menu opens on it.
    set_active(model=model, reasoning_effort=session.config["llm"].get("reasoning_effort"))
    # Refresh info bar.
    if session.layout is not None and session.layout.active:
        session.layout.draw_chrome()


def _try_fetch(session: "ConsoleSession") -> tuple[list[str], Exception | None]:
    """Ask the endpoint what it serves.

    Returns the models and the failure, because the two need different
    remedies: a rejected key is fixable on the spot, while a gateway
    with no catalogue just means typing the name.
    """
    llm = session.config.get("llm", {})
    base_url = llm.get("base_url", "")
    if not base_url:
        return [], None
    try:
        return fetch_models(base_url, llm.get("api_key_env")), None
    except HarnessError as exc:
        session._print(session.style.dim(f"  (could not list models: {exc})"))
        return [], exc


# Ways out of an empty catalogue. These are menu entries rather than
# printed advice because the operator is already at the point where
# something is wrong - being told to edit a file is not a fix.
TYPE_A_MODEL = "+ type a model name"
RE_ENTER_KEY = "+ re-enter the api key"
SWITCH_ENDPOINT = "+ connect a different endpoint"


def _is_auth_failure(error: Exception | None) -> bool:
    """True when the endpoint refused the credential.

    Detected from the message rather than a typed exception because the
    client raises the harness-level error, and the only thing that
    matters here is which remedy to offer.
    """
    return error is not None and "refused the key" in str(error)


def _rescue_catalogue(session: "ConsoleSession", error: Exception | None) -> bool:
    """Offer a way forward when no model list can be had.

    An empty catalogue is usually a mistyped key, which is fixable here
    rather than by hand-editing the settings file.
    """
    s = session.style
    llm = session.config.get("llm", {})
    key_env = llm.get("api_key_env") or ""
    options: list[Option] = []
    # Offer the key only when it could plausibly be the cause. Offering
    # it for an unreachable host sends someone off to re-paste a key
    # that was never the problem; not offering it for an unknown cause
    # leaves the most common fix off the menu.
    if provider_needs_key(llm.get("base_url", ""), key_env) and (
        error is None or _is_auth_failure(error)
    ):
        options.append(Option(value=RE_ENTER_KEY, hint="most likely if the key was mistyped"))
    options.append(Option(value=TYPE_A_MODEL, hint="if the endpoint hides its list"))
    options.append(Option(value=SWITCH_ENDPOINT, hint=""))
    session._print(s.warn("  no models to choose from yet"))
    choice = _menu(session, "how do you want to fix it?", options, allow_filter=False)
    if choice == RE_ENTER_KEY:
        if not _replace_key(session):
            return False
        return _choose_model(session)
    if choice == TYPE_A_MODEL:
        return _type_a_model(session)
    if choice == SWITCH_ENDPOINT:
        return _connect(session, [])
    session._print(s.dim(f"  or add models by hand: {settings_path()}"))
    return False


def _type_a_model(session: "ConsoleSession") -> bool:
    """Take a model name from the operator and adopt it."""
    name = _read_choice(session, "  model name> ").strip()
    if not name:
        session._print(session.style.dim("  cancelled"))
        return False
    _apply_model(session, name, _pick_effort(session, name))
    endpoint = session.endpoint_name
    if endpoint:
        saved = models_for(endpoint)
        if name not in saved:
            set_models(endpoint, [*saved, name])
    return True


def _choose_model(session: "ConsoleSession") -> bool:
    """Open the model menu; effort follows as part of the same choice.

    All stored endpoints' models are shown together, and picking one
    switches to its owning endpoint automatically.
    """
    llm = session.config.get("llm", {})
    base_url = llm.get("base_url", "")
    # Build combined catalogue from all stored endpoints
    all_by_model: dict[str, str] = {}  # model -> provider
    for ep_name, entry in known_endpoints().items():
        for m in entry.get("models", []) or []:
            if m not in all_by_model:
                all_by_model[m] = ep_name
    # Prefer live catalogue from current endpoint, merged into all
    name = session.endpoint_name
    fetched, error = _try_fetch(session)
    if fetched:
        session.known_models = fetched
        if name:
            set_models(name, fetched)
        for m in fetched:
            if m not in all_by_model:
                all_by_model[m] = name or "current"
        # A very large catalogue (an aggregator like OpenRouter) is a
        # wall of names: let the operator choose how to find a model
        # before the full menu opens.
        if len(fetched) > _LARGE_MODEL_CATALOGUE and name:
            how = _menu(session, f"{len(fetched)} models at {name}", [
                Option(value=SHOW_ALL_MODELS, hint="type to filter as you go"),
                Option(value=SHOW_FIRST_MODELS, hint=f"first {_FIRST_MODEL_WINDOW} from the list"),
                Option(value=TYPE_A_MODEL, hint="if you already know the name"),
            ], allow_filter=False)
            if how == TYPE_A_MODEL:
                return _type_a_model(session)
            if how == SHOW_FIRST_MODELS:
                window = set(sorted(fetched)[:_FIRST_MODEL_WINDOW])
                all_by_model = {m: p for m, p in all_by_model.items() if m in window or p != name}
            elif not how:
                return False
            # SHOW_ALL_MODELS (or a cancel) falls through to the menu.
    elif not all_by_model:
        # No stored models anywhere and fetch failed
        if not base_url:
            session._print(session.style.warn("  no endpoint configured - /model first"))
            return False
        return _rescue_catalogue(session, error)
    else:
        # Use stored catalogue when fetch fails but we have something
        session.known_models = list(all_by_model.keys())

    # Prepare options with provider hint
    current = llm.get("model", "")
    options: list[Option] = []
    for m, provider in sorted(all_by_model.items(), key=lambda x: x[0].lower()):
        hint = provider
        if m == current:
            hint = (hint + " · current").strip(" ·") if hint else "current"
        if is_reasoning_model(m):
            hint = (hint + " · thinks").strip(" ·") if hint else "thinks"
        options.append(Option(value=m, hint=hint))
    # Always offer typing a name
    options.append(Option(value=TYPE_A_MODEL, hint="not listed above"))
    title = "models — all providers" if len(known_endpoints()) > 1 else f"models at {base_url}" if base_url else "models"

    def _on_delete_model(model: str) -> None:
        provider = all_by_model.get(model)
        if not provider:
            return
        entry = known_endpoints().get(provider)
        if not entry:
            return
        models = [m for m in entry.get("models", []) if m != model]
        set_models(provider, models)
        session._print(session.style.dim(f"  removed model '{model}' from {provider}"))
        # keep map in sync
        all_by_model.pop(model, None)

    chosen = _menu(session, title, options, allow_delete=True, on_delete=_on_delete_model)
    if not chosen:
        return False
    if chosen == TYPE_A_MODEL:
        return _type_a_model(session)
    # Auto-switch endpoint if model belongs to different provider
    provider = all_by_model.get(chosen)
    if provider and provider != name:
        session._print(session.style.dim(f"  switching to {provider} for {chosen}"))
        session.use_endpoint(provider)
    _apply_model(session, chosen, _pick_effort(session, chosen))
    return True


NEW_ENDPOINT = "+ add a new endpoint"


def _endpoint_options(session: "ConsoleSession") -> list[Option]:
    """Saved endpoints, alphabetically, with an entry to add another."""
    known = known_endpoints()
    current = session.endpoint_name
    options = []
    for name in sorted(known):
        entry = known[name]
        hint = "current" if name == current else entry.get("base_url", "")
        options.append(Option(value=name, hint=hint))
    options.append(Option(value=NEW_ENDPOINT, hint=""))
    return options


def _connect_choose_endpoint(session: "ConsoleSession") -> str | None:
    """Menu over saved endpoints. None means the operator cancelled. Press d to remove."""
    known = known_endpoints()
    if not known:
        return NEW_ENDPOINT

    def _on_delete(name: str) -> None:
        # Remove endpoint and its key in one go (baseurl + key together)
        entry = known_endpoints().get(name.lower())
        key_env = entry.get("api_key_env") if entry else ""
        removed = remove_endpoint(name.lower())
        if removed:
            # Also remove stored key if only this endpoint used it
            key_removed = False
            key_removal_failed = False
            still_used = False
            if key_env:
                still_used = any(
                    e.get("api_key_env") == key_env
                    for n, e in known_endpoints().items()
                    if n != name.lower()
                )
                if not still_used:
                    try:
                        from core.agent.keys import remove as remove_key

                        remove_key(key_env)
                        key_removed = True
                    except Exception:
                        key_removal_failed = True
            if key_removed:
                session._print(session.style.dim(f"  removed '{name}' (+ key {key_env})"))
            elif key_removal_failed:
                session._print(session.style.dim(f"  removed '{name}'"))
                session._print(session.style.warn(f"  warning: could not remove stored key {key_env}"))
            elif key_env and still_used:
                session._print(session.style.dim(f"  removed '{name}' (kept key {key_env} — still used by another endpoint)"))
            else:
                session._print(session.style.dim(f"  removed '{name}'"))

    return _menu(
        session,
        "endpoints",
        _endpoint_options(session),
        allow_filter=False,
        allow_delete=True,
        on_delete=_on_delete,
    )


def _store_key(session: "ConsoleSession", key_env: str, key: str) -> bool:
    """Persist a key; a failed write surfaces as an error line, not a crash."""
    try:
        store_key(key_env, key)
        return True
    except OSError as exc:
        session._print(session.style.ember(f"  could not save the key: {exc}"))
        return False


def _replace_key(session: "ConsoleSession", name: str = "") -> bool:
    """Store a key over whatever is already there.

    Always prompts, even when a key is stored: a mistyped key used to
    be permanent because /model skipped the prompt once the store
    held any value at all.
    """
    s = session.style
    llm = session.config.get("llm", {})
    name = (name or session.endpoint_name or "").lower()
    entry = known_endpoints().get(name)
    if entry is not None:
        base_url = entry.get("base_url", "")
        key_env = entry.get("api_key_env") or _derive_key_env(name)
    else:
        base_url = llm.get("base_url", "")
        if not base_url:
            session._print(s.warn("  no endpoint to set a key for - /model first"))
            return False
        name = _derive_name(base_url)
        key_env = _derive_key_env(name)

    if not provider_needs_key(base_url, key_env):
        session._print(s.dim(f"  {base_url} does not take a key"))
        return False
    if os.environ.get(key_env):
        # The environment wins over the store, so a file key cannot
        # rescue a bad value that came from a variable. Say so rather
        # than accepting a key that will never be used.
        session._print(s.warn(f"  ${key_env} is set in this shell and wins over stored keys"))
        session._print(s.dim(f"  clear it with: set {key_env}=   then re-run /model key"))

    stored = stored_keys().get(key_env, "")
    if stored:
        session._print(s.dim(f"  {name} is using {mask(stored)}"))
    try:
        key = _read_secret(session, f"  new api key for {name} (hidden, enter confirms)> ").strip()
        if not key:
            # Fallback for tests that mock _read_choice instead of _read_secret
            alt = _read_choice(session, f"  new api key for {name} (visible, blank cancels)> ").strip()
            if alt:
                key = alt
    except Exception:
        key = _read_choice(session, f"  new api key for {name} (visible, blank cancels)> ").strip()
    if not key:
        session._print(s.dim("  cancelled"))
        return False
    if not _store_key(session, key_env, key):
        return False
    session._print(s.dim(f"  key stored ({mask(key)})"))
    return True


def _connect_new(session: "ConsoleSession", url: str = "", key: str = "", model: str = "") -> bool:
    """Walk through adding an endpoint: URL, key, then pick a model."""
    s = session.style
    if not url:
        url = _read_choice(session, "  endpoint url (e.g. https://api.openai.com/v1)> ").strip()
        if not url:
            session._print(s.dim("  tip: paste a full URL, or try /model list to see saved ones"))
            session._print(s.dim("  examples: /model https://api.openai.com/v1  ·  /model https://api.meta.ai/v1"))
            return False
    if "://" not in url:
        # Tolerate a host typed without a scheme rather than failing.
        # Anything that already carries a scheme is left alone, so a
        # wrong one is reported instead of being prefixed into nonsense.
        url = "https://" + url
    url = url.rstrip("/")
    problem = validate_endpoint({"base_url": url})
    if problem:
        session._print(s.ember(f"  {problem}"))
        return False
    if not urlparse(url).hostname:
        session._print(s.ember("  that does not look like a hostname"))
        return False

    name = _derive_name(url)
    key_env = _derive_key_env(name)

    if key:
        if not _store_key(session, key_env, key):
            return False
        session._print(s.dim(f"  key stored ({mask(key)})"))
    elif provider_needs_key(url, key_env):
        # Always prompt for key in interactive add (visible), show existing
        existing = os.environ.get(key_env) or stored_keys().get(key_env, "")
        if existing:
            session._print(s.dim(f"  current key {mask(existing)} ({key_env}) — press enter to keep, or paste new"))
        key = _read_choice(session, f"  api key for {name}> ").strip()
        if key:
            if not _store_key(session, key_env, key):
                return False
            # Also clear env if it was wrong and now stored wins after restart; advise
            if os.environ.get(key_env) and os.environ.get(key_env) != key:
                session._print(s.warn(f"  note: ${key_env} env still set and wins until you restart shell"))
            session._print(s.dim(f"  key stored ({mask(key)})"))
        elif not existing:
            session._print(s.warn("  no key given - skipping the fetch"))
            session._print(s.dim("  store one later with /model key, or add one to"))
            session._print(s.dim(f"  {settings_path()}"))
            return False
        # else keep existing

    try:
        add_endpoint(name, url, key_env, note=f"added {time.strftime('%Y-%m-%d')}")
    except ValueError as exc:
        session._print(s.ember(f"  {exc}"))
        return False
    session._print(s.dim(f"  saved '{name}' > {url}"))
    if not session.use_endpoint(name):
        return False
    # If a model was supplied inline (3-arg form), use it directly
    if model:
        _apply_model(session, model)
        session.refresh_title()
        return True
    session.refresh_title()
    # Endpoint + key done — now fetch models in same flow
    session._print(s.dim("  fetching models…"))
    picked = _choose_model(session)
    # If fetch failed or user cancelled, hint one-liner
    if not picked:
        session._print(s.dim("  tip: /model <url> <key> <model> to set in one go"))
    return picked


def _connect(session: "ConsoleSession", args: list[str]) -> bool:
    """Add or switch endpoints, then pick a model from the menu.

    Only a base URL and a key are needed; the catalogue comes from the
    endpoint itself. /model is the command surface; this is the internal
    endpoint flow behind it.
    """
    # Subcommands first — before treating args as url
    if args and args[0].lower() in ("remove", "forget", "delete", "rm"):
        if len(args) >= 2:
            _connect_remove(session, args[1])
        else:
            eps = sorted(known_endpoints().keys())
            if not eps:
                session._print(session.style.dim("  no endpoints to remove"))
            else:
                choice = _menu(session, "Remove endpoint", [Option(value=n, label=n, hint=known_endpoints()[n].get("base_url","")) for n in eps])
                if choice:
                    _connect_remove(session, choice)
        return True
    if args and args[0].lower() in ("key", "keys"):
        if len(args) >= 3:
            # /model key <name> <key> — direct replace no prompt.
            # The endpoint may carry a custom api_key_env; deriving the
            # env name from the short name would store an orphan key
            # under the wrong variable that the resolver never reads.
            entry = known_endpoints().get(args[1].lower())
            key_env = (entry or {}).get("api_key_env") or _derive_key_env(args[1].lower())
            if not _store_key(session, key_env, args[2]):
                return False
            # The shared mask degrades short values to asterisks; slicing
            # fixed windows here once printed short keys almost in full.
            session._print(session.style.dim(f"  key stored for {args[1].lower()} ({mask(args[2])})"))
            return True
        if len(args) == 2:
            return _replace_key(session, args[1])
        return _replace_key(session, "")
    if len(args) >= 3:
        # Scripted form: /model <url> <key> <model>
        return _connect_new(session, args[0], args[1], args[2])
    if len(args) >= 2:
        # Scripted form: /model <url> <key>
        return _connect_new(session, args[0], args[1])
    if len(args) == 1:
        if args[0].lower() in ("list", "show"):
            session.show_endpoints()
            return True
        # A saved endpoint's own name means "switch to it". Saved names
        # never contain a dot or a slash, so an exact match cannot be a
        # host someone meant to add - and without this, `/model groq`
        # would quietly invent https://groq and ask for a key.
        saved = known_endpoints().get(args[0].lower())
        if saved:
            return session.use_endpoint(args[0].lower())
        return _connect_new(session, args[0])

    choice = _connect_choose_endpoint(session)
    if not choice:
        return False
    if choice == NEW_ENDPOINT:
        return _connect_new(session)
    if not session.use_endpoint(choice):
        return False
    # Refresh info bar after endpoint change.
    if session.layout is not None and session.layout.active:
        session.layout.draw_chrome()
    return _choose_model(session)


def _connect_remove(session: "ConsoleSession", name: str) -> None:
    if remove_endpoint(name.lower()):
        session._print(session.style.dim(f"  removed '{name}'"))
    else:
        session._print(session.style.warn(f"  no endpoint named '{name}'"))


# ---------------------------------------------------------------- /model

# A catalogue this large (an aggregator such as OpenRouter) is a wall of
# names: offer the operator a choice of how to find a model instead of
# dumping the whole list into a menu.
_LARGE_MODEL_CATALOGUE = 40
_FIRST_MODEL_WINDOW = 20

ADD_ENDPOINT = "+ add a provider / endpoint"
PICK_MODEL = "pick a model"
SWITCH_ENDPOINT_ENTRY = "switch endpoint"
REPLACE_KEY_ENTRY = "replace the api key"
REMOVE_ENDPOINT_ENTRY = "remove an endpoint"
SHOW_ALL_MODELS = "pick from the full list"
SHOW_FIRST_MODELS = "show the first few models"


def _model_help(session: "ConsoleSession") -> None:
    """The merged provider-and-model help for /model."""
    s = session.style
    session._print(s.bold("  /model — provider & model, one place"))
    session._print(s.dim("  usage:"))
    session._print("    /model                          — menu: add a provider, pick a model")
    session._print("    /model <name>                   — switch to a model directly")
    session._print("    /model <name> <effort>          — switch and set reasoning")
    session._print("    /model <url> [key] [model]      — add a provider, then pick a model")
    session._print("    /model <endpoint-name>          — switch provider")
    session._print("    /model list                     — show saved providers")
    session._print("    /model remove <name>            — delete a provider")
    session._print("    /model key [name]               — replace a stored key")
    session._print(s.dim("  effort: off | minimal | low | medium | high | xhigh"))
    session._print(s.dim("  examples: /model gpt-4o   ·   /model gpt-5 high   ·   /model https://api.openai.com/v1"))


def _model_master(session: "ConsoleSession") -> bool:
    """The simple entry for non-technical operators: one menu manages the
    provider and the model together."""
    eps = known_endpoints()
    model = session.config.get("llm", {}).get("model", "?")
    current = session.endpoint_name or "no endpoint"
    options = [Option(value=ADD_ENDPOINT, hint="paste a URL like https://api.openai.com/v1")]
    if eps:
        options.append(Option(value=PICK_MODEL, hint="from the endpoint's catalogue"))
        options.append(Option(value=TYPE_A_MODEL, hint="not listed above"))
        if len(eps) > 1:
            options.append(Option(value=SWITCH_ENDPOINT_ENTRY, hint=""))
        options.append(Option(value=REPLACE_KEY_ENTRY, hint=""))
        options.append(Option(value=REMOVE_ENDPOINT_ENTRY, hint=""))
    choice = _menu(session, f"model & endpoint — {current} · {model}", options, allow_filter=False)
    if not choice:
        llm = session.config.get("llm", {})
        session._print(f"model      {llm.get('model', '?')}")
        session._print(f"endpoint   {llm.get('base_url', '?')}")
        return False
    if choice == ADD_ENDPOINT:
        _connect_new(session)
    elif choice == PICK_MODEL:
        _choose_model(session)
    elif choice == TYPE_A_MODEL:
        _type_a_model(session)
    elif choice == SWITCH_ENDPOINT_ENTRY:
        picked = _connect_choose_endpoint(session)
        if not picked:
            return False
        if picked == NEW_ENDPOINT:
            _connect_new(session)
        elif session.use_endpoint(picked):
            _choose_model(session)
    elif choice == REPLACE_KEY_ENTRY:
        names = sorted(known_endpoints().keys())
        if len(names) == 1:
            _replace_key(session, names[0])
        elif names:
            picked = _menu(session, "Replace key for", [Option(value=n, label=n, hint=known_endpoints()[n].get("base_url", "")) for n in names])
            if picked:
                _replace_key(session, picked)
        else:
            session._print(session.style.dim("  no endpoints yet - add one with /model"))
    elif choice == REMOVE_ENDPOINT_ENTRY:
        names = sorted(known_endpoints().keys())
        if not names:
            session._print(session.style.dim("  no endpoints to remove"))
        else:
            picked = _menu(session, "Remove endpoint", [Option(value=n, label=n, hint=known_endpoints()[n].get("base_url", "")) for n in names])
            if picked:
                _connect_remove(session, picked)
    return True


def _model_command(session: "ConsoleSession", parts: list[str]) -> bool:
    """The single provider-and-model command.

    The bare form opens one simple menu; one-liners cover the rest so
    scripts and power users keep working.
    """
    if not parts:
        return _model_master(session)
    first = parts[0].lower()
    if first in ("help", "-h", "--help", "?", "h"):
        _model_help(session)
        return True
    # Endpoint management and the <url> [key] [model] add-form reuse the
    # internal _connect flow.
    if first in ("list", "show", "remove", "forget", "delete", "rm", "key", "keys") or "://" in first:
        return _connect(session, parts)
    saved = known_endpoints().get(first)
    if saved:
        # /model <endpoint-name> — switch provider (then pick a model).
        return _connect(session, parts)
    # Otherwise it is a model name, with an optional reasoning effort.
    effort = parts[1] if len(parts) > 1 and parts[1] in (*REASONING_EFFORTS, "off") else None
    if effort is not None:
        _apply_model(session, parts[0], effort)
    else:
        # A bare model name still asks about reasoning as part of the
        # same choice.
        _apply_model(session, parts[0], _pick_effort(session, parts[0]))
    return True


def show_keys(session: "ConsoleSession") -> None:
    """List stored keys by masked value only - never the key itself."""
    known = stored_keys()
    if not known:
        session._print(session.style.dim("  no keys stored yet"))
        session._print(session.style.dim("  /model stores one when you add an endpoint"))
        return
    session._print(session.style.bold("  stored keys"))
    for name in sorted(known):
        session._print(f"  {name:<24} {session.style.dim(mask(known[name]))}")


def _read_one() -> str:
    """One keystroke, without waiting for a line."""
    if os.name == "nt":
        import msvcrt

        return msvcrt.getwch()
    return sys.stdin.read(1)


def _read_secret(
    session: "ConsoleSession",
    prompt_text: str,
) -> str:
    """Read a key without echoing it.

    Inside the terminal application the answer is typed into a masked
    card. Returns "" when there is no terminal to read from.
    """
    if session is not None and session.ui is not None:
        try:
            return session.ui.ask_line(prompt_text, secret=True)
        except Exception:
            return ""
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return ""
    # The prompt can carry a user-supplied endpoint name; on a narrow
    # locale that must degrade, not crash the secret reader.
    safe_write(prompt_text)
    sys.stdout.flush()
    chars: list[str] = []
    try:
        from core.term import raw_mode

        with raw_mode():
            while True:
                char = _read_one()
                if char in ("\r", "\n"):
                    break
                if char == "\x03":  # ctrl+c
                    raise KeyboardInterrupt
                if char == "\x04":  # ctrl+d on an empty answer cancels
                    if not chars:
                        break
                    continue
                if char in ("\x7f", "\b"):
                    if chars:
                        chars.pop()
                elif len(char) == 1 and char >= " ":
                    chars.append(char)
    except KeyboardInterrupt:
        sys.stdout.write("\n")
        sys.stdout.flush()
        return ""
    sys.stdout.write("\n")
    sys.stdout.flush()
    return "".join(chars).strip()


def _ask_secret(session: "ConsoleSession", label: str) -> str:
    """Read a key for ``session`` through its terminal application."""
    return _read_secret(session, label)


def _derive_name(url: str) -> str:
    """A short handle for an endpoint, e.g. ``https://api.openai.com/v1``.

    Strips the ``api.`` and ``www.`` prefixes and the port, then keeps
    the first label: ``api.openai.com`` -> ``openai``. A path-derived
    suffix avoids collisions between endpoints on one host.
    """
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if not host:
        return "endpoint"
    for prefix in ("api.", "www."):
        if host.startswith(prefix):
            host = host[len(prefix) :]
            break
    host = host.split(":")[0]
    label = host.split(".")[0] if host else ""
    base = re.sub(r"[^a-z0-9]+", "", label) or "endpoint"
    # Add path-derived suffix when useful (last non-v segment)
    path = parsed.path.strip("/").lower()
    if path:
        parts = [p for p in path.split("/") if p not in ("v1", "v2", "v3", "api")]
        if parts:
            suffix = re.sub(r"[^a-z0-9]+", "", parts[-1])
            if suffix and suffix != base:
                return f"{base}-{suffix}"
    return base


def _derive_key_env(name: str) -> str:
    """``openai`` -> ``OPENAI_API_KEY``."""
    return re.sub(r"[^A-Z0-9]+", "_", name.upper()).strip("_") + "_API_KEY"


def _apply_endpoint_override(config: dict, url: str) -> None:
    """Point the llm section at ``url`` and pick the matching key env.

    The key env must follow the URL, not inherit the config default:
    the default is always truthy, so chaining a derivation onto it
    would never fire. Prefer the saved endpoint entry for this URL,
    then fall back to deriving from the hostname.
    """
    llm = config.setdefault("llm", {})
    llm["base_url"] = url.rstrip("/")
    ep_name = endpoint_name_for_url(url)
    if ep_name:
        entry = known_endpoints().get(ep_name) or {}
        llm["api_key_env"] = entry.get("api_key_env") or _derive_key_env(ep_name)
    else:
        llm["api_key_env"] = _derive_key_env(_derive_name(url))



def _needs_first_run(session: "ConsoleSession") -> bool:
    """True when no usable credential is configured yet.

    Used to walk a first-time operator through setup instead of greeting
    them with a dashboard that cannot reach anything.
    """
    llm = session.config.get("llm", {})
    base_url = llm.get("base_url", "")
    key_env = llm.get("api_key_env") or ""
    if not base_url:
        return True
    if provider_needs_key(base_url, key_env):
        return not (os.environ.get(key_env) or has_stored(key_env))
    return False


def _strip_leading_invisible(s: str) -> str:
    """Strip BOM, zero-width and other invisible leading characters."""
    # Include BOM, ZWSP, ZWNJ, ZWJ, NBSP, ideographic space and normal whitespace
    return s.lstrip("\ufeff\u200b\u200c\u200d\u00a0\u3000 \t\r\n")

def dispatch(session: ConsoleSession, line: str) -> bool:
    """Run a slash command. Returns True when the line was a command."""
    # Be forgiving: strip invisible leading chars and normalize full-width forms
    stripped = _strip_leading_invisible(line)
    # Normalize full-width characters that look like slash or at-sign
    if stripped.startswith("\uff0f"):
        stripped = "/" + stripped[1:]
    if stripped.startswith("／"):
        stripped = "/" + stripped[1:]
    if not stripped.startswith("/"):
        return False
    # Strip trailing punctuation that is often typed accidentally after a command
    stripped = stripped.rstrip(".,;:!?)'\"`")
    # Also handle quoted commands like "/help" or '/help'
    stripped = stripped.strip("'\"`")
    parts = stripped.split(None, 1)
    command = parts[0].lower().rstrip(".,;:!?")
    # Normalize any internal full-width slash
    command = command.replace("\uff0f", "/").replace("／", "/")
    argument = parts[1].strip() if len(parts) > 1 else ""
    # Also strip surrounding quotes from argument for robustness
    argument = argument.strip("'\"`")

    if command in ("/exit", "/quit"):
        # Persist the pager state at exit time: content paged through
        # since the last autosave is dropped, what remains survives an
        # interrupted resume. Silent when there is nothing to save yet.
        try:
            session.autosave()
        except Exception:
            pass
        session._print("bye")
        raise SystemExit(0)
    if command in ("/help", "/"):
        session._print(HELP_TEXT)
    elif command == "/workspace":
        session.show_workspace()
    elif command == "/memory":
        mem = session.memory_path
        session._print(f"memory file: {mem}")
        if not os.path.isfile(mem):
            session._print("(empty)")
        else:
            try:
                # Cap the memory display even though the file itself is capped.
                if os.path.getsize(mem) > 20000:
                    with open(mem, encoding="utf-8", errors="replace") as h:
                        data = h.read(8000) + "\n... [truncated]"
                else:
                    with open(mem, encoding="utf-8", errors="replace") as h:
                        data = h.read()
                session._print(data or "(empty)")
            except OSError as exc:
                session._print(session.style.ember(f"  cannot read memory: {exc}"))
    elif command == "/diff":
        # In the TUI the changeset opens in the full-screen review;
        # everywhere else it stays the plain text diff.
        layout = session.layout
        if layout is not None and layout.active:
            diff = session._git("diff", "--no-color", "--unified=3") or ""
            if diff and getattr(layout, "open_review", None):
                layout.open_review(diff)
            else:
                session.show_diff()
        else:
            session.show_diff()
    elif command == "/fix":
        # Send the most recent failed command/tool result to the agent
        # for a diagnosis and a suggested (never auto-run) fix.
        prompt = session._fix_prompt(argument.strip())
        if prompt is None:
            session._print(session.style.dim("  no recent failure to fix"))
        else:
            session.handle(prompt)
    elif command == "/undo":
        session.undo_changes()
    elif command == "/model":
        # One command for providers and models; /connect is gone so a
        # single name is advertised and nothing else drifts in.
        _model_command(session, argument.split())
    elif command in ("/reasoning", "/effort"):
        # Reasoning is a property of the model, so this is now the model
        # menu. Kept as an alias so muscle memory still lands somewhere.
        if argument:
            session.set_reasoning(argument)
        elif not _choose_model(session):
            session.show_reasoning()
    elif command == "/approve":
        if not argument:
            # Modes are a fixed list, so they get a menu too.
            chosen = _menu(
                session,
                "approval mode",
                [Option(value=m, hint="current" if m == session.approvals.mode else "")
                 for m in MODES],
                allow_filter=False,
            )
            if chosen and chosen in MODES:
                session.approvals.mode = chosen
                session.approvals.reset_session()
                session._print(f"approval mode is now {chosen}")
                if session.layout is not None and session.layout.active:
                    session.layout.draw_chrome()
            else:
                session._print(f"approval mode: {session.approvals.mode}  (choose: {'/'.join(MODES)})")
        elif argument in MODES:
            session.approvals.mode = argument
            session.approvals.reset_session()
            session._print(f"approval mode is now {argument}")
            if session.layout is not None and session.layout.active:
                session.layout.draw_chrome()
        else:
            session._print(f"unknown mode '{argument}'; choose one of {'/'.join(MODES)}")
    elif command == "/cost":
        arg = argument.strip().lower()
        session.show_cost(
            compact=arg in ("compact", "brief", "c", "--compact"),
            as_json=arg in ("json", "j", "--json"),
        )
    elif command == "/compact":
        if not session.compact():
            session._print("nothing to compact")
    elif command in ("/clear", "/reset"):
        # /reset is a hidden alias: clearing the conversation is one
        # action, and advertising two names for it is how they drift.
        session.message_count = 0
        session.context.replace_body([])
        session.approvals.reset_session()
        session.reported_changes.clear()
        session.goal = ""
        session.goal_notes = []
        session.todos = []
        session.active_skills = []
        session.auto_attached = []
        if session.layout is not None and session.layout.active:
            session.layout.clear_content()
        session._print("conversation cleared (files kept)")
    elif command == "/sessions":
        # The session manager: browse (panel in the TUI, text picker
        # elsewhere), list, or resume a named session directly.
        parts = argument.split() if argument else []
        layout = session.layout
        if not parts:
            if layout is not None and layout.active and getattr(layout, "open_sessions", None):
                layout.open_sessions()
            else:
                session.pick_session()
        elif parts[0] in ("list", "show"):
            session.show_sessions()
        else:
            session.resume_session(parts[0])
    elif command == "/goal":
        _goal(session, argument)
    elif command == "/todo":
        _todo(session, argument)
    elif command == "/workflow":
        _workflow(session, argument)
    elif command in ("/skills", "/skill"):
        _skills(session, argument)
    elif command == "/verbose":
        session.verbose = not session.verbose
        session._print(f"verbose {'on' if session.verbose else 'off'}")
    else:
        # A line like "/tmp/x" or "/usr/local/bin" is a path, not a
        # command; hand it to the agent instead of swallowing it. A
        # single-token "/typo" keeps the unknown-command error.
        if "/" in command[1:] or "/" in argument:
            return False
        session._print(f"unknown command '{command}' - /help for the list")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mantra-console", description="MANTRA interactive console")
    parser.add_argument("--config", default=_resolve_data_path("examples", "config.json"))
    parser.add_argument("--workspace", default=None, help="Persistent working folder (default: <project>/workspace)")
    parser.add_argument("--once", default=None, metavar="MSG", help="Handle one message non-interactively, then exit")
    parser.add_argument("--model", default=None, help="Override the configured model")
    parser.add_argument("--reasoning", default=None, metavar="LEVEL",
                        help=f"Thinking effort: {', '.join(REASONING_EFFORTS)}, or off")
    parser.add_argument("--endpoint", default=None, metavar="URL",
                        help="Override the endpoint base URL for this run")
    parser.add_argument("--approve", default=None, choices=list(MODES), help="Override the approval mode")
    parser.add_argument("--plain", action="store_true", help="Disable ANSI styling")
    parser.add_argument("--compact", action="store_true", help="(kept for compatibility; the full-screen console is the only surface)")
    args = parser.parse_args(argv)

    # Windows consoles start on cp1252; switching both the console codepage
    # and the Python streams to UTF-8 is what lets arrows and box-drawing
    # characters render instead of raising or degrading to "?".
    force_utf8_output()

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        # Same contract as the headless runner: a bad config file is a
        # two-line diagnosis, never a traceback.
        print(f"error: {exc}", file=sys.stderr)
        return 2
    # Apply the user's saved active pick so the startup check and header
    # reflect the chosen endpoint instead of the example default.
    # Failures here are non-fatal: defaults and explicit flags still apply.
    try:
        act = get_active()
        ep_name = (act.get("endpoint") or "").strip()
        if ep_name and not args.endpoint:
            ep = known_endpoints().get(ep_name.lower())
            if ep and ep.get("base_url"):
                llm_cfg = config.setdefault("llm", {})
                llm_cfg["base_url"] = ep["base_url"]
                if ep.get("api_key_env"):
                    llm_cfg["api_key_env"] = ep["api_key_env"]
                if not args.model and act.get("model"):
                    llm_cfg["model"] = act["model"]
                if not args.reasoning and "reasoning_effort" in act:
                    llm_cfg["reasoning_effort"] = act["reasoning_effort"]
    except Exception:
        pass
    if args.endpoint:
        url = args.endpoint.rstrip("/")
        if not url.startswith(("http://", "https://")):
            parser.error("--endpoint must start with http:// or https://")
        _apply_endpoint_override(config, url)
    if args.model:
        config.setdefault("llm", {})["model"] = args.model
    if args.reasoning:
        level = args.reasoning.strip().lower()
        config.setdefault("llm", {})["reasoning_effort"] = (
            None if level in ("off", "none") else level
        )
        if level not in ("off", "none") and level not in REASONING_EFFORTS:
            parser.error(
                f"unknown reasoning level '{args.reasoning}'; "
                f"choose from {', '.join(REASONING_EFFORTS)} or off"
            )
    if args.approve:
        config["approvals"] = args.approve

    # Color follows the stream: a real terminal gets ANSI, a pipe or
    # redirect gets clean text unless the operator forces color.
    # NO_COLOR (the cross-tool convention) always wins.
    if args.plain or os.environ.get("NO_COLOR") is not None:
        color_on = False
    else:
        mode = os.environ.get("MANTRA_COLOR", "auto").lower()
        forced = os.environ.get("MANTRA_FORCE_COLOR", "").lower() in ("1", "true", "yes")
        color_on = mode == "always" or (mode != "never" and (sys.stdout.isatty() or forced))
    style = Style(enabled=color_on)
    workspace = args.workspace or _infer_workspace()
    session = ConsoleSession(config, workspace, style)

    interactive = sys.stdin.isatty() and sys.stdout.isatty()

    if args.once is not None:
        session._warn_if_key_missing()
        result = session.handle(args.once)
        # handle() returns None when the run was interrupted or failed
        # before producing a result: that is a failure, not a success.
        if result is None or result.stopped_reason in ("error", "aborted"):
            return 1
        return 0

    if not interactive:
        # Piped runs keep the plain read-eval-print loop.
        session.banner()
        _repl_plain(session)
        return 0
    return _run_terminal(session)


def _repl_plain(session: ConsoleSession, reader: Any = None) -> None:
    """The plain read-eval-print loop for piped (non-tty) runs.

    ``reader`` supplies one line per call when a caller drives the loop
    with a script instead of the console.
    """
    while True:
        try:
            line = (reader("MANTRA > ") if reader is not None else input("MANTRA > ")).strip()
        except (KeyboardInterrupt, EOFError):
            print("bye")
            return
        if not line:
            continue
        try:
            if not dispatch(session, line):
                session.handle(line)
        except SystemExit:
            return


def _run_terminal(session: ConsoleSession) -> int:
    """Interactive entry: the terminal application owns everything."""
    from core.tui.app import TuiApp

    app = TuiApp(session)
    app.composer.completer = ConsoleCompleter(session)
    session._warn_if_key_missing()
    needs_setup = _needs_first_run(session)
    try:
        if needs_setup:
            # Nothing usable is configured; walk the operator through it
            # inside the running application so menus and prompts work.
            session._print(session.style.dim("no endpoint configured yet - let's connect one."))
            app.run_detached(lambda: _connect(session, []))
        app.start()
    finally:
        try:
            session.autosave()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
