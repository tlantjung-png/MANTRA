"""Menu: keyboard/mouse, filterable, mouse-aware."""

from __future__ import annotations

import os
import re
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

_CURSOR_REPORT = re.compile(r"\033\[(\d+);(\d+)R")


# Re-exported for the console's secret reader; shared by every interactive part.
from mantra.term import raw_mode, safe_write, visible_len  # noqa: F401
from mantra import theme

# ANSI: enable mouse click reporting and the SGR coordinate format that
# reports positions larger than 223 cells.
_MOUSE_ON = "\033[?1000h\033[?1006h"
_MOUSE_OFF = "\033[?1000l\033[?1006l"

_SGR_MOUSE = re.compile(r"<(\d+);(\d+);(\d+)([Mm])")

KEY_UP = "key:up"
KEY_DOWN = "key:down"
KEY_ENTER = "key:enter"
KEY_CANCEL = "key:cancel"
KEY_BACKSPACE = "key:backspace"
KEY_MOUSE = "key:mouse"
KEY_PAGE_DOWN = "key:page-down"

_ANSI_RE = re.compile(r"\033\[[0-9;?]*[ -/]*[@-~]")





@dataclass
class Option:
    """One row. ``value`` is returned; ``label`` and ``hint`` are shown."""

    value: str
    label: str = ""
    hint: str = ""
    enabled: bool = True

    def __post_init__(self) -> None:
        if not self.label:
            self.label = self.value

    @property
    def text(self) -> str:
        return f"{self.label}  {self.hint}" if self.hint else self.label


def options_from(values: Iterable[Any]) -> list[Option]:
    """Coerce plain strings, ``(value, hint)`` pairs, or Options."""
    out: list[Option] = []
    for item in values:
        if isinstance(item, Option):
            out.append(item)
        elif isinstance(item, (tuple, list)) and len(item) == 2:
            out.append(Option(value=str(item[0]), hint=str(item[1])))
        else:
            out.append(Option(value=str(item)))
    return out


@dataclass
class MouseEvent:
    """A parsed SGR mouse report."""

    button: int
    column: int
    row: int
    pressed: bool


