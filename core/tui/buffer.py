"""Cell-grid screen buffer with diff-based flushing.

Every frame is composed into a full grid of cells (character + style id),
diffed against the previously flushed grid, and only the changed runs are
written out. Nothing is ever written at a stale absolute position by a
background thread: the grid is the single source of truth for the screen.

Styles are interned SGR parameter tuples; the renderer emits a style's
parameters when the run's style changes and a reset when it ends.
"""

from __future__ import annotations

import re

from core.term import _WidthScanner

# A wide (CJK/fullwidth) character occupies two cells; the second cell
# stores this marker so the diff never splits a wide glyph.
_CONT = "\x00"

_ANSI_RE = re.compile(r"\033\[([0-9;?]*)([ -/]*[@-~])|\033\][^\x07\x1b]*(?:\x07|\x1b\\)?")
_SGR_RE = re.compile(r"\033\[([0-9;]*)m")

# Control bytes must never reach the terminal through a cell: a bare ESC
# or C1 byte would be written raw by the renderer. Tab (\t) and newline
# (\n) are skipped so they can never enter a cell.
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")


class StyleTable:
    """Interns SGR parameter tuples into small integer ids."""

    def __init__(self) -> None:
        self._ids: dict[tuple[str, ...], int] = {(): 0}
        self._params: list[tuple[str, ...]] = [()]

    def id_for(self, params: tuple[str, ...]) -> int:
        got = self._ids.get(params)
        if got is None:
            got = len(self._params)
            self._ids[params] = got
            self._params.append(params)
        return got

    def params_for(self, style_id: int) -> tuple[str, ...]:
        return self._params[style_id] if 0 <= style_id < len(self._params) else ()


DEFAULT_STYLE = 0


def parse_ansi_spans(text: str, table: StyleTable) -> list[tuple[str, int]]:
    """Split styled text into ``(plain_text, style_id)`` spans.

    SGR parameters accumulate across the string the way a terminal
    interprets them: a bold prefix styles everything after it until a
    reset. Non-SGR escapes (cursor movement, OSC) are dropped: transcript
    content may never move the cursor.
    """
    spans: list[tuple[str, int]] = []
    current: list[str] = []
    plain: list[str] = []

    def flush() -> None:
        if plain:
            spans.append(("".join(plain), table.id_for(tuple(current))))
            plain.clear()

    i = 0
    n = len(text)
    while i < n:
        m = _ANSI_RE.match(text, i)
        if not m:
            plain.append(text[i])
            i += 1
            continue
        # The escape terminates the span that precedes it; the new SGR
        # parameters apply to everything after it.
        flush()
        i = m.end()
        body, final = m.group(1) or "", m.group(2) or "m"
        if final != "m":
            continue
        parts = body.split(";") if body else [""]
        j = 0
        while j < len(parts):
            p = parts[j]
            if p == "0" or p == "":
                current = []
            elif p == "38" or p == "48":
                # Extended color: 38;5;N or 38;2;R;G;B — keep whole.
                take = ["38" if p == "38" else "48"]
                if j + 1 < len(parts) and parts[j + 1] == "5":
                    take += parts[j + 1 : j + 3]
                    j += 2
                elif j + 1 < len(parts) and parts[j + 1] == "2":
                    take += parts[j + 1 : j + 5]
                    j += 4
                current.extend(take)
            elif p.isdigit() or (p.startswith("-") and p[1:].isdigit()):
                current.append(p)
            j += 1
    flush()
    return spans


