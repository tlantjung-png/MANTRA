"""Application-side text selection over the transcript.

The selection is drawn by the application: a mouse press sets a pending
anchor, motion past a small threshold turns it into a drag, and the
highlighted cells are painted with an overlay style at render time.
Copying extracts the plain text of the selected display rows from the
transcript.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from core.term import _WidthScanner, ansi_strip as strip_ansi


DRAG_THRESHOLD = 2  # cells of motion before a press becomes a drag

_ANSI_RE = re.compile(r"\033\[[0-9;?]*[ -/]*[@-~]|\033][^\x07\x1b]*(?:\x07|\x1b\\)?")


def col_to_offset(text: str, col: int) -> int:
    """Map a cell column to a character offset in styled ``text``.

    Wide (CJK) characters occupy two cells, so the offset is not the
    column as soon as a row contains any; ANSI escapes carry no width.
    A column inside a wide character's second cell maps back to that
    character, so a selection starting mid-glyph still copies it.
    """
    offset = 0
    cells = 0
    i = 0
    n = len(text)
    # Per-scan ZWJ state: a continuation char after a ZWJ occupies no new
    # cells, so width math must track it within this one string only.
    scanner = _WidthScanner()
    while i < n and cells < col:
        m = _ANSI_RE.match(text, i)
        if m:
            i = m.end()
            continue
        w = max(1, scanner.feed(text[i]))
        if cells + w > col:
            break  # col falls inside this character: keep its offset
        cells += w
        offset += 1
        i += 1
    return offset


@dataclass
class Anchor:
    row: int        # display-row index in the transcript
    col: int        # cell column within the row


class Selection:
    def __init__(self) -> None:
        self.anchor: Anchor | None = None
        self.head: Anchor | None = None
        self.active = False
        self._press_cell: tuple[int, int] | None = None

    # ── gestures ──────────────────────────────────────────────

    def begin_press(self, x: int, y: int) -> None:
        """A press inside the transcript area: remember it, don't commit."""
        self._press_cell = (x, y)
        self.active = False
        self.anchor = None
        self.head = None

    def begin_drag(self, x: int, y: int, row_at: Callable) -> None:
        """Motion with the button held: become a drag past the threshold."""
        if self._press_cell is None:
            return
        px, py = self._press_cell
        if not self.active:
            if abs(x - px) + abs(y - py) < DRAG_THRESHOLD:
                return
            src = row_at(py)
            if src is None:
                return
            self.anchor = Anchor(src[0], px)
            self.active = True
        src = row_at(y)
        if src is None:
            return
        self.head = Anchor(src[0], x)

    def end_press(
        self, x: int, y: int, row_at: Callable, text_at: Callable
    ) -> tuple[str, bool] | None:
        """Release: return (text, was_drag) when there is something to copy.

        ``row_at`` maps a screen row to (display index, text); ``text_at``
        maps a display index to its styled text.
        """
        press = self._press_cell
        self._press_cell = None
        if self.active and self.anchor is not None and self.head is not None:
            start, end = self._ordered()
            text = self._extract(start, end, text_at)
            self._keep_persistent()
            return (text, True) if text else None
        # A clean click without drag: clear any persistent selection.
        if press is not None:
            self.clear()
        return None

    def _keep_persistent(self) -> None:
        pass  # the highlight naturally persists until the next press

    def clear(self) -> None:
        self.anchor = None
        self.head = None
        self.active = False
        self._press_cell = None

    # ── geometry ──────────────────────────────────────────────

    def _ordered(self) -> tuple[Anchor, Anchor]:
        a, b = self.anchor, self.head
        if (b.row, b.col) < (a.row, a.col):
            return b, a
        return a, b

    def _extract(self, start: Anchor, end: Anchor, text_at: Callable) -> str:
        lines: list[str] = []
        for row in range(start.row, end.row + 1):
            styled = text_at(row) or ""
            text = strip_ansi(styled)
            if row == start.row and row == end.row:
                s = col_to_offset(styled, start.col)
                e = col_to_offset(styled, end.col + 1)
                text = text[s:e]
            elif row == start.row:
                text = text[col_to_offset(styled, start.col) :]
            elif row == end.row:
                text = text[: col_to_offset(styled, end.col + 1)]
            lines.append(text)
        return "\n".join(lines).rstrip()

    # ── painting ──────────────────────────────────────────────

    def overlay_rows(self, first_display_row: int, height: int) -> list[tuple[int, int, int]]:
        """(row_offset, start_col, end_col) spans to highlight.

        ``row_offset`` is 0-based within the visible window; returns an
        empty list when there is no active selection on screen.
        """
        if not (self.active and self.anchor is not None and self.head is not None):
            return []
        start, end = self._ordered()
        spans: list[tuple[int, int, int]] = []
        for offset in range(height):
            row = first_display_row + offset
            if row < start.row or row > end.row:
                continue
            if row == start.row == end.row:
                spans.append((offset, start.col, end.col + 1))
            elif row == start.row:
                spans.append((offset, start.col, 10**6))
            elif row == end.row:
                spans.append((offset, 0, end.col + 1))
            else:
                spans.append((offset, 0, 10**6))
        return spans


# Kept at module level to avoid importing typing just for one alias.
from typing import Callable  # noqa: E402
