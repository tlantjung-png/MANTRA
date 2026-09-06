"""Transcript: the styled conversation model and its viewport.

Content is a list of styled logical lines (ANSI SGR only — cursor
movement is stripped at ingest). Wrapping into display rows is cached per
accepted width and recomputed lazily. The viewport shows a window over
the display rows: while following, the newest rows are shown; scrolling
up detaches and an explicit jump (or a new submission) reattaches.
"""

from __future__ import annotations

import re
import threading

from core.term import _char_width

# Word-wrap for styled text: escape sequences carry no width, are never
# split, and open SGR codes are re-emitted on continuation rows. Wide
# (CJK) characters count double.
_ANSI_RE = re.compile(r"\033\[[0-9;?]*[ -/]*[@-~]")

# Bare C0/C1 controls that survive the ANSI filter (ESC c, BEL, BS, C1
# CSI bytes, ...) can reset or corrupt the terminal frame. Layout
# controls the transcript actually renders (\n \r \t) are not matched.
# NOTE: \x1b (ESC) is deliberately inside the class: it must be removed
# when it is NOT part of a kept SGR sequence. sanitize_ingest shields
# the ESC byte of kept sequences before this strip runs.
_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")

# Printable stand-in for the ESC byte of a kept SGR sequence while the
# control-char strip runs; restored afterwards.
_SGR_SHIELD = "_SGRESC_"


def sanitize_ingest(text: str) -> str:
    """Strip every escape sequence except SGR color/weight.

    Transcript lines are data, never cursor control: if a positioning or
    mouse sequence ever leaks into a print path, it must not be able to
    relocate the frame or replay itself on screen.
    """

    def _keep(m: "re.Match[str]") -> str:
        # Keep SGR sequences whole, every other escape is dropped. The
        # ESC byte is shielded so the strip below cannot eat it and
        # leave literal "[2m" codes behind (which would render as text
        # and never colour the line).
        return _SGR_SHIELD + m.group(0)[1:] if m.group(0).endswith("m") else ""

    # Strip every escape except SGR, drop bare C0/C1 controls (including
    # the ESC byte of dropped sequences), then restore the shielded ESC
    # bytes so the stored line carries real, parseable SGR.
    shielded = _ANSI_RE.sub(_keep, text)
    stripped = _CTRL_RE.sub("", shielded)
    return stripped.replace(_SGR_SHIELD + "[", "\x1b[")


def wrap_ansi(text: str, width: int) -> list[str]:
    if width <= 0:
        return [text]
    plain = _ANSI_RE.sub("", text)
    if sum(_char_width(c) for c in plain) <= width:
        return [text]
    n = len(text)
    i = 0
    chunks: list[tuple[bool, list[str]]] = []
    cur: list[str] = []
    cur_is_space = False

    def close_chunk() -> None:
        nonlocal cur
        if cur:
            chunks.append((cur_is_space, list(cur)))
            cur = []

    while i < n:
        m = _ANSI_RE.match(text, i)
        if m:
            cur.append(m.group(0))
            i = m.end()
            continue
        ch = text[i]
        is_space = ch in " \t"
        if not cur:
            cur_is_space = is_space
        elif is_space != cur_is_space:
            close_chunk()
            cur_is_space = is_space
        cur.append(ch)
        i += 1
    close_chunk()

    out: list[str] = []
    seg: list[str] = []
    seg_w = 0
    open_codes: list[str] = []

    def flush_seg() -> None:
        nonlocal seg, seg_w
        if not seg:
            return
        if open_codes:
            seg.append("\x1b[0m")
        out.append("".join(seg))
        seg = []
        seg_w = 0
        open_codes.clear()

    def add_tok(tok: str) -> None:
        nonlocal seg_w
        seg.append(tok)
        if tok == "\x1b[0m":
            open_codes.clear()
        elif tok.startswith("\x1b[") and tok.endswith("m"):
            open_codes.append(tok)
        elif not tok.startswith("\x1b"):
            seg_w += _visible(tok)

    def _visible(s: str) -> int:
        total = 0
        j = 0
        while j < len(s):
            m = _ANSI_RE.match(s, j)
            if m:
                j = m.end()
                continue
            total += _char_width(s[j])
            j += 1
        return total

    for is_space, chunk in chunks:
        if is_space:
            for tok in chunk:
                if tok.startswith("\x1b"):
                    add_tok(tok)
                else:
                    if seg_w >= width:
                        break
                    seg.append(tok)
                    seg_w += _char_width(tok)
            continue
        word_w = 0
        for tok in chunk:
            if not tok.startswith("\x1b"):
                word_w += _char_width(tok)
        if word_w == 0:
            continue
        if word_w <= width:
            if seg_w and seg_w + word_w > width:
                flush_seg()
            for tok in chunk:
                add_tok(tok)
            continue
        if seg_w:
            flush_seg()
        for tok in chunk:
            if tok.startswith("\x1b"):
                add_tok(tok)
                continue
            if seg_w + _char_width(tok) > width:
                carry = list(open_codes)
                flush_seg()
                for code in carry:
                    add_tok(code)
            add_tok(tok)
    flush_seg()
    return out if out else [""]