class Menu:
    """Draws a selectable list below the cursor."""

    def __init__(
        self,
        style: Any,
        title: str,
        options: Sequence[Option],
        *,
        hint: str = "",
        max_rows: int = 12,
        allow_filter: bool = True,
        cursor: int = 0,
        frame: Any = None,
        allow_delete: bool = False,
        on_delete: Any = None,
    ) -> None:
        self.style = style
        self.title = title
        self.options = list(options)
        self.hint = hint
        self.max_rows = max_rows
        self.allow_filter = allow_filter
        self.query = ""
        self.cursor = cursor
        # First visible index in the scroll window (see _visible).
        self._scroll = 0
        # Measured absolute screen row of the first option row; None until
        # a DSR report succeeds. Instance state: two menus must not share it.
        self._row_base: int | None = None
        # Menus draw inside the frame so a list never hangs off the screen edge.
        self.frame = frame
        # Measured once, on the first draw: see _cursor_row.
        self._row_base_auto: int | None = None
        self.allow_delete = allow_delete
        self.on_delete = on_delete
        self._deleted: str | None = None

    # ---- public API ------------------------------------------------------

    def pick(self) -> str | None:
        """Show the menu and return the chosen value.

        "" cancels by key; None means no terminal to draw on or an interrupt.
        """
        if not self.options:
            return None
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            return None

        if os.name == "nt":
            os.system("")  # enable VT processing on Windows consoles

        drawn = 0
        try:
            sys.stdout.write(_MOUSE_ON)
            sys.stdout.flush()
            with self._raw_mode():
                drawn = self._draw(drawn)
                while True:
                    try:
                        key = self._read_key()
                    except KeyboardInterrupt:
                        return None
                    result = self._handle(key)
                    if result is not None:
                        return result
                    drawn = self._draw(drawn)
        except KeyboardInterrupt:
            return None
        finally:
            try:
                sys.stdout.write(_MOUSE_OFF)
            except Exception:
                pass
            try:
                self._finish(drawn)
            except Exception:
                pass

    # ---- state -----------------------------------------------------------

    @property
    def matches(self) -> list[Option]:
        if not self.allow_filter or not self.query:
            return self.options
        needle = self.query.lower()
        return [o for o in self.options if needle in o.text.lower()]

    def _visible(self) -> list[Option]:
        """The scroll window of options on screen, following the cursor.

        Without a moving window, navigation past ``max_rows`` advanced the
        cursor through rows that were never rendered — the operator was
        moving a highlight they could not see.
        """
        matches = self.matches
        if len(matches) <= self.max_rows:
            self._scroll = 0
            return matches
        start = max(0, min(self.cursor - self.max_rows // 2, len(matches) - self.max_rows))
        self._scroll = start
        return matches[start : start + self.max_rows]

    def _handle(self, key: str) -> str | None:
        """Return a value to finish, or None to keep going."""
        matches = self.matches
        # d/D delete the highlighted item, except while filtering.
        if self.allow_delete and key in ("d", "D") and not self.query:
            if matches and 0 <= self.cursor < len(matches):
                target = matches[self.cursor]
                # Don't delete the special add-entry
                if target.value.startswith("+"):
                    return None
                if self.on_delete:
                    try:
                        self.on_delete(target.value)
                    except Exception:
                        pass
                # Remove from options list
                self.options = [o for o in self.options if o.value != target.value]
                self._deleted = target.value
                if self.cursor >= len(self.matches):
                    self.cursor = max(0, len(self.matches) - 1)
                # If nothing left, cancel
                if not self.options or all(o.value.startswith("+") for o in self.options):
                    return ""
                return None
        if key == KEY_UP:
            self.cursor = max(0, self.cursor - 1)
            return None
        if key == KEY_DOWN:
            if matches:
                self.cursor = min(len(matches) - 1, self.cursor + 1)
            return None
        if key == KEY_BACKSPACE:
            self.query = self.query[:-1]
            self.cursor = 0
            return None
        if key == KEY_PAGE_DOWN:
            if matches:
                self.cursor = min(len(matches) - 1, self.cursor + self.max_rows)
            return None
        if key == KEY_ENTER:
            if not matches or self.cursor >= len(matches):
                return None
            return matches[self.cursor].value
        if key == KEY_CANCEL:
            return ""  # key-cancel, distinct from None (interrupt / no tty)
        if isinstance(key, MouseEvent):
            # Only a left press selects: a drag-release reports the row
            # it ended on, which is not what the operator clicked.
            if not (key.pressed and key.button == 0):
                return None
            option = self._option_at_row(key.row)
            if option is not None:
                return option.value
            return None
        if key and len(key) == 1 and key >= " ":
            if self.allow_filter:
                self.query += key
                self.cursor = 0
            return None
        return None

    def _option_at_row(self, screen_row: int) -> Option | None:
        """Map an absolute terminal row to an option using the measured base row."""
        if self._row_base is None:
            return None
        index = screen_row - self._row_base
        if 0 <= index < len(self._visible()):
            return self._visible()[index]
        return None

    # ---- rendering -------------------------------------------------------

    def _locate(self, offset: int) -> None:
        """Measure the first option's absolute screen row so clicks map correctly."""
        if self._row_base_auto is None:
            self._row_base_auto = self._cursor_row()
        if self._row_base_auto is not None:
            self._row_base = self._row_base_auto + offset

    def _cursor_row(self) -> int | None:
        """The caret's absolute row via Device Status Report, or None on timeout."""
        out = sys.stdout
        try:
            out.write("\033[6n")
            out.flush()
        except Exception:
            return None
        return self._read_report()

    def _read_report(self) -> int | None:
        deadline = time.monotonic() + 0.25
        buf = ""
        try:
            if os.name == "nt":
                import msvcrt

                while time.monotonic() < deadline:
                    if msvcrt.kbhit():
                        buf += msvcrt.getwch()
                        if buf.endswith("R"):
                            break
                    else:
                        time.sleep(0.01)
            else:
                import select

                while time.monotonic() < deadline:
                    if not select.select([sys.stdin], [], [], 0.02)[0]:
                        continue
                    buf += sys.stdin.read(1)
                    if buf.endswith("R"):
                        break
        except Exception:
            return None
        match = _CURSOR_REPORT.search(buf)
        return int(match.group(1)) if match else None

    def _finish(self, drawn: int) -> None:
        """Erase the menu rows and leave the caret on a clean line."""
        out = sys.stdout
        if drawn:
            # Down one row and clear per drawn row, then back up over them.
            for _ in range(drawn):
                out.write("\033[1B\r\033[K")
            out.write(f"\033[{drawn}A")
        out.write("\r\033[K")
        out.flush()

    def _draw(self, drawn: int) -> int:
        out = sys.stdout
        # Move down first, then clear: clearing before moving erases the
        # current row and leaves the bottom row of the menu on screen.
        out.write("\r\033[K")
        for _ in range(drawn):
            out.write("\033[1B\r\033[K")
        for _ in range(drawn):
            out.write("\033[1A")

        rows = self._rows()
        if self.frame is not None:
            # Framed mode: the title sits on the row the caret is already
            # on, so the menu does not open with a blank gap above it.
            painted = [self.frame.frame(row) for row in rows]
            for index, row in enumerate(painted):
                if index:
                    out.write("\n")
                # Option labels are user content (endpoints, models, skill
                # descriptions) and must not crash a narrow-locale console.
                safe_write(row)
            if len(painted) > 1:
                out.write(f"\033[{len(painted) - 1}A")
            out.write("\r")
            self._locate(offset=1)
        else:
            for row in rows:
                out.write("\n\033[K")
                safe_write(row)
            if rows:
                # Move the caret back up over the painted rows so the menu
                # stays anchored below the original line.
                out.write(f"\033[{len(rows)}A")
            out.write("\r")
            self._locate(offset=2)
        out.flush()
        return len(rows)

    def _rows(self) -> list[str]:
        s = self.style
        matches = self.matches
        visible = self._visible()

        # Fallback click mapping until a DSR cursor report measures the
        # real row (see _locate): the first option sits one row below the
        # header block, which is the title plus an optional filter/invite
        # row. A hardcoded guess broke once a filter row appeared.
        header_rows = 1 + (1 if self.allow_filter and (self.query or len(self.options) > self.max_rows) else 0)
        self._row_base = header_rows + 1

        # Blood & Bone: title in bone bold, filter in ash, the selected
        # row in the single crimson accent, everything else quiet.
        rows = [s._wrap(theme.BONE_BOLD, self.title) if self.title else ""]
        if self.allow_filter and self.query:
            rows.append(s._wrap(theme.ASH, f"  filter: {self.query}_"))
        elif self.allow_filter and len(self.options) > self.max_rows:
            rows.append(s._wrap(theme.FAINT, "  type to filter"))

        if not matches:
            rows.append(s._wrap(theme.FAINT, "  (no matches)"))
            return rows

        for index, option in enumerate(visible):
            # The window scrolls, so the highlighted row is the one whose
            # absolute match index equals the cursor, not window index 0.
            selected = self._scroll + index == self.cursor
            marker = "›" if selected else " "
            line = f" {marker} {option.text}"
            if selected:
                rows.append(s._wrap(theme.BLOOD_BOLD, line))  # selected — crimson
            elif not option.enabled:
                rows.append(s._wrap(theme.FAINT, line))
            else:
                rows.append(line)

        hidden = len(matches) - len(visible)
        if hidden > 0:
            rows.append(s._wrap(theme.FAINT, f"   ... {hidden} more"))
        if self.hint:
            rows.append(s._wrap(theme.FAINT, "  " + self.hint))
        return rows

    # ---- input -----------------------------------------------------------

    @contextmanager
    def _raw_mode(self):
        with raw_mode():
            yield

    def _input_pending(self) -> bool:
        """True when more bytes are already buffered on stdin."""
        if os.name == "nt":
            try:
                import msvcrt

                return bool(msvcrt.kbhit())
            except ImportError:  # pragma: no cover
                return False
        try:
            import select
        except ImportError:  # pragma: no cover - POSIX always has select
            return True
        try:
            return bool(select.select([sys.stdin], [], [], 0)[0])
        except Exception:
            return True

    def _read_char(self) -> str:
        if os.name == "nt":
            import msvcrt

            char = msvcrt.getwch()
            if char in ("\x00", "\xe0"):
                # Extended keys: the prefix byte is followed by a code byte.
                return {"H": KEY_UP, "P": KEY_DOWN}.get(msvcrt.getwch(), char)
            return char
        return sys.stdin.read(1)

    def _read_key(self):
        char = self._read_char()
        if char == "\r" or char == "\n":
            return KEY_ENTER
        if char == "\x03":  # ctrl+c
            raise KeyboardInterrupt
        if char == "\x1b":
            # A lone Escape cancels, but an arrow or mouse report also
            # starts with Escape; peek before committing to cancel.
            if self._input_pending():
                return self._read_sequence(char)
            return KEY_CANCEL
        if char in ("\x7f", "\b"):
            return KEY_BACKSPACE
        if char == " ":
            # Space pages forward in menus (PageDown equivalent), but only
            # while the filter query is empty: once the operator is typing
            # a query, space is a character like any other.
            if self.allow_filter and self.query:
                return char
            matches = self.matches
            if matches:
                self.cursor = min(len(matches) - 1, self.cursor + self.max_rows)
            return None
        if not self._input_pending():
            return char
        # Buffered input means this char starts a sequence, not a literal key.
        return self._read_sequence(char)

    def _read_sequence(self, first: str):
        """Read the rest of an escape or control sequence."""
        if os.name == "nt":
            rest = ""
            while self._input_pending() and len(rest) < 32:
                rest += self._read_char()
                if rest[-1] in ("M", "m", "~") or rest[-1].isalpha():
                    break
            return self._classify(first + rest)

        if first == "\x1b":
            second = sys.stdin.read(1)
            if second != "[":
                return first
            return self._read_bracket()

        arrow = {"A": KEY_UP, "B": KEY_DOWN}.get(first)
        return arrow or first

    def _read_bracket(self):
        if os.name == "nt":
            # Windows never classifies bracket sequences; treat them as cancel.
            return KEY_CANCEL
        body = ""
        while len(body) < 32:
            char = sys.stdin.read(1)
            if char in ("M", "m") or char.isalpha():
                body += char
                break
            body += char
            if char in ("<",) and not self._input_pending():
                # No more buffered bytes: the rest of this report will
                # never arrive, so stop waiting on it.
                break

        match = _SGR_MOUSE.match(body)
        if match:
            button, column, row, state = match.groups()
            return MouseEvent(
                button=int(button),
                column=int(column),
                row=int(row),
                pressed=state == "M",
            )

        # CSI [ A / [ B are arrows; anything else is not something we
        # act on, so treat it as a no-op keystroke.
        if body == "A":
            return KEY_UP
        if body == "B":
            return KEY_DOWN
        if body == "6~":
            return KEY_PAGE_DOWN
        return None

    def _classify(self, text: str):
        if not text.startswith("\x1b"):
            return text
        match = _SGR_MOUSE.search(text)
        if match:
            button, column, row, state = match.groups()
            return MouseEvent(
                button=int(button),
                column=int(column),
                row=int(row),
                pressed=state == "M",
            )
        if text.endswith("A"):
            return KEY_UP
        if text.endswith("B"):
            return KEY_DOWN
        return None


def choose(
    style: Any,
    title: str,
    options: Sequence[Any],
    *,
    hint: str = "",
    max_rows: int = 12,
    allow_filter: bool = True,
    cursor: int = 0,
    frame: Any = None,
    allow_delete: bool = False,
    on_delete: Any = None,
) -> str | None:
    """Convenience wrapper: build a menu from anything and pick from it."""
    return Menu(
        style,
        title,
        options_from(options),
        hint=hint,
        max_rows=max_rows,
        allow_filter=allow_filter,
        cursor=cursor,
        frame=frame,
        allow_delete=allow_delete,
        on_delete=on_delete,
    ).pick()
