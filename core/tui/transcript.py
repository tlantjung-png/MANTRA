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

from core.term import _WidthScanner, _char_width

# Word-wrap for styled text: escape sequences carry no width, are never
# split, and open SGR codes are re-emitted on continuation rows. Wide
# (CJK) characters count double.
_ANSI_RE = re.compile(r"\033\[[0-9;?]*[ -/]*[@-~]")

# Bare C0/C1 controls (ESC c, BEL, BS, C1 CSI bytes, ...) can reset or
# corrupt the terminal frame. \n is handled by line-splitting at ingest;
# \t is expanded to spaces here so a tab stop can never reach the
# terminal (see D1).
_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0e-\x1f\x7f-\x9f]")


def sanitize_ingest(text: str) -> str:
    """Strip every escape sequence except SGR color/weight.

    Transcript lines are data, never cursor control: if a positioning or
    mouse sequence ever leaks into a print path, it must not be able to
    relocate the frame or replay itself on screen.

    Single pass, no sentinel: SGR sequences are copied verbatim (their
    control bytes are exempt from the strip), everything else — other
    escapes, bare C0/C1 controls, and CR — is dropped. Because kept
    sequences never pass through a printable stand-in, model output
    cannot forge one.
    """
    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "\x1b":
            m = _ANSI_RE.match(text, i)
            if m and m.group(0).endswith("m"):
                out.append(m.group(0))  # SGR: kept whole
                i = m.end()
            else:
                # Any other escape is dropped; skip its full body when
                # parseable so trailing parameter bytes cannot leak.
                i = m.end() if m else i + 1
            continue
        if ch == "\t":
            out.append("    ")  # expand tabs: a raw tab stop must never paint
            i += 1
            continue
        if ch == "\r" or _CTRL_RE.match(ch):
            i += 1  # carriage returns and bare C0/C1 controls are never layout
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def wrap_ansi(text: str, width: int) -> list[str]:
    if width <= 0:
        return [text]
    plain = _ANSI_RE.sub("", text)
    quick = _WidthScanner()
    if sum(quick.feed(c) for c in plain) <= width:
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
    # One scanner per wrap call: a ZWJ at the end of one token must count
    # as a continuation at the start of the next, so the state is shared
    # across every token laid out below but never leaks to other strings.
    scanner = _WidthScanner()

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
            total += scanner.feed(s[j])
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
                    seg_w += scanner.feed(tok)
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
_PARTIAL_CAP = 4096  # longest live tail held for an unterminated line (D23)


class Transcript:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.raw: list[str] = []          # styled logical lines
        self.display: list[str] = []      # wrapped display rows (styled)
        self.partial = ""                 # live, unterminated tail (styled)
        self._partial_rows: list[str] = []  # wrapped tail rows, cached per width
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
            self.partial = parts.pop()[-_PARTIAL_CAP:]
            self._cache_partial_locked()
            for line in parts:
                self.append(line)

    def flush_partial(self) -> None:
        with self._lock:
            if self.partial:
                pending = self.partial
                self.partial = ""
                self._partial_rows = []
                self.append(pending)

    def clear(self) -> None:
        with self._lock:
            self.raw = []
            self.display = []
            self.partial = ""
            self._partial_rows = []
            self.offset = 0
            self.follow = True
            self.version += 1

    def set_width(self, cols: int) -> None:
        with self._lock:
            if self._width == cols:
                return
            self._width = cols
            self._rewrap_locked()

    def _cache_partial_locked(self) -> None:
        """Recompute the wrapped tail rows (cached per width, see D23)."""
        self._partial_rows = (
            wrap_ansi(self.partial, self._width) if (self.partial and self._width) else []
        )

    def _clamp_offset_locked(self) -> None:
        """Pin offset to the display pool after rewrap/truncation (D4)."""
        total = len(self.display) + len(self._partial_rows)
        max_offset = max(0, total - self.viewport_height)
        if self.offset > max_offset:
            self.offset = max_offset

    def _rewrap_locked(self) -> None:
        self.display = []
        if not self._width:
            return
        for line in self.raw:
            self.display.extend(wrap_ansi(line, self._width))
        self._cache_partial_locked()
        self._clamp_offset_locked()
        self.version += 1

    # ── scrolling ─────────────────────────────────────────────

    def visible_rows(self, height: int) -> list[str]:
        with self._lock:
            pool = self.display if self.offset > 0 else self.display + self._partial_rows
            if height <= 0:
                return []
            if self.offset > 0:
                end = max(0, len(pool) - self.offset)
                start = max(0, end - height)
                return pool[start:end]
            return pool[-height:]

    def scroll_up(self, amount: int = 3) -> None:
        with self._lock:
            total = len(self.display) + len(self._partial_rows)
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
            pool = self.display + self._partial_rows
            start = max(0, len(pool) - height)
            return [(i, pool[i]) for i in range(start, len(pool))]

    def row_text(self, display_index: int) -> str:
        with self._lock:
            if 0 <= display_index < len(self.display):
                return self.display[display_index]
            # The wrapped partial tail is visible while following; resolve
            # it too so a drag ending there still copies (see D9/D11).
            rel = display_index - len(self.display)
            if 0 <= rel < len(self._partial_rows):
                return self._partial_rows[rel]
            return ""