class Buffer:
    """A screen-sized grid of (character, style) cells."""

    __slots__ = ("cols", "rows", "chars", "styles", "styles_table")

    def __init__(self, cols: int, rows: int, styles_table: StyleTable) -> None:
        self.cols = max(1, cols)
        self.rows = max(1, rows)
        self.styles_table = styles_table
        self.chars: list[str] = [" "] * (self.cols * self.rows)
        self.styles: list[int] = [DEFAULT_STYLE] * (self.cols * self.rows)

    def resize(self, cols: int, rows: int) -> None:
        self.cols = max(1, cols)
        self.rows = max(1, rows)
        self.chars = [" "] * (self.cols * self.rows)
        self.styles = [DEFAULT_STYLE] * (self.cols * self.rows)

    def reset(self) -> None:
        for i in range(len(self.chars)):
            self.chars[i] = " "
            self.styles[i] = DEFAULT_STYLE

    def _index(self, x: int, y: int) -> int:
        return y * self.cols + x

    def set_cell(self, x: int, y: int, ch: str, style: int = DEFAULT_STYLE) -> None:
        if 0 <= x < self.cols and 0 <= y < self.rows:
            i = self._index(x, y)
            self.chars[i] = ch
            self.styles[i] = style

    def set_style(self, x: int, y: int, width: int, style: int) -> None:
        for cx in range(x, min(x + width, self.cols)):
            if 0 <= y < self.rows:
                i = self._index(cx, y)
                self.styles[i] = style

    def fill_style(self, x: int, y: int, width: int, height: int, style: int) -> None:
        for cy in range(y, min(y + height, self.rows)):
            self.set_style(x, cy, width, style)

    def set_str(self, x: int, y: int, text: str, style: int = DEFAULT_STYLE, max_width: int | None = None) -> int:
        """Write text at (x, y); returns the ending column (clipped)."""
        if not 0 <= y < self.rows:
            return x
        limit = self.cols if max_width is None else min(self.cols, x + max_width)
        cx = x
        # Per-scan ZWJ state so a ZWJ sequence inside this text is counted
        # as one grapheme without leaking into other set_str calls.
        scanner = _WidthScanner()
        for ch in text:
            if cx >= limit:
                break
            if _CTRL_RE.match(ch):
                continue  # control bytes must never reach the terminal
            w = scanner.feed(ch)
            if w == 0:
                continue  # combining marks: skip (rare in our content)
            if w >= 2:
                if cx + 1 >= limit:
                    break
                self.set_cell(cx, y, ch, style)
                self.set_cell(cx + 1, y, _CONT, style)
                cx += 2
            else:
                self.set_cell(cx, y, ch, style)
                cx += 1
        return cx

    def set_spans(self, x: int, y: int, spans: list[tuple[str, int]], max_width: int | None = None) -> int:
        cx = x
        for text, style in spans:
            cx = self.set_str(cx, y, text, style, max_width)
            if max_width is not None and cx >= max_width:
                break
        return cx

    def set_styled_line(self, x: int, y: int, styled_text: str, styles_table: StyleTable, max_width: int | None = None) -> int:
        return self.set_spans(x, y, parse_ansi_spans(styled_text, styles_table), max_width)


class Renderer:
    """Double buffer: frames are composed into ``buffer`` then diffed."""

    def __init__(self, backend, cols: int, rows: int) -> None:
        self.backend = backend
        self.styles = StyleTable()
        self.buffer = Buffer(cols, rows, self.styles)
        self._front_chars: list[str] = []
        self._front_styles: list[int] = []
        self._cursor: tuple[int, int] | None = None

    def resize(self, cols: int, rows: int) -> None:
        self.buffer.resize(cols, rows)
        self._front_chars = []
        self._front_styles = []

    def set_cursor(self, x: int, y: int) -> None:
        self._cursor = (x, y)

    def force_full_repaint(self) -> None:
        self._front_chars = []
        self._front_styles = []

    def flush(self) -> None:
        """Emit the difference between the composed and shown grids."""
        buf = self.buffer
        out: list[str] = []
        if len(self._front_chars) != len(buf.chars):
            out.append("\033[2J")
            self._front_chars = ["\x01"] * len(buf.chars)  # force diff
            self._front_styles = [-1] * len(buf.styles)
        cols = buf.cols
        for row in range(buf.rows):
            base = row * cols
            col = 0
            while col < cols:
                i = base + col
                ch, st = buf.chars[i], buf.styles[i]
                if ch == _CONT:
                    col += 1
                    continue
                if ch == self._front_chars[i] and st == self._front_styles[i]:
                    col += 1
                    continue
                # Extend the run while cells keep changing.
                run_start = col
                seg: list[str] = []
                cur_style = st
                while col < cols:
                    j = base + col
                    c2, s2 = buf.chars[j], buf.styles[j]
                    if c2 == self._front_chars[j] and s2 == self._front_styles[j]:
                        break
                    if c2 == _CONT:
                        col += 1
                        continue
                    if s2 != cur_style:
                        break
                    seg.append(c2)
                    col += 1
                params = self.styles.params_for(cur_style)
                out.append(f"\033[{row + 1};{run_start + 1}H")
                if params:
                    out.append("\033[" + ";".join(params) + "m")
                else:
                    out.append("\033[0m")
                out.append("".join(seg))
                if params:
                    out.append("\033[0m")
                # Record what we emitted (including the styled run).
                for k in range(run_start, col):
                    j = base + k
                    self._front_chars[j] = buf.chars[j]
                    self._front_styles[j] = buf.styles[j]
        if self._cursor is not None:
            cx, cy = self._cursor
            out.append(f"\033[{cy + 1};{cx + 1}H")
        else:
            out.append("\033[?25l")
        self.backend.write("".join(out))
