"""Overlays: floating UI drawn above the frame — dropdowns, menus,
question cards, text-input prompts.

An overlay consumes key events and reports a result when it finishes.
Rendering goes straight into the frame buffer; nothing writes to the
terminal directly.
"""

from __future__ import annotations

from dataclasses import dataclass
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


def _blank_box(buf: Buffer, x: int, y: int, w: int, h: int, style: int) -> None:
    """An opaque card: blank interior plus the round border frame.

    Overlays float over live conversation, so every cell they own must
    be painted — otherwise the page behind shows through the gaps.
    """
    if w < 2 or h < 2:
        return
    blank = " " * (w - 2)
    for cy in range(y + 1, y + h - 1):
        buf.set_str(x + 1, cy, blank, style)
    buf.set_str(x, y, "╭" + "─" * (w - 2) + "╮", style)
    buf.set_str(x, y + h - 1, "╰" + "─" * (w - 2) + "╯", style)


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
        self._last_rect: tuple[int, int, int, int] | None = None

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

    @property
    def rect(self) -> tuple[int, int, int, int] | None:
        """The box geometry from the last render (None before drawing)."""
        return self._last_rect

    def consume_wheel(self, direction: int) -> None:
        """A wheel notch over the box walks the highlight like the arrows."""
        matches = self.matches
        if direction < 0:
            self.cursor = max(0, self.cursor - 1)
        elif matches:
            self.cursor = min(len(matches) - 1, self.cursor + 1)

    def wants_wheel(self) -> bool:
        """True while the box can still act on a notch.

        A list with one entry has nowhere to walk to; claiming the notch
        anyway would swallow it and leave the operator with a wheel that
        does nothing at all.
        """
        return len(self.matches) > 1

    def click(self, mx: int, my: int) -> bool:
        """A press inside the box: walk the highlight to that row; only a
        second click on the already-highlighted row accepts it. Anything
        else inside (borders, the ``… more`` row) is swallowed — the
        ``… more`` row pages forward like PageDown."""
        rect = self._last_rect
        if rect is None:
            return False
        x, y, w, h = rect
        if not (x <= mx < x + w and y <= my < y + h):
            return False
        row = my - (y + 1)
        if row < 0 or row > h - 3:
            return True  # border/title rows: consumed, no action
        matches = self.matches
        visible = max(1, h - 3)
        window = matches[self._scroll : self._scroll + visible]
        if row >= len(window):
            if self._scroll + visible < len(matches):
                self.cursor = min(len(matches) - 1, self.cursor + self.max_rows)
            return True
        idx = self._scroll + row
        if idx == self.cursor:
            self.result = matches[idx].value
        else:
            self.cursor = idx
        return True

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
                            # Caller-supplied hook: keep it isolated so the
                            # menu still removes the entry locally.
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
        # Clamp to the frame: in a cramped terminal the box must shrink
        # (never below its border pair), never spill off-screen.
        target = max(40, self.width or 0) or self._auto_width()
        width = max(2, min(cols, target))
        height = max(3, min(rows - 2, self.max_rows + 4))
        x = max(0, min((cols - width) // 2, max(0, cols - width)))
        y = max(0, min((rows - height) // 2, max(0, rows - height)))
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
        # Opaque box first (the conversation behind must not show
        # through), then the frame with the title baked into the border.
        _blank_box(buf, x, y, width, height, box_style)
        top = "╭─ " + (self.title + ("  filter: " + self.query if self.query else "")) + " "
        top += "─" * max(0, width - visible_len(top) - 1) + "╮"
        buf.set_str(x, y, top, box_style)
        label = self.title + ("  filter: " + self.query if self.query else "")
        buf.set_str(x + 3, y, label[: max(0, width - 4)], title_style)
        # Rows
        visible = max(1, height - 3)
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
        if hidden > 0 and height >= 4:
            buf.set_str(
                x + 2, y + height - 2, f"… {hidden} more (wheel or click)"[: width - 3], dim_style
            )


class QuestionCard:
    """A blocking prompt: the answer is typed at the card, not a line."""

    def __init__(self, title: str, body: str, choices: str = "yna") -> None:
        self.title = title
        self.body = body
        self.choices = choices  # subset of "yna" = yes/no/always
        self.answer: str | None = None
        # Body scroll (wheel): lines hidden above the rendered window.
        self.scroll = 0
        self._last_view = 1
        self._hint_zones: list[tuple[int, int, str]] = []
        self._last_rect: tuple[int, int, int, int] | None = None

    @property
    def finished(self) -> bool:
        return self.answer is not None

    @property
    def rect(self) -> tuple[int, int, int, int] | None:
        """The card geometry from the last render (None before drawing)."""
        return self._last_rect

    def consume_wheel(self, direction: int) -> None:
        """A wheel notch scrolls a long body one line at a time."""
        limit = max(0, len(self.body.split("\n")) - self._last_view)
        self.scroll = max(0, min(self.scroll + direction, limit))

    def wants_wheel(self) -> bool:
        """True only while the body is taller than the card.

        A body that fits is not scrollable, so a notch over the card
        belongs to the conversation behind it rather than being swallowed
        by a card that cannot move.
        """
        return len(self.body.split("\n")) > self._last_view

    def click(self, mx: int, my: int) -> bool:
        """A press inside the card: a drawn button answers it, anything
        else is swallowed (a modal must not highlight the page)."""
        rect = self._last_rect
        if rect is None:
            return False
        x, y, w, h = rect
        if not (x <= mx < x + w and y <= my < y + h):
            return False
        for zx, zy, answer in self._hint_zones:
            if my == zy and zx <= mx < zx + 6:
                self.answer = answer
                return True
        return True

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
        width = max(
            2,
            min(
                cols,
                max(50, max(visible_len(ln) for ln in [self.title] + body_lines) + 10),
            ),
        )
        height = max(3, min(rows - 2, max(4, len(body_lines) + 4)))
        x = max(0, min((cols - width) // 2, max(0, cols - width)))
        y = max(0, min(max(1, (rows - height) // 2 - 2), max(0, rows - height)))
        self._last_rect = (x, y, width, height)
        box_style = buf.styles_table.id_for(S_BOX)
        title_style = buf.styles_table.id_for(S_SELECT)
        dim_style = buf.styles_table.id_for(S_DIM)
        _blank_box(buf, x, y, width, height, box_style)
        top = "╭─ " + self.title + " " + "─" * max(0, width - visible_len(self.title) - 6) + "╮"
        buf.set_str(x, y, top, title_style)
        # Body window: ``scroll`` lines live above the window (wheel down
        # to reach them); the first row shows the indicator instead of
        # content so the hidden part is discoverable.
        max_body = height - 3
        if self.scroll > 0 and max_body >= 2:
            buf.set_str(
                x + 2, y + 1, f"… {self.scroll} more above (wheel)"[: width - 4], dim_style
            )
            window = body_lines[self.scroll : self.scroll + max_body - 1]
            first_row = y + 2
        else:
            self.scroll = 0
            window = body_lines[: max(0, max_body)]
            first_row = y + 1
        self._last_view = max(1, len(window))
        for i, line in enumerate(window):
            buf.set_str(x + 2, first_row + i, line[: width - 4], dim_style)
        hints = {
            "yna": "[y]es   [n]o   [a]lways for this session",
            "yn": "[y]es   [n]o",
        }.get(self.choices, "[y]es   [n]o")
        self._hint_zones = []
        if height >= 4:
            hint_row = y + height - 2
            buf.set_str(x + 2, hint_row, hints[: width - 4], dim_style)
            for c in "yna":
                if c in self.choices:
                    idx = hints.find(f"[{c}]")
                    if idx >= 0:
                        self._hint_zones.append((x + 2 + idx, hint_row, c))


class LinePrompt:
    """A single-line text input in a card (used for keys and confirmations)."""

    def __init__(self, label: str, secret: bool = False, default: str = "") -> None:
        self.label = label
        self.secret = secret
        self.buffer = default
        self.cursor = len(default)
        self.result: str | None = None
        self.cancelled = False
        # Wheel-slid window into a value wider than the card (None =
        # anchored on the caret); any edit re-anchors.
        self._view_col: int | None = None
        self._last_rect: tuple[int, int, int, int] | None = None

    @property
    def finished(self) -> bool:
        return self.result is not None or self.cancelled

    @property
    def rect(self) -> tuple[int, int, int, int] | None:
        """The card geometry from the last render (None before drawing)."""
        return self._last_rect

    @property
    def _text_cols(self) -> int:
        """Visible text columns inside the card (mirrors render)."""
        rect = self._last_rect
        if rect is not None:
            return max(4, rect[2] - 6)
        return 40

    def consume_wheel(self, direction: int) -> None:
        """A wheel notch slides the value; clamped at both ends."""
        span = self._text_cols
        if self._view_col is None:
            shown = visible_len(self.buffer)
            self._view_col = 0 if shown <= span else max(0, min(self.cursor, shown) - span // 2)
        limit = max(0, len(self.buffer) - span)
        self._view_col = max(0, min(self._view_col + direction, limit))

    def wants_wheel(self) -> bool:
        """True only while the value is wider than the card."""
        return len(self.buffer) > self._text_cols

    def click(self, mx: int, my: int) -> bool:
        """The card has no buttons: a press inside is swallowed so it
        cannot start a selection on the conversation behind it."""
        rect = self._last_rect
        if rect is None:
            return False
        x, y, w, h = rect
        return x <= mx < x + w and y <= my < y + h

    def consume_key(self, key: str, mods: frozenset = frozenset()) -> None:
        before = (self.buffer, self.cursor)
        self._consume_key(key, mods)
        if (self.buffer, self.cursor) != before:
            self._view_col = None  # an edit re-anchors the wheel window

    def _consume_key(self, key: str, mods: frozenset = frozenset()) -> None:
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
        self._view_col = None

    def render(self, buf: Buffer, cols: int, rows: int) -> None:
        width = max(2, min(cols, max(56, visible_len(self.label) + 20)))
        height = min(4, max(3, rows - 2))
        x = max(0, min((cols - width) // 2, max(0, cols - width)))
        y = max(0, min(max(1, (rows - height) // 2 - 2), max(0, rows - height)))
        self._last_rect = (x, y, width, height)
        box_style = buf.styles_table.id_for(S_BOX)
        title_style = buf.styles_table.id_for(S_SELECT)
        dim_style = buf.styles_table.id_for(S_DIM)
        _blank_box(buf, x, y, width, height, box_style)
        top = "╭─ " + self.label + " " + "─" * max(0, width - visible_len(self.label) - 6) + "╮"
        buf.set_str(x, y, top, title_style)
        shown = ("*" * len(self.buffer)) if self.secret else self.buffer
        # Window the value: around the caret normally, or at the
        # wheel-slid offset (any edit re-anchors to the caret).
        span = max(4, width - 6)
        if self._view_col is not None:
            keep = max(0, min(self._view_col, max(0, len(shown) - span)))
        elif visible_len(shown) <= span:
            keep = 0
        else:
            keep = max(0, self.cursor - span // 2)
        shown_window = shown[keep : keep + span]
        buf.set_str(x + 2, y + 1, shown_window[: width - 4], dim_style)
        if height >= 4:
            buf.set_str(x + 2, y + 2, "type your answer · enter confirms · esc cancels", dim_style)
        caret_col = min(x + width - 2, x + 2 + max(0, self.cursor - keep))
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
    anchor_x: int,
) -> tuple[int, int, int, int]:
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
    height = len(rows) + 2  # borders
    width = max(2, min(max(2, cols - 2), max(40, max(visible_len(t) for _, t in rows) + 4)))
    # Hang off the token being completed, clamped to the right edge, and
    # stop with the bottom border one row above the status line so the
    # prompt being typed is never covered.
    x = max(0, min(anchor_x, max(0, cols - width)))
    y = max(1, anchor_row - height)
    dim = buf.styles_table.id_for(S_DIM)
    sel = buf.styles_table.id_for(S_SELECT)
    box = buf.styles_table.id_for(S_BOX)
    _blank_box(buf, x, y, width, height, box)
    for i, (idx, text) in enumerate(rows):
        style = sel if idx == selected else dim
        buf.set_str(x + 1, y + 1 + i, text[: width - 2], style)
    return x, y, width, height