_MAX_LINES = 8000


class Transcript:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.raw: list[str] = []          # styled logical lines
        self.display: list[str] = []      # wrapped display rows (styled)
        self.partial = ""                 # live, unterminated tail (styled)
        self._width = 0
        self.offset = 0                   # display rows from the bottom
        self.follow = True
        self.viewport_height = 30         # kept current by the app each frame
        self.version = 0                  # bumped on every content change

    # ── content ───────────────────────────────────────────────

    def append(self, styled_line: str) -> None:
        with self._lock:
            clean = sanitize_ingest(styled_line)
            self.raw.append(clean)
            if len(self.raw) > _MAX_LINES:
                self.raw = self.raw[-_MAX_LINES:]
                self._rewrap_locked()
            else:
                if self._width:
                    self.display.extend(wrap_ansi(clean, self._width))
            self.version += 1

    def append_partial(self, styled_text: str) -> None:
        """Feed a live fragment: complete lines commit, the tail is held."""
        with self._lock:
            data = (self.partial + styled_text).replace("\r", "")
            parts = data.split("\n")
            self.partial = parts.pop()
            for line in parts:
                self.append(line)

    def flush_partial(self) -> None:
        with self._lock:
            if self.partial:
                pending = self.partial
                self.partial = ""
                self.append(pending)

    def clear(self) -> None:
        with self._lock:
            self.raw = []
            self.display = []
            self.partial = ""
            self.offset = 0
            self.follow = True
            self.version += 1

    def set_width(self, cols: int) -> None:
        with self._lock:
            if self._width == cols:
                return
            self._width = cols
            self._rewrap_locked()

    def _rewrap_locked(self) -> None:
        self.display = []
        if not self._width:
            return
        for line in self.raw:
            self.display.extend(wrap_ansi(line, self._width))
        self.version += 1

    # ── scrolling ─────────────────────────────────────────────

    def visible_rows(self, height: int) -> list[str]:
        with self._lock:
            pool = self.display if self.offset > 0 else self.display + (
                wrap_ansi(self.partial, self._width) if (self.partial and self._width) else []
            )
            if height <= 0:
                return []
            if self.offset > 0:
                end = max(0, len(pool) - self.offset)
                start = max(0, end - height)
                return pool[start:end]
            return pool[-height:]

    def scroll_up(self, amount: int = 3) -> None:
        with self._lock:
            total = len(self.display) + (
                len(wrap_ansi(self.partial, self._width)) if (self.partial and self._width) else 0
            )
            # Detach only as far as there is content above the viewport:
            # when everything already fits, scrolling must be a no-op.
            max_offset = max(0, total - self.viewport_height)
            new = min(max_offset, self.offset + amount)
            if new != self.offset:
                self.offset = new
                self.follow = False
                self.version += 1

    def scroll_down(self, amount: int = 3) -> None:
        with self._lock:
            new = max(0, self.offset - amount)
            if new != self.offset:
                self.offset = new
                if self.offset == 0:
                    self.follow = True
                self.version += 1

    def jump_bottom(self) -> None:
        with self._lock:
            self.offset = 0
            self.follow = True
            self.version += 1

    @property
    def scrolled(self) -> int:
        return self.offset

    # ── selection support ─────────────────────────────────────

    def row_source(self, height: int) -> list[tuple[int, str]]:
        """(display-row-index, styled-text) for the currently visible rows."""
        with self._lock:
            if self.offset > 0:
                end = max(0, len(self.display) - self.offset)
                start = max(0, end - height)
                return [(i, self.display[i]) for i in range(start, end)]
            pool = self.display + (
                wrap_ansi(self.partial, self._width) if (self.partial and self._width) else []
            )
            start = max(0, len(pool) - height)
            return [(i, pool[i]) for i in range(start, len(pool))]

    def row_text(self, display_index: int) -> str:
        with self._lock:
            if 0 <= display_index < len(self.display):
                return self.display[display_index]
            return ""
