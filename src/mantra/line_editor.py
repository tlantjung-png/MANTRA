"""Single-key editor with completion popup; falls back to input when not tty."""

from __future__ import annotations

import os
import re
import sys
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable


# Re-export shared primitives so every module agrees on width and size.
# Previously this file counted wide chars (2 cols) while console/compact
# counted 1, causing cursor drift for CJK paths. Centralised in term.py.
from mantra.term import safe_write, term_size, visible_len  # noqa: F401

_ANSI_RE = re.compile(r"\033\[[0-9;?]*[ -/]*[@-~]")

# SGR mouse report body: [ESC [] < button ; column ; row M|m — the "<"
# is optional because some collection paths strip it before parsing.
_SGR_MOUSE = re.compile(r"<?(\d+);(\d+);(\d+)([Mm])")


class MouseEvent:
    """A parsed SGR mouse report."""

    __slots__ = ("button", "column", "row", "pressed")

    def __init__(self, button: int, column: int, row: int, pressed: bool) -> None:
        self.button = button
        self.column = column
        self.row = row
        self.pressed = pressed


def _parse_sgr_body(body: str):
    """MouseEvent from the text after ``ESC [``, or ESC when malformed."""
    m = _SGR_MOUSE.match(body)
    if not m:
        return "\x1b"
    button, column, row, state = m.groups()
    return MouseEvent(int(button), int(column), int(row), state == "M")


# Bracketed-paste body is kept verbatim: a lost tail would re-read as
# stray keys. Ends at ESC[201~, EOF, or an idle gap.
_PASTE_IDLE = 0.75  # seconds without input -> treat the paste as finished


def _assemble_bracketed_paste(read_char, ready, idle_grace: float = _PASTE_IDLE) -> str:
    """Collect a bracketed-paste body after ESC[200~ has been consumed.

    ``read_char`` yields the next character (blocking); ``ready()``
    reports whether input is pending right now.
    """
    import time

    parts: list[str] = []
    tail = ""
    last = time.monotonic()
    marker = "\x1b[201~"
    while True:
        if not ready():
            if time.monotonic() - last > idle_grace:
                break
            time.sleep(0.004)
            continue
        ch = read_char()
        if not ch:
            break
        last = time.monotonic()
        parts.append(ch)
        tail = (tail + ch)[-len(marker):]
        if tail == marker:
            del parts[-len(marker):]
            break
    return "".join(parts)


# Key constants for special keys that don't map to a single character.
KEY_LEFT = "key:left"
KEY_RIGHT = "key:right"
KEY_UP = "key:up"
KEY_DOWN = "key:down"
KEY_DELETE = "key:delete"
KEY_HOME = "key:home"
KEY_END = "key:end"
KEY_PAGE_UP = "key:page-up"
KEY_PAGE_DOWN = "key:page-down"
KEY_RESIZE = "key:resize"
KEY_CTRL_LEFT = "key:ctrl-left"
KEY_CTRL_RIGHT = "key:ctrl-right"
KEY_SHIFT_ENTER = "key:shift-enter"

_WINDOWS_SPECIALS: dict[str, str] = {
    "H": KEY_UP,
    "P": KEY_DOWN,
    "K": KEY_LEFT,
    "M": KEY_RIGHT,
    "S": KEY_PAGE_DOWN,
    "I": KEY_PAGE_UP,
    "G": KEY_HOME,
    "O": KEY_END,
    "R": KEY_DELETE,
    # VT arrow codes, for terminals that report arrows this way on Windows.
    "A": KEY_UP,
    "B": KEY_DOWN,
    "C": KEY_RIGHT,
    "D": KEY_LEFT,
}

_POSIX_SPECIALS: dict[str, str] = {
    "A": KEY_UP,
    "B": KEY_DOWN,
    "C": KEY_RIGHT,
    "D": KEY_LEFT,
    "H": KEY_HOME,
    "F": KEY_END,
    "5~": KEY_PAGE_UP,
    "6~": KEY_PAGE_DOWN,
    "2~": KEY_DELETE,
    "3~": KEY_DELETE,
}

def _prev_word_pos(text: str, pos: int) -> int:
    if pos <= 0:
        return 0
    i = pos
    while i > 0 and text[i-1].isspace():
        i -= 1
    while i > 0 and not text[i-1].isspace():
        i -= 1
    return i

def _next_word_pos(text: str, pos: int) -> int:
    n = len(text)
    if pos >= n:
        return n
    i = pos
    while i < n and not text[i].isspace():
        i += 1
    while i < n and text[i].isspace():
        i += 1
    return i


def _clip_vis(text: str, width: int) -> str:
    """Truncate text so its visible width fits ``width`` columns."""
    if width <= 0:
        return ""
    out: list[str] = []
    used = 0
    for ch in text:
        w = visible_len(ch)
        if used + w > width:
            break
        out.append(ch)
        used += w
    return "".join(out)


def _char_columns(text: str) -> int:
    """Visible width of one character (wide chars take two columns)."""
    import unicodedata

    if unicodedata.combining(text):
        return 0
    if unicodedata.category(text) in ("Mn", "Me", "Cf"):
        return 0
    return 2 if unicodedata.east_asian_width(text) in ("W", "F") else 1


def _column_to_index(text: str, col: int) -> int:
    """Buffer index whose visible column is ``col`` (wide-char aware).

    Mouse reports give a terminal column; mapping it by raw character
    count lands on the wrong character whenever wide (CJK/emoji)
    characters precede the click point.
    """
    width = 0
    for i, ch in enumerate(text):
        if width >= col:
            return i
        width += _char_columns(ch)
    return len(text)


def _display_to_buffer_index(buffer: str, display_index: int) -> int:
    """Map an index in the newline-expanded display text back to the buffer.

    The display form replaces each newline with " ↵ ", so every
    newline before the target adds two display-only characters.
    """
    remaining = display_index
    bi = 0
    while bi < len(buffer) and remaining > 0:
        if buffer[bi] == "\n":
            remaining -= 3
        else:
            remaining -= 1
        bi += 1
    return min(bi, len(buffer))


def _click_to_buffer_index(buffer: str, col: int) -> int:
    """Terminal column on the prompt row -> buffer index."""
    display = buffer.replace("\n", " ↵ ")
    display_index = _column_to_index(display, col)
    return _display_to_buffer_index(buffer, display_index)


def _size_chip(buffer: str) -> str:
    """Dim row marker: line and character counts of the prompt buffer.

    ``· 3 lines · 421 chars`` - the explicit size readout for a paste,
    so the operator always knows how much text entered the buffer.
    """
    lines = buffer.count("\n") + 1
    plural = "s" if lines != 1 else ""
    return f"· {lines} line{plural} · {len(buffer):,} chars"


