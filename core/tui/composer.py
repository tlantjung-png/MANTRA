"""Composer: the bottom prompt box as a widget.

Owns the editing state (buffer, caret, completion popup) and renders
itself into the frame's bottom rows. It never touches the terminal — the
frame renderer owns the screen. The completion popup is a window that
follows the selection; the overflow rows page by whole windows (arrowing
past the edge or clicking them).
"""

from __future__ import annotations

from dataclasses import dataclass

from core.term import visible_len, _char_width

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
            if self.is_multiline:
                self.cursor = self._vertical_caret(-1 if key == "up" else 1)
            return
        if key == "left":
            self.cursor = max(0, self.cursor - 1)
            self._recompute_soft()
            return
        if key == "right":
            self.cursor = min(len(self.buffer), self.cursor + 1)
            self._recompute_soft()
            return
        if key == "home" or key == "ctrl+a":
            self.cursor = 0
            self._recompute_soft()
            return
        if key == "end" or key == "ctrl+e":
            self.cursor = len(self.buffer)
            self._recompute_soft()
            return
        if key == "backspace":
            if self.cursor > 0:
                self.buffer = self.buffer[: self.cursor - 1] + self.buffer[self.cursor :]
                self.cursor -= 1
                self._last_token = None
                self._dismissed = False
                self._recompute()
            return
        if key == "delete":
            self.buffer = self.buffer[: self.cursor] + self.buffer[self.cursor + 1 :]
            self._dismissed = False
            self._recompute()
            return
        if key == "ctrl+left":
            self.cursor = self._prev_word()
            self._recompute_soft()
            return
        if key == "ctrl+right":
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
        for ch in text:
            w = max(1, _char_width(ch))
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
        for ch in text:
            w = max(1, _char_width(ch))
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
        if visible_len(self.buffer) <= avail:
            return label_vis + col
        # Windowed: compute the visible offset the same way _window does.
        keep = max(0, col - avail // 2)
        return label_vis + max(0, col - keep)
