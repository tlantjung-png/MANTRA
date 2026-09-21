# Split out of core/console.py; import it from core.console, never from here.

"""Console presentation: ANSI styling, markdown-lite rendering, and the
streaming renderer that applies it to model output as it arrives."""

from __future__ import annotations

import os
import re
import sys
from typing import Any

from core import theme
from core.term import enable_vt, term_size, visible_len
from core.tui.transcript import wrap_ansi  # styled-aware wrap for table cells


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
_ANSI_SANITIZE_RE = re.compile(r"\x1b(?:\[[0-9;?]*[ -/]*[@-~]|\][^\x07]*\x07|\].*?\x1b\\)", re.DOTALL)


def _sanitize_output(text: str) -> str:
    """Strip raw ANSI escape sequences from model output.

    Prevents cursor movement, color changes, or other terminal
    manipulation from untrusted model-generated text.
    """
    return _ANSI_SANITIZE_RE.sub("", text)


def operator_line(style: "Style", text: str, stamp: str = "") -> str:
    """One operator line, rendered identically live and on replay.

    The live submit passes the send time for the chip; a resumed session
    has no per-message timestamp, so the replay passes none rather than
    fabricating one. The accent colour with no "you" word is the shared
    marker - the label the live surface deliberately does not show.
    """
    body = _sanitize_output(text)
    chip = f"\033[2m\033[{theme.CHIP_BG}m{stamp}\033[0m " if stamp else ""
    return chip + style._wrap(theme.BLOOD, body)


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


def _truncate_codepoint(text: str, limit: int) -> str:
    """Truncate *text* at *limit* bytes without splitting a UTF-8 sequence.

    The byte-aware cut keeps the cap measured consistently and never
    leaves a multibyte character half-decoded.
    """
    return text.encode("utf-8")[:limit].decode("utf-8", errors="ignore")


class StreamingRenderer:
    """Apply inline markdown formatting to streamed text fragments.

    Buffers incoming text until a newline arrives, then processes the
    complete line through the full markdown pipeline.  Code fences are
    tracked across pieces so content inside them stays literal.

    ``report_hook`` is an optional callback the session sets while a turn
    streams: called with each ``TODO ADD:`` / ``TODO DONE:`` line the
    agent emits (outside code fences) and expected to return the styled
    line to show instead of the raw protocol text. Without a hook the
    renderer behaves exactly as if the hook were absent.
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
                # The hook swallowed the line (report already applied
                # earlier in this stream, or a done item re-reported):
                # render nothing rather than the raw protocol text.
                return ""
        return _render_md_line(line, self.style, self)

    def render_piece(self, piece: str) -> str:
        """Render a text fragment with inline markdown."""
        self._buf += _sanitize_output(piece)
        # Bound single-line growth without newlines to avoid unbounded
        # memory. Render the head chunk without injecting a line break
        # (a forced break could split an inline-code span or a fence
        # opener and desync the fence state machine), and drop the
        # oldest tail explicitly - with a visible note - when the
        # buffer still exceeds 50k chars.
        if len(self._buf) > 30000 and "\n" not in self._buf:
            chunk = self._buf[:20000]
            self._buf = self._buf[20000:]
            rendered = _render_md_line(chunk, self.style, self)
            if len(self._buf) > 50000:
                self._buf = self._buf[-50000:]
                rendered += "\n" + self.style.dim(
                    "[streamed line truncated - renderer buffer capped at 50k chars]"
                ) + "\n"
            return rendered
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
    # A row wider than 20 columns cannot fit a terminal grid; cap the
    # layout so the width budget below can never go negative.
    ncols = min(ncols, 20)
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
        term_cols = 80  # piped output or exotic terminal; a sane default
    budget = max(1, max(60, min(100, term_cols - 4)) - 3 * (ncols - 1))
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
        or len({len(_table_cells(ln)) for ln in lines}) == 1
    ):
        return "\n".join(_inline_md(ln, style) for ln in lines)
    return _render_table([_table_cells(ln) for ln in lines], style)


def _syntax_highlight(line: str, style: Style) -> str:
    """Generic syntax highlight for any language — Blood & Bone: muted sage
    strings, bold-bone keywords, stone numerals. Single-pass to avoid ANSI
    nesting; types stay in the default face."""
    # Combined pattern with named groups — one pass, no re-highlight of inserted ANSI
    pattern = re.compile(
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
    segment = re.sub(
        r'!\[([^\]]*)\]\([^)]*\)',
        lambda m: m.group(1),
        segment,
    )
    # Links — the crimson accent (interactive affordance).
    segment = re.sub(
        r'\[([^\]]+)\]\([^)]+\)',
        lambda m: style._wrap(theme.LINK, m.group(1).replace("\\[", "[").replace("\\]", "]")),
        segment,
    )
    # Bold — strong bone.
    segment = re.sub(r'\*\*(.+?)\*\*', lambda m: style._wrap(theme.BONE_BOLD, m.group(1)), segment)
    # Italic — ash.
    segment_esc_star = segment.replace("\\*", _ESC_STAR)
    segment_esc_star = re.sub(r'(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)', lambda m: style._wrap(theme.ASH_ITAL, m.group(1)), segment_esc_star)
    segment = segment_esc_star.replace(_ESC_STAR, "*")
    # Strikethrough.
    segment = re.sub(r'~~(.+?)~~', lambda m: style.strike(m.group(1)), segment)
    # Restore the protected code spans in sage.
    for i, span in enumerate(spans):
        segment = segment.replace(_CS % (i + 1), style._wrap(theme.SAGE, span))
    return segment.replace(_ESC, "`")