def _line_window(text: str, caret: int, width: int) -> tuple[int, int, str]:
    """A horizontal window of ``text`` that keeps the caret visible.

    Returns (char index where the window starts, visible offset of the
    caret inside the returned string, shown text). Wide characters count
    by visible column width, so the window is measured in columns, not
    Python characters: a CJK char occupies two columns but one index.
    """
    if width <= 0:
        return 0, 0, ""
    n = len(text)
    cum = [0] * (n + 1)
    for i, ch in enumerate(text):
        cum[i + 1] = cum[i] + visible_len(ch)
    caret_vis = cum[min(caret, n)]
    if cum[n] <= width:
        return 0, caret_vis, text
    start = 0
    while start < caret and cum[start] < caret_vis - (width - 1):
        start += 1
    shown = _clip_vis(text[start:], width)
    return start, caret_vis - cum[start], shown

def _is_shift_pressed() -> bool:
    try:
        import ctypes
        return bool(ctypes.windll.user32.GetKeyState(0x10) & 0x8000)
    except Exception:
        return False

def _get_clipboard_text() -> str:
    # Clipboard read: subprocess first, ctypes fallback on Windows;
    # pbpaste/xclip/xsel chain on POSIX.
    try:
        if os.name == "nt":
            import subprocess
            try:
                out = subprocess.run(["powershell", "-command", "Get-Clipboard"], capture_output=True, text=True, timeout=2)
                if out.returncode == 0 and out.stdout is not None:
                    txt = out.stdout
                    if txt.endswith("\r\n"):
                        txt = txt[:-2]
                    elif txt.endswith("\n"):
                        txt = txt[:-1]
                    if txt:
                        return txt
            except Exception:
                pass
            try:
                import ctypes
                ctypes.windll.user32.OpenClipboard(0)
                try:
                    h = ctypes.windll.user32.GetClipboardData(13)
                    if not h:
                        return ""
                    ctypes.windll.kernel32.GlobalLock.restype = ctypes.c_void_p
                    ptr = ctypes.windll.kernel32.GlobalLock(h)
                    if not ptr:
                        return ""
                    try:
                        text = ctypes.wstring_at(ptr)
                        return text or ""
                    finally:
                        ctypes.windll.kernel32.GlobalUnlock(h)
                finally:
                    ctypes.windll.user32.CloseClipboard()
            except Exception:
                pass
        else:
            import subprocess
            for cmd in (["pbpaste"], ["xclip", "-o", "-selection", "clipboard"], ["xsel", "-b", "-o"]):
                try:
                    out = subprocess.run(cmd, capture_output=True, text=True, timeout=1)
                    if out.returncode == 0 and out.stdout:
                        return out.stdout
                except Exception:
                    continue
    except Exception:
        pass
    return ""

def _set_clipboard_text(text: str) -> None:
    # Clipboard write: clip subprocess, then ctypes fallback on Windows;
    # pbcopy/xclip/xsel chain on POSIX.
    try:
        if os.name == "nt":
            import subprocess
            try:
                p = subprocess.Popen(["clip"], stdin=subprocess.PIPE, text=True)
                p.communicate(text, timeout=2)
                if p.returncode == 0:
                    return
            except Exception:
                pass
            try:
                import ctypes
                ctypes.windll.user32.OpenClipboard(0)
                try:
                    ctypes.windll.user32.EmptyClipboard()
                    data = text.encode("utf-16-le") + b"\x00\x00"
                    h = ctypes.windll.kernel32.GlobalAlloc(0x0002, len(data))
                    if h:
                        ptr = ctypes.windll.kernel32.GlobalLock(h)
                        ctypes.memmove(ptr, data, len(data))
                        ctypes.windll.kernel32.GlobalUnlock(h)
                        ctypes.windll.user32.SetClipboardData(13, h)
                finally:
                    ctypes.windll.user32.CloseClipboard()
            except Exception:
                pass
        else:
            import subprocess
            for cmd in (["pbcopy"], ["xclip", "-selection", "clipboard"], ["xsel", "-b", "-i"]):
                try:
                    p = subprocess.Popen(cmd, stdin=subprocess.PIPE)
                    p.communicate(text.encode("utf-8"), timeout=1)
                    if p.returncode == 0:
                        break
                except Exception:
                    continue
    except Exception:
        pass

HINT = ""


@dataclass
class Completion:
    """A set of completion candidates with their source span."""

    start: int
    end: int
    items: list[str] = field(default_factory=list)
    labels: list[str] | None = None

    def label(self, index: int) -> str:
        if self.labels and 0 <= index < len(self.labels):
            return self.labels[index]
        return self.items[index] if 0 <= index < len(self.items) else ""


