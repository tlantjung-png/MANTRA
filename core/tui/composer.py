"""Composer: the bottom prompt box as a widget.

Owns the editing state (buffer, caret, completion popup) and renders
itself into the frame's bottom rows. It never touches the terminal — the
frame renderer owns the screen. The completion popup is a window that
follows the selection; the overflow rows page by whole windows (arrowing
past the edge or clicking them).
"""

from __future__ import annotations

from dataclasses import dataclass

from core.term import visible_len, _WidthScanner

from core.tui.buffer import Buffer
from core.tui.clipboard import paste_text


@dataclass
class Completion:
    start: int
    end: int
    items: list[str]
    labels: list[str] | None = None

    def label(self, index: int) -> str:
        if self.labels and 0 <= index < len(self.labels):
            return self.labels[index]
        return self.items[index] if 0 <= index < len(self.items) else ""


class Composer:
    def __init__(self, label: str = "MANTRA >", max_popup: int = 10) -> None:
        self.label = label
        self.max_popup = max_popup
        self.buffer = ""
        self.cursor = 0
        self.completion: Completion | None = None
        self.selected = 0
        self._popup_off = 0
        self._last_token: str | None = None
        self._dismissed = False
        # Set by the app: callable(buffer, cursor) -> Completion | None.
        self.completer = None
        # Submitted text (Enter with no popup). Consumed by the app.
        self.submitted: str | None = None
        # Mouse selection as buffer offsets. Typing or paste replaces the
        # selected range; caret moves clear it.
        self.sel_anchor: int | None = None
        self.sel_head: int | None = None

    # ── state ─────────────────────────────────────────────────

    def clear(self) -> None:
        self.buffer = ""
        self.cursor = 0
        self.completion = None
        self.selected = 0
        self._popup_off = 0
        self._last_token = None
        self._dismissed = False
        self.submitted = None
        self.sel_anchor = None
        self.sel_head = None

    # ── selection ─────────────────────────────────────────────

    def has_selection(self) -> bool:
        return (
            self.sel_anchor is not None
            and self.sel_head is not None
            and self.sel_anchor != self.sel_head
        )

    def selected_range(self) -> tuple[int, int] | None:
        if not self.has_selection():
            return None
        assert self.sel_anchor is not None and self.sel_head is not None
        return (self.sel_anchor, self.sel_head) if self.sel_anchor < self.sel_head else (self.sel_head, self.sel_anchor)

    def selected_text(self) -> str:
        span = self.selected_range()
        return "" if span is None else self.buffer[span[0] : span[1]]

    def clear_selection(self) -> None:
        self.sel_anchor = None
        self.sel_head = None

    def _drop_selection(self) -> bool:
        """Delete the selected range, parking the caret at its start."""
        span = self.selected_range()
        if span is None:
            return False
        self.buffer = self.buffer[: span[0]] + self.buffer[span[1] :]
        self.cursor = span[0]
        self.clear_selection()
        self._last_token = None
        self._dismissed = False
        self._recompute()
        return True

    @property
    def is_multiline(self) -> bool:
        return "\n" in self.buffer

    @property
    def popup_open(self) -> bool:
        return bool(self.completion and self.completion.items and not self._dismissed)

    # ── completion ────────────────────────────────────────────

    def _recompute(self) -> None:
        if self.completer is None or self._dismissed:
            self.completion = None
            return
        completion = self.completer.complete(self.buffer, self.cursor)
        if completion is None or not completion.items:
            self.completion = None
            self._popup_off = 0
            return
        token = self.buffer[completion.start : completion.end]
        if token != self._last_token:
            self.selected = 0
            self._popup_off = 0
        self._last_token = token
        self.completion = completion
        self.selected = min(self.selected, len(completion.items) - 1)

    # ── input ─────────────────────────────────────────────────

    def consume_key(self, key: str, mods: frozenset = frozenset()) -> None:
        """Apply one key event. Submit text lands in ``self.submitted``."""
        if key == "enter":
            if self.popup_open:
                self._accept_popup()
                return
            self.clear_selection()
            if self.buffer.strip():
                text = self.buffer
                self.clear()
                self.submitted = text
            return
        if key == "newline":
            self._insert("\n")
            return
        if key == "esc":
            if self.popup_open:
                self._dismissed = True
                self.completion = None
            return
        if key == "tab":
            self._dismissed = False
            self._recompute()
            if self.popup_open and self.completion:
                # Tab accepts the highlighted item outright.
                self._accept_popup()
            return
        if key == "shift+tab":
            if self.popup_open:
                self.selected = max(0, self.selected - 1)
            return
        if key in ("up", "down"):
            if self.popup_open:
                step = -1 if key == "up" else 1
                self.selected = max(0, min(len(self.completion.items) - 1, self.selected + step))
                return
            self.clear_selection()
            if self.is_multiline:
                self.cursor = self._vertical_caret(-1 if key == "up" else 1)
            return
        if key == "left":
            self.clear_selection()
            self.cursor = max(0, self.cursor - 1)
            self._recompute_soft()
            return
        if key == "right":
            self.clear_selection()
            self.cursor = min(len(self.buffer), self.cursor + 1)
            self._recompute_soft()
            return
        if key == "home" or key == "ctrl+a":
            self.clear_selection()
            self.cursor = 0
            self._recompute_soft()
            return
        if key == "end" or key == "ctrl+e":
            self.clear_selection()
            self.cursor = len(self.buffer)
            self._recompute_soft()
            return
        if key == "backspace":
            if self._drop_selection():
                return
            if self.cursor > 0:
                self.buffer = self.buffer[: self.cursor - 1] + self.buffer[self.cursor :]
                self.cursor -= 1
                self._last_token = None
                self._dismissed = False
                self._recompute()
            return
        if key == "delete":
            if self._drop_selection():
                return
            self.buffer = self.buffer[: self.cursor] + self.buffer[self.cursor + 1 :]
            self._dismissed = False
            self._recompute()
            return
        if key == "ctrl+left":
            self.clear_selection()
            self.cursor = self._prev_word()
            self._recompute_soft()
            return
        if key == "ctrl+right":
            self.clear_selection()
            self.cursor = self._next_word()
            self._recompute_soft()
            return
        if key == "ctrl+v":
            clip = paste_text()
            if clip:
                self.consume_paste(clip)
            return
        if key == "ctrl+u":
            self.buffer = self.buffer[self.cursor :]
            self.cursor = 0
            self._recompute()
            return
        if key == "ctrl+k":
            self.buffer = self.buffer[: self.cursor]
            self._recompute()
            return
        if key == "ctrl+w":
            pos = self._prev_word()
            self.buffer = self.buffer[:pos] + self.buffer[self.cursor :]
            self.cursor = pos
            self._recompute()
            return
        if len(key) == 1:
            self._insert(key)
            return
        # Unhandled keys (ctrl+o etc.) are the app's business.

    def consume_paste(self, text: str) -> None:
        self._insert(text.replace("\r\n", "\n").replace("\r", "\n"))

    def set_text(self, text: str) -> None:
        """Replace the buffer with *text* (prompt history recall)."""
        self.clear()
        self._insert(text)

    def _insert(self, text: str) -> None:
        if self.has_selection():
            self._drop_selection()
        self.buffer = self.buffer[: self.cursor] + text + self.buffer[self.cursor :]
        self.cursor += len(text)
        self._last_token = None
        self._dismissed = False
        self._recompute()

    def _accept_popup(self) -> None:
        if not (self.completion and self.completion.items):
            return
        chosen = self.completion.items[min(self.selected, len(self.completion.items) - 1)]
        start, end = self.completion.start, self.completion.end
        self.buffer = self.buffer[:start] + chosen + self.buffer[end:]
        self.cursor = start + len(chosen)
        self._dismissed = True
        self.completion = None
        self._last_token = None

    def popup_click(self, row: int) -> bool:
        """A mouse click at screen ``row``; handled when it hits the popup.

        Returns True when the click was consumed (including paging and
        dismissal), so the app does not also treat it as a transcript
        selection.
        """
        return False  # replaced by the overlay's own hit map in the app

    def _recompute_soft(self) -> None:
        if self.popup_open:
            self._recompute()

    def _prev_word(self) -> int:
        i = self.cursor
        while i > 0 and self.buffer[i - 1].isspace():
            i -= 1
        while i > 0 and not self.buffer[i - 1].isspace():
            i -= 1
        return i

    def _next_word(self) -> int:
        n = len(self.buffer)
        i = self.cursor
        while i < n and not self.buffer[i].isspace():
            i += 1
        while i < n and self.buffer[i].isspace():
            i += 1
        return i

    # ── mouse geometry ────────────────────────────────────────
    # Maps screen cells back onto buffer offsets with the same layout
    # math as rendering, so a press/drag in the prompt box positions
    # the caret and selects text for editing.

    def _label_vis(self) -> int:
        return visible_len(f"│ {self.label} ")

    @staticmethod
    def _window_keep(text: str, caret_col: int, width: int) -> int:
        """Horizontal scroll offset mirroring ``_window``."""
        if visible_len(text) <= width:
            return 0
        return max(0, caret_col - width // 2)

    def row_map(self, cols: int, rows_total: int, box_height: int) -> list[tuple[int, int, str, int]]:
        """One (screen_y, line_no, line_text, window_keep) per visible row."""
        label_vis = self._label_vis()
        avail = max(0, cols - label_vis - 1)
        height = box_height - 1
        if not self.is_multiline:
            keep = self._window_keep(self.buffer, self.cursor, avail)
            return [(rows_total - 2, 0, self.buffer, keep)]
        lines = self.buffer.split("\n")
        caret_line = min(len(lines) - 1, self.buffer[: self.cursor].count("\n"))
        max_lines = max(1, height - 1)
        first = max(0, caret_line + 1 - max_lines)
        y = rows_total - box_height
        top = y + height - (1 + (caret_line + 1 - first))
        caret_col = self.cursor - sum(len(lines[i]) + 1 for i in range(caret_line))
        keep = self._window_keep(lines[caret_line], caret_col, avail)
        return [
            (top + 1 + i, idx, lines[idx], keep)
            for i, idx in enumerate(range(first, caret_line + 1))
        ]

    @staticmethod
    def _col_to_offset(text: str, keep: int, col: int) -> int:
        """Char offset whose display cell contains column ``keep + col``."""
        target = keep + col
        display = 0
        scanner = _WidthScanner()
        for i, ch in enumerate(text):
            w = max(1, scanner.feed(ch))
            if display < keep:
                display += w
                continue
            if display + w > target:
                return i
            display += w
        return len(text)

    @staticmethod
    def _display_col(text: str, keep: int, offset: int) -> int:
        """Display column of char ``offset`` relative to the window start."""
        total = 0
        scanner = _WidthScanner()
        for ch in text[:offset]:
            total += max(1, scanner.feed(ch))
        return max(0, total - keep)

    def _line_start(self, line_no: int) -> int:
        return sum(len(line) + 1 for line in self.buffer.split("\n")[:line_no])

    def offset_at(self, screen_y: int, x: int, cols: int, rows_total: int, box_height: int) -> int | None:
        """Buffer offset under screen cell (``screen_y``, ``x``), if any."""
        label_vis = self._label_vis()
        for sy, line_no, text, keep in self.row_map(cols, rows_total, box_height):
            if sy != screen_y:
                continue
            col = x - label_vis
            if col <= 0:
                return self._line_start(line_no)
            off = self._col_to_offset(text, keep, col)
            return self._line_start(line_no) + min(off, len(text))
        return None

    def selection_spans(self, cols: int, rows_total: int, box_height: int) -> list[tuple[int, int, int]]:
        """(screen_y, start_col, end_col) highlight spans for the selection."""
        span = self.selected_range()
        if span is None:
            return []
        start, end = span
        label_vis = self._label_vis()
        avail = max(0, cols - label_vis - 1)
        out: list[tuple[int, int, int]] = []
        for sy, line_no, text, keep in self.row_map(cols, rows_total, box_height):
            line_start = self._line_start(line_no)
            line_end = line_start + len(text)
            s = max(start, line_start)
            e = min(end, line_end)
            if s < e:
                c0 = self._display_col(text, keep, s - line_start)
                c1 = self._display_col(text, keep, e - line_start)
                out.append((sy, label_vis + c0, label_vis + c1))
            elif s == e == line_end and end > line_end:
                # The selection covers this row's newline: extend the
                # highlight to the row edge so the wrap is visible.
                out.append((sy, label_vis + self._display_col(text, keep, len(text)), label_vis + avail))
        return out

    def _vertical_caret(self, direction: int) -> int:
        if not self.buffer:
            return self.cursor
        lines = self.buffer.split("\n")
        starts = []
        idx = 0
        for line in lines:
            starts.append(idx)
            idx += len(line) + 1
        cl = min(len(lines) - 1, self.buffer[: self.cursor].count("\n"))
        col = max(0, self.cursor - starts[cl])
        target = cl + direction
        if target < 0 or target >= len(lines):
            return self.cursor
        return starts[target] + min(col, len(lines[target]))

    # ── rendering ─────────────────────────────────────────────

    def render(self, buf: Buffer, y: int, height: int, cols: int, wall: str) -> int:
        """Draw the prompt box with its bottom row at ``y + height - 1``.

        Returns the caret column on the last row (for the hardware
        cursor). A multi-line buffer expands upward.
        """
        rows = self._compose_rows(cols, height, wall)
        top = y + height - len(rows)
        for i, row_text in enumerate(rows):
            buf.set_styled_line(0, top + i, row_text, buf.styles_table, cols)
        return self._caret_col(cols)

    def _compose_rows(self, cols: int, height: int, wall: str) -> list[str]:
        styled_label = f"\033[2m│ \033[0m\033[1m{self.label}\033[0m "
        label_vis = visible_len(f"│ {self.label} ")
        avail = max(0, cols - label_vis - 1)
        rows: list[str] = []
        if self.is_multiline:
            lines = self.buffer.split("\n")
            caret_line = min(len(lines) - 1, self.buffer[: self.cursor].count("\n"))
            max_lines = max(1, height - 1)
            first = max(0, caret_line + 1 - max_lines)
            vis = lines[first : caret_line + 1]
            chip = f"· {len(lines)} lines · {len(self.buffer):,} chars"
            if first > 0:
                chip = f"… {first} more above · " + chip
            # The chip sits on a ruled divider inside the box, not on a
            # second top edge: the prompt is one closed rectangle whose
            # top edge is the status row.
            side = max(0, (cols - visible_len(chip) - 6) // 2)
            rows.append(f"\033[2m│ {'─' * side} {chip} {'─' * side} │\033[0m")
            for i, text in enumerate(vis):
                if i == len(vis) - 1:
                    rows.append(self._single_row(styled_label, self._window(text, self._col_in_line(lines, caret_line, self.cursor), avail), avail, wall))
                else:
                    rows.append(self._single_row("\033[2m│\033[0m " + " " * (visible_len(self.label) + 1), self._clip(text, avail), avail, wall))
        else:
            shown = self._window(self.buffer, self.cursor, avail)
            rows.append(self._single_row(styled_label, shown, avail, wall))
        return rows

    def _single_row(self, left: str, content: str, avail: int, wall: str) -> str:
        pad = " " * max(0, avail - visible_len(content))
        return left + content + pad + "\033[2m│\033[0m"

    @staticmethod
    def _col_in_line(lines: list[str], caret_line: int, cursor: int) -> int:
        start = 0
        for i in range(caret_line):
            start += len(lines[i]) + 1
        return cursor - start

    @staticmethod
    def _clip(text: str, width: int) -> str:
        out = []
        used = 0
        scanner = _WidthScanner()
        for ch in text:
            w = max(1, scanner.feed(ch))
            if used + w > width:
                break
            out.append(ch)
            used += w
        return "".join(out)

    @staticmethod
    def _window(text: str, caret_col: int, width: int) -> str:
        """A horizontal window of ``text`` keeping the caret visible."""
        if visible_len(text) <= width:
            return text
        keep = max(0, caret_col - width // 2)
        result: list[str] = []
        used = 0
        col = 0
        scanner = _WidthScanner()
        for ch in text:
            w = max(1, scanner.feed(ch))
            if col >= keep and used + w <= width:
                result.append(ch)
                used += w
            col += w
            if used >= width:
                break
        return "".join(result)

    def _caret_col(self, cols: int) -> int:
        label_vis = visible_len(f"│ {self.label} ")
        avail = max(0, cols - label_vis - 1)
        if self.is_multiline:
            lines = self.buffer.split("\n")
            cl = min(len(lines) - 1, self.buffer[: self.cursor].count("\n"))
            start = sum(len(lines[i]) + 1 for i in range(cl))
            col = self.cursor - start
        else:
            col = self.cursor
        # The caret lives on the rendered line, so it moves in display
        # columns: a wide character before it pushes the caret two cells,
        # not one. Counting string indexes put the caret inside the wide
        # character it sits after.
        line = lines[cl] if self.is_multiline else self.buffer
        col_in_line = min(col, len(line))
        display = visible_len(line[:col_in_line])
        if visible_len(self.buffer) <= avail:
            return label_vis + display
        # Windowed: mirror _window's keep calculation in display columns.
        keep = max(0, display - avail // 2)
        return label_vis + max(0, display - keep)
