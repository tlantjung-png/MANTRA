"""Overlays: floating UI drawn above the frame — dropdowns, menus,
question cards, text-input prompts.

An overlay consumes key events and reports a result when it finishes.
Rendering goes straight into the frame buffer; nothing writes to the
terminal directly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from core.term import visible_len

from core.tui.buffer import Buffer

# Style presets as SGR parameter tuples (kept small on purpose).
S_TITLE = ("1;38;5;253",)
S_DIM = ("2",)
S_SELECT = ("1;38;5;131",)
S_ACCENT = ("38;5;131",)
S_ERROR = ("38;5;167",)
S_BOX = ("38;5;238",)


@dataclass
class Option:
    value: str
    label: str = ""
    hint: str = ""

    def __post_init__(self) -> None:
        if not self.label:
            self.label = self.value


class MenuOverlay:
    """A filterable, scrollable option list in a bordered box."""

    def __init__(
        self,
        title: str,
        options: list[Any],
        *,
        allow_filter: bool = True,
        width: int | None = None,
        max_rows: int = 12,
        allow_delete: bool = False,
        on_delete: Any = None,
    ) -> None:
        self.title = title
        self.options: list[Option] = [
            o if isinstance(o, Option) else Option(str(o)) for o in options
        ]
        self.allow_filter = allow_filter
        self.allow_delete = allow_delete
        self.on_delete = on_delete
        self.query = ""
        self.cursor = 0
        self._scroll = 0
        self.max_rows = max_rows
        self.width = width
        self.result: str | None = None
        self.cancelled = False

    # ── state ─────────────────────────────────────────────────

    @property
    def matches(self) -> list[Option]:
        if not self.allow_filter or not self.query:
            return self.options
        needle = self.query.lower()
        return [o for o in self.options if needle in f"{o.label} {o.hint}".lower()]

    @property
    def finished(self) -> bool:
        return self.cancelled or self.result is not None

    def consume_key(self, key: str, mods: frozenset = frozenset()) -> None:
        matches = self.matches
        if key == "esc" or key == "ctrl+c":
            self.cancelled = True
            return
        if self.allow_delete and key in ("d", "D") and not self.query:
            if matches and 0 <= self.cursor < len(matches):
                target = matches[self.cursor]
                if not target.value.startswith("+"):
                    if self.on_delete is not None:
                        try:
                            self.on_delete(target.value)
                        except Exception:
                            pass
                    self.options = [o for o in self.options if o.value != target.value]
                    if self.cursor >= len(self.matches):
                        self.cursor = max(0, len(self.matches) - 1)
                    if not self.options or all(
                        o.value.startswith("+") for o in self.options
                    ):
                        self.cancelled = True
            return
        if key == "enter":
            if matches and 0 <= self.cursor < len(matches):
                self.result = matches[self.cursor].value
            return
        if key == "up":
            self.cursor = max(0, self.cursor - 1)
            return
        if key == "down":
            if matches:
                self.cursor = min(len(matches) - 1, self.cursor + 1)
            return
        if key == "pageup":
            if matches:
                self.cursor = max(0, self.cursor - self.max_rows)
            return
        if key == "pagedown":
            if matches:
                self.cursor = min(len(matches) - 1, self.cursor + self.max_rows)
            return
        if key == "backspace":
            self.query = self.query[:-1]
            self.cursor = 0
            return
        if len(key) == 1 and self.allow_filter:
            self.query += key
            self.cursor = 0

    # ── rendering ─────────────────────────────────────────────

    def preferred_size(self, cols: int, rows: int) -> tuple[int, int, int, int]:
        width = min(cols - 4, max(40, self.width or 0) or self._auto_width())
        height = min(rows - 4, self.max_rows + 4)
        x = (cols - width) // 2
        y = (rows - height) // 2
        return x, y, width, height

    def _auto_width(self) -> int:
        widest = len(self.title) + 4
        for o in self.matches[:24]:
            widest = max(widest, visible_len(o.label) + visible_len(o.hint) + 8)
        return min(100, widest + 4)

    def render(self, buf: Buffer, cols: int, rows: int) -> None:
        x, y, width, height = self.preferred_size(cols, rows)
        self._last_rect = (x, y, width, height)
        matches = self.matches
        box_style = buf.styles_table.id_for(S_BOX)
        title_style = buf.styles_table.id_for(S_TITLE)
        dim_style = buf.styles_table.id_for(S_DIM)
        sel_style = buf.styles_table.id_for(S_SELECT)
        # Frame
        top = "╭─ " + (self.title + ("  filter: " + self.query if self.query else "")) + " "
        top += "─" * max(0, width - visible_len(top) - 1) + "╮"
        buf.set_str(x, y, top, box_style)
        for cy in range(y + 1, y + height - 1):
            buf.set_str(x, cy, "│", box_style)
            buf.set_str(x + width - 1, cy, "│", box_style)
        bottom = "╰" + "─" * (width - 2) + "╯"
        buf.set_str(x, y + height - 1, bottom, box_style)
        # Rows
        visible = height - 3
        self._scroll = max(0, min(self.cursor - visible // 2, max(0, len(matches) - visible)))
        window = matches[self._scroll : self._scroll + visible]
        for i, opt in enumerate(window):
            idx = self._scroll + i
            marker = "› " if idx == self.cursor else "  "
            text = f" {marker}{opt.label}"
            if opt.hint:
                text += f"  {opt.hint}"
            style = sel_style if idx == self.cursor else dim_style
            buf.set_str(x + 1, y + 1 + i, text[: width - 2], style)
        hidden = len(matches) - self._scroll - len(window)
        if hidden > 0:
            buf.set_str(x + 2, y + height - 2, f"… {hidden} more", dim_style)


class QuestionCard:
    """A blocking prompt: the answer is typed at the card, not a line."""

    def __init__(self, title: str, body: str, choices: str = "yna") -> None:
        self.title = title
        self.body = body
        self.choices = choices  # subset of "yna" = yes/no/always
        self.answer: str | None = None

    @property
    def finished(self) -> bool:
        return self.answer is not None

    def consume_key(self, key: str, mods: frozenset = frozenset()) -> None:
        if key == "esc" or key == "ctrl+c":
            self.answer = "n"
            return
        if key in ("y", "Y") and "y" in self.choices:
            self.answer = "y"
        elif key in ("n", "N") and "n" in self.choices:
            self.answer = "n"
        elif key in ("a", "A") and "a" in self.choices:
            self.answer = "a"

    def render(self, buf: Buffer, cols: int, rows: int) -> None:
        body_lines = self.body.split("\n")
        width = min(cols - 4, max(50, max(visible_len(ln) for ln in [self.title] + body_lines) + 10))
        height = min(max(4, rows - 4), len(body_lines) + 4)
        x = (cols - width) // 2
        y = max(1, (rows - height) // 2 - 2)
        box_style = buf.styles_table.id_for(S_BOX)
        title_style = buf.styles_table.id_for(S_SELECT)
        dim_style = buf.styles_table.id_for(S_DIM)
        top = "╭─ " + self.title + " " + "─" * max(0, width - visible_len(self.title) - 6) + "╮"
        buf.set_str(x, y, top, title_style)
        max_body = height - 3
        shown = body_lines[:max_body]
        for i, line in enumerate(shown):
            buf.set_str(x + 2, y + 1 + i, line[: width - 4], dim_style)
        if len(body_lines) > max_body:
            buf.set_str(x + 2, y + 1 + max_body - 1, f"… {len(body_lines) - max_body} more lines", dim_style)
        hints = {
            "yna": "[y]es   [n]o   [a]lways for this session",
            "yn": "[y]es   [n]o",
        }.get(self.choices, "[y]es   [n]o")
        buf.set_str(x + 2, y + height - 2, hints, dim_style)
        for cy in range(y + 1, y + height - 1):
            buf.set_str(x + width - 1, cy, "│", box_style)
        buf.set_str(x, y + height - 1, "╰" + "─" * (width - 2) + "╯", box_style)


class LinePrompt:
    """A single-line text input in a card (used for keys and confirmations)."""

    def __init__(self, label: str, secret: bool = False, default: str = "") -> None:
        self.label = label
        self.secret = secret
        self.buffer = default
        self.cursor = len(default)
        self.result: str | None = None
        self.cancelled = False

    @property
    def finished(self) -> bool:
        return self.result is not None or self.cancelled

    def consume_key(self, key: str, mods: frozenset = frozenset()) -> None:
        if key == "esc" or key == "ctrl+c":
            self.cancelled = True
            return
        if key == "enter":
            self.result = self.buffer
            return
        if key == "backspace":
            if self.cursor > 0:
                self.buffer = self.buffer[: self.cursor - 1] + self.buffer[self.cursor :]
                self.cursor -= 1
            return
        if key == "delete":
            self.buffer = self.buffer[: self.cursor] + self.buffer[self.cursor + 1 :]
            return
        if key == "left":
            self.cursor = max(0, self.cursor - 1)
            return
        if key == "right":
            self.cursor = min(len(self.buffer), self.cursor + 1)
            return
        if key == "home":
            self.cursor = 0
            return
        if key == "end":
            self.cursor = len(self.buffer)
            return
        if len(key) == 1:
            self.buffer = self.buffer[: self.cursor] + key + self.buffer[self.cursor :]
            self.cursor += 1

    def insert(self, text: str) -> None:
        """Insert ``text`` at the caret as one edit (e.g. a paste)."""
        self.buffer = self.buffer[: self.cursor] + text + self.buffer[self.cursor :]
        self.cursor += len(text)

    def render(self, buf: Buffer, cols: int, rows: int) -> None:
        width = min(cols - 4, max(56, visible_len(self.label) + 20))
        height = 4
        x = (cols - width) // 2
        y = max(1, (rows - height) // 2 - 2)
        box_style = buf.styles_table.id_for(S_BOX)
        title_style = buf.styles_table.id_for(S_SELECT)
        dim_style = buf.styles_table.id_for(S_DIM)
        top = "╭─ " + self.label + " " + "─" * max(0, width - visible_len(self.label) - 6) + "╮"
        buf.set_str(x, y, top, title_style)
        shown = ("*" * len(self.buffer)) if self.secret else self.buffer
        # Keep the caret visible: window the text around the cursor.
        keep = max(0, self.cursor - (width - 6))
        shown_window = shown[keep : keep + width - 6]
        buf.set_str(x + 2, y + 1, shown_window[: width - 4], dim_style)
        buf.set_str(x + 2, y + 2, "type your answer · enter confirms · esc cancels", dim_style)
        for cy in range(y + 1, y + height - 1):
            buf.set_str(x + width - 1, cy, "│", box_style)
        buf.set_str(x, y + height - 1, "╰" + "─" * (width - 2) + "╯", box_style)
        caret_col = x + 2 + max(0, self.cursor - keep)
        buf.set_style(caret_col, y + 1, 1, buf.styles_table.id_for(("7",)))


def render_completion(
    buf: Buffer,
    items: list[str],
    labels: list[str] | None,
    selected: int,
    offset: int,
    max_rows: int,
    anchor_row: int,
    cols: int,
) -> tuple[int, int, int]:
    """Draw the completion dropdown above ``anchor_row``.

    Returns (x, y, width, height) of the painted popup so clicks can be
    hit-tested; returns height 0 when there was nothing to draw.
    """
    if not items:
        return 0, 0, 0, 0
    window = items[offset : offset + max_rows]
    rows: list[tuple[int, str]] = []  # (absolute item index, styled text)
    if offset > 0:
        rows.append((-1, f"  … {offset} above"))
    for i, _ in enumerate(window):
        idx = offset + i
        label = labels[idx] if labels and 0 <= idx < len(labels) else items[idx]
        marker = "> " if idx == selected else "  "
        text = f"  {marker}{label}"
        rows.append((idx, text))
    remaining = len(items) - (offset + len(window))
    if remaining > 0:
        rows.append((-2, f"  … {remaining} more (click or keep pressing down)"))
    height = len(rows)
    width = min(cols - 2, max(40, max(visible_len(t) for _, t in rows) + 4))
    x = max(0, (cols - width) // 2)
    y = max(0, anchor_row - height)
    dim = buf.styles_table.id_for(S_DIM)
    sel = buf.styles_table.id_for(S_SELECT)
    box = buf.styles_table.id_for(S_BOX)
    buf.set_str(x, y, "╭" + "─" * (width - 2) + "╮", box)
    for i, (idx, text) in enumerate(rows):
        buf.set_str(x, y + 1 + i, "│", box)
        style = sel if idx == selected else dim
        buf.set_str(x + 1, y + 1 + i, text[: width - 2], style)
        buf.set_str(x + width - 1, y + 1 + i, "│", box)
    buf.set_str(x, y + height + 1, "╰" + "─" * (width - 2) + "╯", box)
    return x, y, width, height + 2