class LineEditor:
    """Single-key line editor with an inline completion popup."""

    def __init__(
        self,
        style: Any,
        completer: Any = None,
        max_popup: int = 8,
        hint: str = HINT,
        on_ctrl_g: Callable[[], None] | None = None,
        on_submit: Callable[[int], None] | None = None,
        no_popup: bool = False,
        popup_above: bool = False,
        on_page_up: Callable[[], None] | None = None,
        on_page_down: Callable[[], None] | None = None,
        on_resize: Callable[[], str | None] | None = None,
    ) -> None:
        self.style = style
        self.completer = completer
        self.max_popup = max_popup
        self.hint = hint
        self.on_submit = on_submit
        self.on_ctrl_g = on_ctrl_g
        self.on_page_up = on_page_up
        self.on_page_down = on_page_down
        self.on_resize = on_resize
        self._restore_region: Callable[[], None] | None = None
        self._region_cleared = False
        self._dismissed = False
        self._last_token: str | None = None
        self.no_popup = no_popup
        self.popup_above = popup_above

        # Called before each _draw to let the host restore content
        # after popup erasure.
        self.on_before_draw: Callable[[], None] | None = None

        # Absolute row for the prompt (when known). Used to compute
        # absolute rows for popup above, avoiding newlines in scroll region.
        self.fixed_row: int | None = None
        self._prev_fixed_row: int | None = None

        # Track terminal size for resize detection without side effects.
        self._term_cols: int | None = None
        self._term_rows: int | None = None

        # Selection auto-copy
        self._sel_anchor: int | None = None
        self._sel_end: int | None = None
        self._sel_active = False
        # Deferred "copied" toast: (mode, layout, clear_after). The toast
        # must not sleep inside the raw-mode read loop — that froze input
        # for up to 0.7s — so it is cleared on a later draw instead.
        self._toast: tuple[str, Any, float] | None = None

        # Keys buffered elsewhere (e.g. by the turn-scoped scroll reader
        # while a task streams) that the editor must deliver at its next
        # read, before touching the terminal.
        self.preload: list[str] = []

        # Multi-line prompt ("paste box") state: absolute rows currently
        # covered above the input row while the buffer spans several
        # lines, so the next draw/cleanup knows exactly what to restore.
        self._box_rows: list[int] = []
        self._box_active = False

    # ── public API ────────────────────────────────────────────

    def read(self, prompt: str = "", skip_newline: bool = False) -> str:
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            return input(prompt)

        if self.completer is not None and hasattr(self.completer, "begin"):
            self.completer.begin()

        head, sep, prompt = prompt.rpartition("\n")
        if sep:
            # Multi-line prompt: write the head immediately; only the tail
            # becomes the live editable line.
            safe_write(head + sep)
            sys.stdout.flush()

        buffer = ""
        cursor = 0
        popup: Completion | None = None
        selected = 0
        drawn = 0
        self._dismissed = False
        self._last_token = None
        # Enable bracketed paste only. Mouse reporting is deliberately NOT
        # enabled here: with it on, every drag is handed to the app and the
        # terminal's native selection over the conversation stops working,
        # which is the one thing users reach for first. The SGR parsing
        # below stays available for surfaces that do capture the mouse
        # (e.g. the turn-scoped scroll reader while a task streams).
        try:
            sys.stdout.write("\033[?2004h")
            sys.stdout.flush()
        except Exception:
            pass

        try:
            with self._raw_mode():
                drawn = self._draw(prompt, buffer, cursor, popup, selected, drawn)
                while True:
                    if self._region_cleared and self._restore_region is not None:
                        self._restore_region()
                        self._region_cleared = False
                    key = self._read_key()
                    if key == KEY_RESIZE:
                        # The host may have moved the fixed prompt to a new bottom row.
                        # A resize redraws the whole layout, so discard any popup-row
                        # bookkeeping tied to the old geometry before repainting. A
                        # multi-line box also forgets its old row numbers - the next
                        # draw starts fresh with the new geometry.
                        self._box_rows = []
                        self._box_active = False
                        if self.on_resize is not None:
                            try:
                                new_prompt = self.on_resize()
                                if isinstance(new_prompt, str):
                                    _head, _sep, prompt = new_prompt.rpartition("\n")
                            except Exception:
                                pass
                        drawn = 0
                        drawn = self._draw(prompt, buffer, cursor, popup, selected, drawn)
                        continue
                    if key in ("\r", "\n"):
                        if popup and popup.items and not self._dismissed:
                            chosen = popup.items[min(selected, len(popup.items) - 1)]
                            buffer = buffer[: popup.start] + chosen + buffer[popup.end :]
                            cursor = popup.start + len(chosen)
                            if popup.start == 0 and chosen.startswith("/"):
                                if self.on_submit is not None:
                                    self.on_submit(visible_len(prompt) + len(buffer))
                                break
                            popup, selected = self._recompute(buffer, cursor, selected)
                            self._dismissed = True
                            popup = None
                            drawn = self._draw(prompt, buffer, cursor, popup, selected, drawn)
                            continue
                        if self.on_submit is not None:
                            self.on_submit(visible_len(prompt) + len(buffer))
                        break
                    if key == "\x03":
                        raise KeyboardInterrupt
                    if key == "\x04":
                        if not buffer:
                            raise EOFError
                        continue
                    if key == "\x1b":
                        # Escape: dismiss popup if open, otherwise ignore.
                        if popup and popup.items:
                            self._dismissed = True
                            popup = None
                            drawn = self._draw(prompt, buffer, cursor, None, selected, drawn)
                        continue
                    if key == "\x07" and self.on_ctrl_g is not None:
                        try:
                            self.on_ctrl_g()
                        except Exception:
                            pass
                        drawn = self._draw(prompt, buffer, cursor, None, 0, drawn)
                        continue
                    if key in ("\x7f", "\b"):
                        if cursor > 0:
                            buffer = buffer[: cursor - 1] + buffer[cursor:]
                            cursor -= 1
                            self._last_token = None
                            popup, selected = self._recompute(buffer, cursor, selected)
                    elif key == KEY_LEFT:
                        cursor = max(0, cursor - 1)
                        popup, selected = self._recompute(buffer, cursor, selected)
                    elif key == KEY_RIGHT:
                        cursor = min(len(buffer), cursor + 1)
                        popup, selected = self._recompute(buffer, cursor, selected)
                    elif key == KEY_HOME:
                        cursor = 0
                        popup, selected = self._recompute(buffer, cursor, selected)
                    elif key == KEY_END:
                        cursor = len(buffer)
                        popup, selected = self._recompute(buffer, cursor, selected)
                    elif key == KEY_DELETE:
                        buffer = buffer[:cursor] + buffer[cursor + 1 :]
                        popup, selected = self._recompute(buffer, cursor, selected)
                    elif key == KEY_CTRL_LEFT:
                        cursor = _prev_word_pos(buffer, cursor)
                        popup, selected = self._recompute(buffer, cursor, selected)
                    elif key == KEY_CTRL_RIGHT:
                        cursor = _next_word_pos(buffer, cursor)
                        popup, selected = self._recompute(buffer, cursor, selected)
                    elif key == KEY_SHIFT_ENTER:
                        buffer = buffer[:cursor] + "\n" + buffer[cursor:]
                        cursor += 1
                        self._last_token = None
                        popup, selected = self._recompute(buffer, cursor, selected)
                    elif isinstance(key, str) and key.startswith("\x1b[200~"):
                        pasted = key[len("\x1b[200~"):]
                        if pasted.endswith("\x1b[201~"):
                            pasted = pasted[:-len("\x1b[201~")]
                        pasted = pasted.replace("\r\n", "\n").replace("\r", "\n")
                        buffer = buffer[:cursor] + pasted + buffer[cursor:]
                        cursor += len(pasted)
                        popup, selected = self._recompute(buffer, cursor, selected)
                    elif hasattr(key, "button") and hasattr(key, "row"):
                        # Wheel: SGR buttons 64 (up) and 65 (down). Scroll the
                        # viewport exactly like the keyboard PageUp/PageDown
                        # paths, so wheel and keys behave the same.
                        if key.pressed and key.button in (64, 65):
                            if key.button == 64 and self.on_page_up is not None:
                                self.on_page_up()
                            elif key.button == 65 and self.on_page_down is not None:
                                self.on_page_down()
                            self._region_cleared = True
                            continue
                        # Mouse selection — auto copy
                        is_prompt = self.fixed_row is not None and key.row == self.fixed_row
                        if key.pressed and key.button == 0:
                            if self._sel_anchor is None:
                                if is_prompt:
                                    pvis = visible_len(prompt)
                                    col = max(0, key.column - pvis - 1)
                                    idx = _click_to_buffer_index(buffer, col)
                                    self._sel_anchor = max(0, min(len(buffer), idx))
                                    self._sel_end = self._sel_anchor
                                    self._sel_active = True
                                else:
                                    self._sel_anchor = 0
                                    self._sel_end = 0
                                    self._sel_active = True
                            else:
                                if is_prompt:
                                    pvis = visible_len(prompt)
                                    col = max(0, key.column - pvis - 1)
                                    idx = _click_to_buffer_index(buffer, col)
                                    cur = max(0, min(len(buffer), idx))
                                    # Inclusive of the release cell, so a
                                    # drag ending on "o" copies "hello".
                                    self._sel_end = min(len(buffer), cur + 1)
                            drawn = self._draw(prompt, buffer, cursor, popup, selected, drawn)
                            continue
                        else:
                            if self._sel_anchor is not None and self._sel_active:
                                try:
                                    if is_prompt and self._sel_end is not None:
                                        start = min(self._sel_anchor, self._sel_end)
                                        finish = max(self._sel_anchor, self._sel_end)
                                        if start == finish:
                                            pvis = visible_len(prompt)
                                            col = max(0, key.column - pvis - 1)
                                            end = max(0, min(len(buffer), _click_to_buffer_index(buffer, col)))
                                            start = min(self._sel_anchor, end)
                                            finish = max(self._sel_anchor, end)
                                        if start != finish:
                                            sel_text = buffer[start:finish]
                                            _set_clipboard_text(sel_text)
                                            self._show_copied_toast(getattr(self, "layout_ref", None))
                                    else:
                                        try:
                                            layout = getattr(self, "layout_ref", None)
                                            raw = None
                                            if layout is not None and hasattr(layout, "get_line_at_row"):
                                                raw = layout.get_line_at_row(key.row)
                                            if raw is None:
                                                getter = getattr(self, "viewport_getter", None)
                                                if callable(getter):
                                                    lines = getter()
                                                    if lines and key.row:
                                                        # Rough offset: the getter's rows exclude
                                                        # chrome, so back up three screen rows.
                                                        idx = max(0, min(len(lines)-1, key.row - 3))
                                                        raw = lines[idx] if 0 <= idx < len(lines) else ""
                                            if raw:
                                                import re as _re2
                                                clean = _re2.sub(r"\x1b\[[0-9;]*m", "", raw).strip()
                                                if clean:
                                                    _set_clipboard_text(clean)
                                                    self._show_copied_toast(getattr(self, "layout_ref", None))
                                        except Exception:
                                            pass
                                except Exception:
                                    pass
                            self._sel_anchor = None
                            self._sel_end = None
                            self._sel_active = False
                            drawn = self._draw(prompt, buffer, cursor, popup, selected, drawn)
                            continue
                    elif key == KEY_UP:
                        if "\n" in buffer and self._box_active:
                            # Multi-line prompt open: arrows edit the
                            # pasted block instead of scrolling the
                            # transcript. No completion popup in a box.
                            cursor = self._vertical_caret(buffer, cursor, -1)
                            popup = None
                        elif popup and popup.items:
                            selected = max(0, selected - 1)
                        elif self.on_page_up is not None:
                            self.on_page_up()
                            self._region_cleared = True
                    elif key == KEY_DOWN:
                        if "\n" in buffer and self._box_active:
                            cursor = self._vertical_caret(buffer, cursor, 1)
                            popup = None
                        elif popup and popup.items:
                            selected = min(len(popup.items) - 1, selected + 1)
                        elif self.on_page_down is not None:
                            self.on_page_down()
                            self._region_cleared = True
                    elif key == KEY_PAGE_UP:
                        if self.on_page_up is not None:
                            self.on_page_up()
                            self._region_cleared = True
                    elif key == KEY_PAGE_DOWN:
                        if self.on_page_down is not None:
                            self.on_page_down()
                            self._region_cleared = True
                    elif key in ("\t",):
                        self._dismissed = False
                        popup, selected = self._recompute(buffer, cursor, selected)
                    elif len(key) == 1 and key.isprintable():
                        buffer = buffer[:cursor] + key + buffer[cursor:]
                        cursor += 1
                        self._last_token = None
                        popup, selected = self._recompute(buffer, cursor, selected)
                    elif key == "\x16":  # Ctrl+V paste
                        clip = _get_clipboard_text()
                        if clip:
                            clip = clip.replace("\r\n", "\n").replace("\r", "\n")
                            buffer = buffer[:cursor] + clip + buffer[cursor:]
                            cursor += len(clip)
                            popup, selected = self._recompute(buffer, cursor, selected)
                    else:
                        continue
                    drawn = self._draw(prompt, buffer, cursor, popup, selected, drawn)
        finally:
            try:
                sys.stdout.write("\033[?2004l")
                sys.stdout.flush()
            except Exception:
                pass
            self._finish(drawn, skip_newline=skip_newline)

        return buffer

    # ── completion ────────────────────────────────────────────

    def _recompute(self, buffer: str, cursor: int, selected: int):
        if self.completer is None or self._dismissed or self.no_popup:
            return None, 0
        completion = self.completer.complete(buffer, cursor)
        if completion is None or not completion.items:
            self._last_token = None
            return None, 0
        token = buffer[completion.start : completion.end]
        selected = 0 if token != self._last_token else selected
        self._last_token = token
        return completion, min(selected, len(completion.items) - 1)

    # ── multi-line prompt (paste box) ────────────────────────

    def _vertical_caret(self, buffer: str, cursor: int, delta: int) -> int:
        """Move the caret one line up/down, keeping its column where possible.

        Used by Up/Down while the multi-line prompt is open, so arrow
        keys edit the pasted block instead of scrolling the transcript.
        """
        if not buffer:
            return cursor
        lines = buffer.split("\n")
        starts = []
        idx = 0
        for line in lines:
            starts.append(idx)
            idx += len(line) + 1
        # Caret line and column (the caret may sit on a newline boundary).
        cl = min(len(lines) - 1, buffer[:cursor].count("\n"))
        col = max(0, cursor - starts[cl])
        col = min(col, len(lines[cl]))
        target = cl + delta
        if target < 0 or target >= len(lines):
            return cursor
        return starts[target] + min(col, len(lines[target]))

    def _box_geometry(self):
        """(layout, cols, content_top, fixed_row) when a multi-line box can
        be drawn above the fixed bottom prompt, else None."""
        if not self.popup_above or self.fixed_row is None:
            return None
        layout = getattr(self, "layout_ref", None)
        if layout is None or not getattr(layout, "active", False):
            return None
        try:
            cols = int(getattr(layout, "_cols", 0) or 0)
            top = int(getattr(layout, "content_top", 0) or 0)
        except Exception:
            return None
        if cols < 20 or top < 1 or self.fixed_row <= top:
            return None
        return layout, cols, top, self.fixed_row

    def _leave_box(self, clear_prompt: bool) -> None:
        """Undo a multi-line box: clear its rows and restore content/chrome."""
        rows, self._box_rows = self._box_rows, []
        self._box_active = False
        layout = getattr(self, "layout_ref", None)
        try:
            for r in rows:
                if r >= 1 and (self.fixed_row is None or r != self.fixed_row):
                    safe_write(f"\033[{r};1H\033[2K")
            if clear_prompt and self.fixed_row is not None:
                safe_write(f"\033[{self.fixed_row};1H\033[2K")
            if layout is not None and getattr(layout, "active", False) and hasattr(layout, "redraw_content_and_chrome"):
                layout.redraw_content_and_chrome()
            sys.stdout.flush()
        except Exception:
            pass

    def _draw_box(self, prompt, buffer, cursor, drawn_prev: int) -> int:
        """Render a multi-line buffer as a box that expands upward.

        The caret line sits at the bottom prompt row, earlier lines get
        rows above it, and the box's top border rises with the content,
        tail-clipping with a count of hidden lines when it would exceed
        the content area.
        """
        geo = self._box_geometry()
        if geo is None:
            return 0
        layout, cols, top, fixed = geo
        out = sys.stdout

        # First box frame: a full repaint clears any rows the previous
        # prompt left in the content area.
        if not self._box_active:
            try:
                layout.redraw_content_and_chrome()
            except Exception:
                pass
            self._box_rows = []
        else:
            old_rows = list(self._box_rows)
            for r in old_rows:
                if 1 <= r <= fixed:
                    safe_write(f"\033[{r};1H\033[2K")

        lines = buffer.split("\n")
        total_lines = len(lines)
        caret_line = min(total_lines - 1, buffer[:cursor].count("\n"))
        # Column of the caret inside its line.
        start = 0
        for _ in range(caret_line):
            start = buffer.index("\n", start) + 1
        caret_col = cursor - start

        # Prompt label = prompt text minus its absolute-positioning prefix.
        label = prompt
        head, sep, tail = label.rpartition("\n")
        if sep:
            label = tail
        label = re.sub(r"^\033\[\d+(?:;\d+)?H\033\[2K", "", label)

        # The box top border must stay at or below content_top: line rows
        # may occupy at most fixed - top rows (the caret row included).
        max_lines = max(1, fixed - top)
        first = max(0, caret_line + 1 - max_lines)
        vis_lines = lines[first : caret_line + 1]
        k = len(vis_lines)  # visible lines, caret line last (bottom)
        border_row = fixed - k  # top edge of the box rises with k

        # Rows freed by a smaller box (e.g. a deleted newline) must go
        # back to showing content, not stay blank. The box covers
        # border_row..fixed; rows above the new border were covered
        # before only if the box used to be taller.
        old_rows = list(getattr(self, "_box_rows", []))
        self._box_rows = []
        if old_rows:
            new_band = set(range(max(1, border_row), fixed + 1))
            if not new_band.issuperset(old_rows):
                try:
                    layout.redraw_content_and_chrome()
                except Exception:
                    pass
        self._box_active = True

        wall = self._box_wall() or ""
        label_vis = visible_len(label)
        # The real layout's bone label already opens the box ("│ MANTRA >").
        # Plain prompts (tests, read_choice) may not carry the wall, so
        # close the left edge the same way to keep the box a single shape.
        plain_label = re.sub(r"\033\[[0-9;]*m", "", label).strip()
        if not plain_label.startswith("│") and wall:
            label = wall + " " + label
            label_vis = visible_len(label)

        # Every line's text starts at the label's gutter column so the
        # box reads as one aligned editor; upper rows blank the gutter.
        gutter = max(1, label_vis)
        avail = max(0, cols - gutter - 1)  # minus the right wall

        # ── compose each row ────────────────────────────────────
        rows_text: list[tuple[int, str]] = []  # (absolute row, text)
        dim = getattr(self.style, "dim", None) or (lambda t: t)
        chip = _size_chip(buffer)
        if first > 0:
            # Drop the chip's own leading "· " so the clip marker reads
            # "… 9 more above · 30 lines · 421 chars" (one separator).
            chip = f"… {first} more above · " + chip.removeprefix("· ")
        edge = layout.box_edge_row(dim(chip)) if hasattr(layout, "box_edge_row") else dim(chip)
        rows_text.append((border_row, edge))

        # Upper rows reuse the label's gutter width as a blank prefix.
        if wall:
            blank_prefix = wall + " " + " " * max(0, label_vis - 2)
        else:
            blank_prefix = " " * label_vis

        for i, text in enumerate(vis_lines):
            if i == k - 1:
                # Caret line: label + text windowed around the caret.
                _, caret_disp, shown = _line_window(text, caret_col, avail)
                pad = max(0, avail - visible_len(shown))
                rows_text.append((fixed, label + shown + " " * pad + wall))
            else:
                shown = _clip_vis(text, avail)
                pad = max(0, avail - visible_len(shown))
                rows_text.append((border_row + 1 + i, blank_prefix + shown + " " * pad + wall))

        # ── write ───────────────────────────────────────────────
        for row_abs, text in rows_text:
            if 1 <= row_abs <= fixed:
                safe_write(f"\033[{row_abs};1H\033[2K{text}")
        self._box_rows = [r for r, _ in rows_text if 1 <= r <= fixed]

        # Cursor at the caret column on the prompt row.
        col = max(1, label_vis + caret_disp + 1)
        safe_write(f"\033[{fixed};{col}H")
        out.flush()
        return 0

    # ── rendering ─────────────────────────────────────────────

    def _box_wall(self) -> str | None:
        """Styled right wall when this editor owns the layout's boxed prompt row.

        The editor repaints the prompt row on every keystroke, so it must
        reserve the last column and re-emit the wall after the input text.
        """
        if not self.popup_above or self.fixed_row is None:
            return None
        layout = getattr(self, "layout_ref", None)
        if layout is None or not getattr(layout, "active", False):
            return None
        try:
            if int(getattr(layout, "prompt_row", 0) or 0) != self.fixed_row:
                return None
            glyph = layout.wall_glyph()
        except Exception:
            return None
        return glyph if glyph else None

    def _show_copied_toast(self, layout: Any) -> None:
        """Show the "copied" confirmation without blocking the read loop."""
        msg = self.style.dim("copied")
        import time as _t

        try:
            if layout is not None and getattr(layout, "active", False):
                layout.draw_border_status(msg)
                self._toast = ("border", layout, _t.monotonic() + 0.7)
            else:
                sys.stdout.write("\r\x1b[K" + msg + "\n")
                sys.stdout.flush()
                self._toast = ("line", None, _t.monotonic() + 0.35)
        except Exception:
            pass

    def _expire_toast(self) -> None:
        """Clear the "copied" toast once its display window has elapsed."""
        if not self._toast:
            return
        import time as _t

        mode, layout, until = self._toast
        if _t.monotonic() < until:
            return
        try:
            if mode == "border" and layout is not None:
                layout.draw_border_status("")
            elif mode == "line":
                sys.stdout.write("\x1b[1A\r\x1b[K")
                sys.stdout.flush()
        except Exception:
            pass
        self._toast = None

    def _draw(self, prompt, buffer, cursor, popup, selected, drawn) -> int:
        out = sys.stdout
        self._expire_toast()

        # The active compact layout is the single source of truth for
        # geometry: its prompt_row always wins over any cached or
        # term_size-derived value, so a resize repaint can never race a
        # stale editor position. The term_size fallback remains for
        # standalone editors that have no layout attached.
        layout = getattr(self, "layout_ref", None)
        if layout is not None and getattr(layout, "active", False):
            try:
                self.fixed_row = int(
                    getattr(layout, "prompt_row", 0) or self.fixed_row or 0
                )
            except Exception:
                pass
        elif self.popup_above and self.fixed_row is None:
            try:
                _, rows = term_size()
                self.fixed_row = rows
            except Exception:
                pass

        # A multi-line buffer on the fixed bottom prompt renders as a
        # real box that expands upward (a paste box) instead of
        # collapsing every newline to a ↵ marker on a single row.
        if self._box_geometry() is not None and "\n" in buffer:
            return self._draw_box(prompt, buffer, cursor, drawn)
        if self._box_active:
            self._leave_box(clear_prompt=False)

        # If fixed_row moved (resize), clear old prompt row that is now inside content
        if self.fixed_row is not None and self._prev_fixed_row is not None and self.fixed_row != self._prev_fixed_row:
            out.write(f"\033[{self._prev_fixed_row};1H\033[2K")
            # Also clear old popup rows at their previous absolute positions
            if drawn > 0 and self.popup_above:
                for i in range(drawn):
                    # Old popup ended at old_fixed_row-2 (content_bottom), not -1
                    row = self._prev_fixed_row - i - 2
                    if row >= 1:
                        out.write(f"\033[{row};1H\033[2K")
        self._prev_fixed_row = self.fixed_row

        # Ensure cursor is on the prompt row before any clearing,
        # preventing a flash in the content area.
        if self.popup_above and self.fixed_row is not None:
            out.write(f"\033[{self.fixed_row};1H")

        # Wrap all drawing in synchronized output to prevent flicker.
        out.write("\033[?2026h")
        try:
            # Step 1: Clear old popup rows. Use absolute rows when a fixed
            # bottom prompt is active (no newlines inside scroll region), else
            # use relative moves. Always clear the prompt line first.
            # Popup occupies rows fixed_row-2 .. fixed_row-n-1 (content_bottom
            # is fixed_row-2, border is fixed_row-1, prompt is fixed_row).
            if drawn > 0 and self.fixed_row is not None and self.popup_above:
                for i in range(drawn):
                    row = self.fixed_row - i - 2
                    if row >= 1:
                        out.write(f"\033[{row};1H\033[2K")
                out.write("\r\033[K")
            else:
                out.write("\r\033[K")
                if self.popup_above and drawn:
                    for _ in range(drawn):
                        out.write("\033[1A\r\033[K")
                elif drawn:
                    for _ in range(drawn):
                        out.write("\033[1B\r\033[K")
                    for _ in range(drawn):
                        out.write("\033[1A")

            # Step 2: Restore content rows that were corrupted by popup.
            if drawn > 0 and self.on_before_draw is not None:
                try:
                    self.on_before_draw(drawn)
                except Exception:
                    pass

            # Step 3: Write the prompt (contains absolute positioning).
            out.write(prompt)

            # Step 4: Clip buffer to prevent bottom-row wrapping.
            try:
                cols, _ = term_size()
            except Exception:
                cols = 80

            # The boxed bottom prompt reserves its right wall column, so
            # text never escapes the box (the wall is re-emitted below).
            box_wall = self._box_wall()
            wall_vis = 1 if box_wall is not None else 0

            # For display, render Shift+Enter newline as ↵ so prompt stays single row
            display_buffer = buffer.replace("\n", " ↵ ")
            display_cursor = cursor + buffer[:cursor].count("\n") * 2
            pvis = visible_len(prompt)
            space = max(0, cols - pvis - wall_vis)

            # A single very wide line (no newlines) is horizontally
            # clipped; reserve a right-edge chip reporting its size so a
            # huge one-line paste is never silently truncated. Fitting
            # and clipping are measured in visible columns, not Python
            # characters, so wide (CJK) input cannot overflow the box.
            wide_chip = ""
            if space <= 0:
                shown = ""
                cpos = 0
                # start must always be bound: the selection highlight below
                # indexes with it even in this clipped state.
                start = 0
            elif visible_len(display_buffer) <= space:
                shown = display_buffer
                cpos = display_cursor
                start = 0
            else:
                if self.popup_above and self.fixed_row is not None:
                    wide_chip = _size_chip(buffer)
                    chip_vis = visible_len(wide_chip)
                    avail = max(0, space - chip_vis)
                    start, cpos, shown = _line_window(display_buffer, display_cursor, avail)
                else:
                    start, cpos, shown = _line_window(display_buffer, display_cursor, space)

            # Highlight selection while dragging. ``start`` (from
            # _line_window) is the char offset where the window begins;
            # shown is a contiguous slice of display_buffer from that
            # offset, so char offsets within it are exact even when wide
            # characters are present.
            if self._sel_active and self._sel_anchor is not None and self._sel_end is not None:
                s_disp = min(self._sel_anchor, self._sel_end) + buffer[:min(self._sel_anchor, self._sel_end)].count("\n") * 2
                e_disp = max(self._sel_anchor, self._sel_end) + buffer[:max(self._sel_anchor, self._sel_end)].count("\n") * 2
                s = s_disp
                e = e_disp
                if s != e:
                    if visible_len(display_buffer) > space:
                        sel_s = max(0, s - start)
                        sel_e = max(0, min(len(shown), e - start))
                        if sel_s < sel_e:
                            shown = shown[:sel_s] + "\033[7m" + shown[sel_s:sel_e] + "\033[0m" + shown[sel_e:]
                    else:
                        shown = shown[:s] + "\033[7m" + shown[s:e] + "\033[0m" + shown[e:]

            safe_write(shown)
            if wide_chip:
                # Right-edge chip on the same row (ends flush at the last
                # column, just before the box wall), so the text window +
                # chip never wrap.
                col = pvis + space - visible_len(wide_chip)
                safe_write(f"\033[{col + 1}G" + self.style.dim(wide_chip))

            # Close the box: re-emit the right wall on the prompt row so
            # each keystroke leaves the box's right edge intact.
            if box_wall is not None:
                out.write(f"\033[{cols}G" + box_wall)

            # Step 5: Draw popup above the prompt.
            rows: list[str] = []
            if popup:
                for index in range(min(self.max_popup, len(popup.items))):
                    label = popup.label(index)
                    if index == selected:
                        # Crimson like the menu's selected row, so the
                        # completion popup speaks the same "picked" language
                        # as every other picker in the console.
                        rows.append(f"  {self.style.selected('> ' + label)}")
                    else:
                        rows.append(f"    {self.style.dim(label)}")

                if len(popup.items) > self.max_popup:
                    rows.append(self.style.dim(f"    ... {len(popup.items) - self.max_popup} more"))

                if self.hint:
                    rows.append(self.style.dim("    " + self.hint))

            if rows and self.popup_above:
                n = len(rows)
                if self.fixed_row is not None:
                    # Clamp popup so it never overlaps info border (row 2) or
                    # border row (fixed_row-1). Available = fixed_row-4
                    # (content height). Uses fixed_row-2 as content_bottom.
                    max_popup_rows = max(0, self.fixed_row - 4)
                    # Also respect max_popup already limited by repl to content height
                    max_popup_rows = min(max_popup_rows, self.max_popup) if self.max_popup else max_popup_rows
                    if n > max_popup_rows:
                        rows = rows[-max_popup_rows:]
                        n = len(rows)
                    # Anchor popup to content_bottom (fixed_row-2), not border
                    for i, row_text in enumerate(rows):
                        row = self.fixed_row - 2 - n + 1 + i
                        if row >= 1:
                            safe_write(f"\033[{row};1H\033[2K{row_text}")
                    out.write(f"\033[{self.fixed_row};1H")   # absolute: move cursor to prompt row
                else:
                    out.write(f"\033[{n}A")
                    for i, row_text in enumerate(rows):
                        safe_write("\r\033[2K" + row_text)
                        if i < n - 1:
                            out.write("\n")
                    out.write("\033[1B")

            elif rows:
                for row in rows:
                    out.write("\n\033[K")
                    safe_write(row)
                out.write(f"\033[{len(rows)}A")

            # Step 6: Position cursor at the typed text position.
            out.write("\r")
            remaining = pvis + cpos
            if remaining:
                out.write(f"\033[{remaining}C")

        finally:
            out.write("\033[?2026l")
            out.flush()

        return len(rows)

    def _finish(self, drawn: int, skip_newline: bool = False) -> None:
        out = sys.stdout

        # A multi-line box owns its rows and restores content/chrome itself.
        if self._box_active:
            self._leave_box(clear_prompt=True)
            drawn = 0

        if drawn:
            if self.popup_above and self.fixed_row is not None:
                for i in range(drawn):
                    row = self.fixed_row - i - 2
                    if row >= 1:
                        out.write(f"\033[{row};1H\033[2K")
                out.write(f"\033[{self.fixed_row};1H")
            elif self.popup_above:
                for _ in range(drawn):
                    out.write("\033[1A\r\033[K")
                if drawn:
                    out.write(f"\033[{drawn}B")
                out.write("\r")
            else:
                for _ in range(drawn):
                    out.write("\033[1B\r\033[K")
                out.write(f"\033[{drawn}A")
                out.write("\r")

        # Restore host content after popup dismissal.
        if self.popup_above and self.on_before_draw is not None and drawn > 0:
            try:
                self.on_before_draw(drawn)
            except Exception:
                pass

        if not skip_newline:
            out.write("\n")

        out.flush()

    # ── key input ─────────────────────────────────────────────

    @contextmanager
    def _raw_mode(self):
        if os.name == "nt":
            # Enable VT input processing for the duration of the read so
            # conhost / Windows Terminal wrap pastes in ESC[200~..ESC[201~
            # (bracketed paste) now that ``read()`` requested it with
            # ESC[?2004h. Without ENABLE_VIRTUAL_TERMINAL_INPUT, pasted
            # text is delivered as raw key events where every newline is
            # a \r that lands on the submit key - the paste "submits
            # itself" line by line instead of entering the buffer.
            #
            # Only the VT-input bit is OR-ed in: ENABLE_QUICK_EDIT_MODE
            # (0x0040) and every other flag stay untouched, so native
            # text selection over the transcript keeps working while the
            # prompt is open. The original mode is restored on exit.
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.windll.kernel32
            handle = kernel32.GetStdHandle(-10)  # STD_INPUT_HANDLE
            original = None
            try:
                mode = wintypes.DWORD()
                if (
                    handle not in (None, 0, -1)
                    and kernel32.GetConsoleMode(handle, ctypes.byref(mode))
                ):
                    original = mode.value
                    if not original & 0x0200:  # ENABLE_VIRTUAL_TERMINAL_INPUT
                        kernel32.SetConsoleMode(handle, original | 0x0200)
            except Exception:
                original = None
            try:
                yield
            finally:
                if original is not None:
                    try:
                        kernel32.SetConsoleMode(handle, original)
                    except Exception:
                        pass
            return
        import termios
        import tty

        fd = sys.stdin.fileno()
        original = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            yield
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, original)

    def _read_key(self, stop=None, timeout: float | None = None) -> str | None:
        """Read one key, or None on timeout/stop.

        ``stop`` is a threading.Event consulted while waiting; ``timeout``
        bounds the wait. The plain prompt loop calls this with no
        arguments (wait forever); the turn-scoped scroll reader passes a
        stop event and a short timeout so its thread can end promptly when
        the task does, without stealing the user's keys.
        """
        # Deliver keys buffered during a streaming turn first, so typing
        # that happened while the agent worked is not lost, and so a
        # non-scroll key swallowed by the scroll reader reaches the prompt.
        if self.preload:
            return self.preload.pop(0)
        if os.name == "nt":
            import msvcrt
            import time

            while not msvcrt.kbhit():
                if stop is not None and stop.is_set():
                    return None
                if timeout is not None:
                    timeout -= 0.02
                    if timeout <= 0:
                        return None
                # Throttle size poll to avoid per-loop overhead.
                try:
                    now = time.monotonic()
                    if now - getattr(self, "_last_size_poll", 0) >= 0.2:
                        self._last_size_poll = now
                        cols, rows = term_size()
                        if (self._term_cols is None or self._term_rows is None):
                            self._term_cols, self._term_rows = cols, rows
                        elif cols != self._term_cols or rows != self._term_rows:
                            self._term_cols, self._term_rows = cols, rows
                            return KEY_RESIZE
                except Exception:
                    pass
                time.sleep(0.02)

            char = msvcrt.getwch()
            if char in ("\x00", "\xe0"):
                # Extended-key prefix; s/t are the magic Ctrl+Left/Ctrl+Right
                # second bytes, the rest resolve through the specials table.
                second = msvcrt.getwch()
                if second == "s":
                    return KEY_CTRL_LEFT
                if second == "t":
                    return KEY_CTRL_RIGHT
                return _WINDOWS_SPECIALS.get(second, char)
            if char == "\r" and _is_shift_pressed():
                return KEY_SHIFT_ENTER
            if char == "\x1b":
                if self._input_pending():
                    # Read the sequence through msvcrt, never sys.stdin:
                    # mixing the two on this platform stalls or drops bytes.
                    seq = msvcrt.getwch()
                    if seq != "[":
                        return self._finish_esc(seq)
                    # Collect the CSI (everything up to its terminating
                    # letter or ~) so bracketed-paste start (200~) is
                    # recognized instead of being swallowed by the generic
                    # ESC table lookup - that used to drop the brackets and
                    # let every newline inside a paste hit the submit key.
                    buf = ""
                    waited = 0
                    while True:
                        if not self._input_pending():
                            waited += 1
                            if waited > 10:  # ~50ms grace for the rest of the burst
                                break
                            time.sleep(0.005)
                            continue
                        waited = 0
                        ch = msvcrt.getwch()
                        if ch == "<":
                            return self._read_sgr_mouse(msvcrt.getwch)
                        buf += ch
                        if ch == "~" or ch.isalpha() or len(buf) > 16:
                            break
                    if buf == "200~":
                        pasted = _assemble_bracketed_paste(
                            msvcrt.getwch, lambda: msvcrt.kbhit()
                        )
                        return "\x1b[200~" + pasted + "\x1b[201~"
                    # Same CSI mapping as the POSIX path: arrows, ctrl
                    # arrows, page keys, and the generic specials table.
                    if buf == "1;5A":
                        return KEY_UP
                    if buf == "1;5B":
                        return KEY_DOWN
                    if buf == "1;5C":
                        return KEY_CTRL_RIGHT
                    if buf == "1;5D":
                        return KEY_CTRL_LEFT
                    if buf in ("13;2u", "13u"):
                        return KEY_SHIFT_ENTER
                    if buf in _POSIX_SPECIALS:
                        return _POSIX_SPECIALS[buf]
                    if buf and buf[0] in _POSIX_SPECIALS:
                        return _POSIX_SPECIALS[buf[0]]
                    return "\x1b"
            return char

        import select
        import time as _time
        while True:
            try:
                ready, _, _ = select.select([sys.stdin], [], [], 0.02)
            except (OSError, ValueError):
                ready = [sys.stdin]

            if stop is not None and stop.is_set():
                return None
            if timeout is not None:
                timeout -= 0.02
                if timeout <= 0:
                    return None

            try:
                now = _time.monotonic()
                if now - getattr(self, "_last_size_poll", 0) >= 0.2:
                    self._last_size_poll = now
                    cols, rows = term_size()
                    if (self._term_cols is None or self._term_rows is None):
                        self._term_cols, self._term_rows = cols, rows
                    elif cols != self._term_cols or rows != self._term_rows:
                        self._term_cols, self._term_rows = cols, rows
                        return KEY_RESIZE
            except Exception:
                pass

            if ready:
                break

        char = sys.stdin.read(1)
        if char != "\x1b":
            if char in ("\r", "\n") and _is_shift_pressed():
                return KEY_SHIFT_ENTER
            return char
        if not self._await_more_input():
            return char
        seq = sys.stdin.read(1)
        if seq == "[":
            # Check for bracketed paste start 200~
            # Peek ahead
            if self._await_more_input():
                # Try to read rest of CSI; an SGR mouse report ("<b;c;rm")
                # has its own terminator and length, so it is collected
                # separately instead of through the 6-char CSI limit.
                buf = ""
                sgr = False
                while True:
                    if not self._await_more_input():
                        break
                    ch = sys.stdin.read(1)
                    buf += ch
                    if not sgr and buf == "<":
                        sgr = True
                    if sgr:
                        if ch in ("M", "m") or len(buf) > 32:
                            break
                        continue
                    if ch.isalpha() or ch == "~":
                        break
                    if len(buf) > 6:
                        break
                if sgr:
                    return _parse_sgr_body(buf)
                full = seq + buf
                if full == "[200~":
                    # Bracketed paste start — collect the body whole so a
                    # big multi-line paste is inserted as text and never
                    # leaks stray newlines into the submit path.
                    pasted = _assemble_bracketed_paste(
                        lambda: sys.stdin.read(1), lambda: self._input_pending()
                    )
                    return "\x1b[200~" + pasted + "\x1b[201~"
                if full == "[1;5A":
                    return KEY_UP
                if full == "[1;5B":
                    return KEY_DOWN
                if full == "[1;5C":
                    return KEY_CTRL_RIGHT
                if full == "[1;5D":
                    return KEY_CTRL_LEFT
                if full in ("[13;2u", "[13u"):
                    return KEY_SHIFT_ENTER
                if buf == "<":
                    return "\x1b"
                # Fallback to table
                if buf in _POSIX_SPECIALS:
                    return _POSIX_SPECIALS[buf]
                if buf and buf[0] in _POSIX_SPECIALS:
                    return _POSIX_SPECIALS[buf[0]]
                return char
            if not self._await_more_input():
                # Nothing followed ESC <seq> within the grace window: give
                # the Escape back instead of blocking on a read that may
                # never complete.
                return char
            buf = sys.stdin.read(1)
            if buf == "<":
                return "\x1b"
            # "3"/"5"/"6" start legacy sequences (e.g. ESC 3 ~): read the
            # completing char instead of treating the digit as a literal.
            rest = buf
            if buf in "356" and self._await_more_input():
                rest += sys.stdin.read(1)
            return _POSIX_SPECIALS.get(rest, char)
        return char

    def _read_sgr_mouse(self, read_one) -> str:
        """Collect the rest of an SGR mouse report and parse it.

        Called after ESC [ < has been consumed; ``read_one`` yields one
        character from the terminal. Returns the MouseEvent, or ESC when
        the report is malformed so the popup is not disturbed.

        Every read is gated on ``_input_pending`` so a partial or
        interrupted report can never block the editor: if the terminal
        stops mid-sequence, we give up after the pending check fails.
        """
        body = ""
        for _ in range(32):
            if not self._input_pending():
                break
            ch = read_one()
            if not ch:
                break
            body += ch
            if ch in ("M", "m"):
                break
        return _parse_sgr_body(body)

    def _finish_esc(self, prefix: str) -> str:
        rest = prefix
        # Bounded: only consume characters that are actually pending, so a
        # lone ESC or a truncated sequence returns instead of blocking the
        # editor forever waiting for a completion that never arrives. A
        # short grace period (_await_more_input) covers the case where the
        # sequence's later bytes simply haven't landed yet.
        for _ in range(8):
            if not self._await_more_input():
                break
            ch = sys.stdin.read(1)
            if not ch:
                break
            rest += ch
            if ch.isalpha() or ch == "~":
                break
        inner = rest[1:]
        return _POSIX_SPECIALS.get(inner[:2], "\x1b")

    def _await_more_input(self, max_wait: float = 0.05, poll: float = 0.005) -> bool:
        """True if more bytes show up within a short grace window.

        Escape sequences can arrive split across reads; waiting briefly
        prevents a delayed arrow or paste wrapper from being read as a
        lone Escape or dropped entirely.
        """
        if self._input_pending():
            return True
        import time

        waited = 0.0
        while waited < max_wait:
            time.sleep(poll)
            waited += poll
            if self._input_pending():
                return True
        return False

    def _input_pending(self) -> bool:
        if os.name == "nt":
            try:
                import msvcrt
                return msvcrt.kbhit()
            except Exception:
                return False
        try:
            import select
        except ImportError:
            return True
        try:
            return bool(select.select([sys.stdin], [], [], 0)[0])
        except Exception:
            return True


def make_reader(editor: LineEditor) -> Callable[[str], str]:
    """Adapt the editor to the ``prompt -> line`` signature the REPL wants."""
    return editor.read