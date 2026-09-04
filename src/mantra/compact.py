"""Compact TUI: viewport buffer, border row, fixed prompt."""

from __future__ import annotations

import re
import sys
import threading
import time
from typing import Any

from mantra.term import term_size as _term_size
from mantra.term import visible_len as _vis

_ANSI_RE = re.compile(r"\033\[[0-9;?]*[ -/]*[@-~]")

_UI_LOCK = threading.RLock()


def _ansi(code: str, text: str, enabled: bool = True) -> str:
    return f"\033[{code}m{text}\033[0m" if enabled else text


from mantra import theme

# Blood & Bone chrome helpers — monochrome stone, one crimson accent.
def _ash(t, e=True): return _ansi(theme.ASH, t, e)            # labels, secondary
def _faint(t, e=True): return _ansi(theme.FAINT, t, e)        # hints, markers
def _hair(t, e=True): return _ansi(theme.HAIR, t, e)          # borders, walls, rules
def _blood_bold(t, e=True): return _ansi(theme.BLOOD_BOLD, t, e)  # wordmark accent


def _pad(text: str, width: int) -> str:
    diff = width - _vis(text)
    return text + (" " * max(0, diff))


def _shorten(text: str, width: int) -> str:
    if width <= 0:
        return ""
    if _vis(text) <= width:
        return text
    if width <= 3:
        return "." * width
    result: list[str] = []
    vis = 0
    limit = width - 3  # reserve room for the 3-column "..." suffix
    i = 0
    while i < len(text):
        m = _ANSI_RE.match(text, i)
        if m:
            result.append(m.group(0))
            i = m.end()
            continue
        ch = text[i]
        w = _vis(ch)
        if vis + w > limit:
            break
        result.append(ch)
        vis += w
        i += 1
    result.append("...")
    return "".join(result)


def _fit_line(text: str, width: int) -> str:
    if width <= 0:
        return ""
    if _vis(text) <= width:
        return text
    return _shorten(text, width)


def _wrap_ansi(text: str, width: int) -> list[str]:
    """Word-wrap styled text to ``width`` columns.

    ANSI escape sequences carry no visible width, so they are never
    counted and never split. When a wrap lands inside a coloured word
    (a long URL, an unbroken code token), the open SGR codes are
    re-emitted on the continuation row so the colour survives the wrap.
    Wide (CJK) characters count double. Used on every committed line so
    long tool output — markdown tables, diffs, audit tables — wraps into
    readable screen rows instead of being truncated at the right edge.
    """
    if width <= 0:
        return [text]
    if _vis(text) <= width:
        return [text]

    n = len(text)
    i = 0
    # Split into words and whitespace runs; escape sequences attach to
    # the surrounding word so styling is never split from its text.
    chunks: list[tuple[bool, list[str]]] = []
    cur: list[str] = []
    cur_is_space = False

    def close_chunk() -> None:
        if cur:
            chunks.append((cur_is_space, list(cur)))
            cur.clear()

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

    # Lay chunks out, wrapping at word boundaries.
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
            seg_w += _vis(tok)

    for is_space, chunk in chunks:
        if is_space:
            # Whitespace runs become the wrap point: drop at a line start.
            for tok in chunk:
                if tok.startswith("\x1b"):
                    add_tok(tok)
                else:
                    if seg_w >= width:
                        break
                    seg.append(tok)
                    seg_w += _vis(tok)
            continue
        word_w = 0
        for tok in chunk:
            if not tok.startswith("\x1b"):
                word_w += _vis(tok)
        if word_w == 0:
            continue
        if word_w <= width:
            if seg_w and seg_w + word_w > width:
                flush_seg()
            for tok in chunk:
                add_tok(tok)
            continue
        # One word wider than the screen: split it on visible characters,
        # carrying the colour across the row boundary.
        if seg_w:
            flush_seg()
        for tok in chunk:
            if tok.startswith("\x1b"):
                add_tok(tok)
                continue
            cw = _vis(tok)
            if seg_w > 0 and seg_w + cw > width:
                carry = list(open_codes)
                flush_seg()
                for code in carry:
                    add_tok(code)
            add_tok(tok)
    flush_seg()
    return out if out else [""]


def _version(session: Any) -> str:
    try:
        from mantra import __version__  # type: ignore
        return __version__
    except Exception:
        return "0.1.0"


def _safe_write(text: str) -> None:
    out = sys.stdout
    try:
        out.write(text)
    except UnicodeEncodeError:
        enc = getattr(out, "encoding", None) or "utf-8"
        out.write(text.encode(enc, errors="replace").decode(enc, errors="replace"))


def render_card(session: Any, width: int | None = None, enabled: bool = True) -> list[str]:
    """Centered text card — no container, truly compact."""
    cols, _ = _term_size()
    if width is None:
        width = max(30, cols - 8)
        if width > 80:
            width = 80
    ver = _version(session)
    inner = width
    # M A N T R A wordmark in the muted-crimson accent; tagline and
    # version stay quiet below it so the splash reads as one calm mark.
    mantra_pulse = _blood_bold("M A N T R A", enabled)
    lines = []
    for raw in [
        mantra_pulse,
        _ash("Spells Matter", enabled),
        _faint(ver, enabled),
    ]:
        vis = _vis(raw)
        pad = max(0, (inner - vis) // 2)
        content = " " * pad + raw
        lines.append(content)
    out: list[str] = []
    for r in lines:
        if _vis(r) > inner:
            r = _shorten(r, inner)
        out.append(_pad(r, inner))
    return out


def bottom_status(session: Any, enabled: bool = True) -> str:
    llm = session.config.get("llm", {}) if hasattr(session, "config") else {}
    model = llm.get("model", "?")
    reasoning = llm.get("reasoning_effort") or "off"
    ws = getattr(getattr(session, "sandbox", None), "root", "")
    if ws:
        ws_short = ws.replace("\\", "/").rstrip("/").split("/")[-1]
    else:
        ws_short = ""
    if ws_short:
        core = f"{model} ({reasoning}) · {ws_short}"
    else:
        core = f"{model} ({reasoning})"
    return _faint(core, enabled)


# Legacy names (replaced by layout methods).
show_splash = None
hide_splash = None
draw_status = None


class CompactLayout:
    """
    Full-screen compact TUI with internal viewport.

    Screen model:
        content_top .. content_bottom   scrollable viewport
        border_row                      spinner / border / transient status
        prompt_row                      fixed prompt (last row)

    Content is stored in an internal viewport buffer. It is not written
    directly to the terminal as normal scrollback.
    """

    RESERVED_BOTTOM = 2  # border + prompt
    RESERVED_TOP = 2  # info bar + border
    MAX_LINES = 8000

    def __init__(self) -> None:
        self.active = False
        self._alt = False

        self._cols = 0
        self._rows = 0

        self.info_row = 1        # top info bar
        self.info_border_row = 1  # separator below info
        self.content_top = 1
        self.content_bottom = 1

        self.border_row = 1
        self.prompt_row = 1

        self._session: Any = None
        self._style: Any = None
        self._splash_visible = False
        # Called (with no args) right after a resize recalc, while the UI
        # lock is held, so the prompt renderer (the line editor) can sync
        # its geometry in the same step as the recalc. The layout is the
        # single owner of geometry; anything drawing the prompt only mirrors
        # it. None when no prompt renderer is attached.
        self.prompt_sync: Any = None

        # Internal viewport buffer. self.raw keeps the logical
        # (pre-wrap) lines so a terminal resize can re-wrap; self.lines
        # holds the wrapped display rows the viewport scrolls over.
        self.raw: list[str] = []
        self.lines: list[str] = []
        self.partial = ""
        self.offset = 0
        self._last_visible: list[str] | None = None
        self._last_render = 0.0

    # ── screen lifecycle ──────────────────────────────────────

    def enter(self) -> None:
        with _UI_LOCK:
            self._enter_locked()

    def _enter_locked(self) -> None:
        if not sys.stdout.isatty() or self._alt:
            return
        cols, rows = _term_size()
        if rows < 8 or cols < 30:
            return
        _safe_write("\033[?1049h")       # alternate screen
        _safe_write("\033[?7l")          # disable autowrap
        _safe_write("\033[r")            # reset scroll region
        _safe_write("\033[2J\033[H")     # clear screen
        sys.stdout.flush()
        self._alt = True

    def _leave_alt_locked(self) -> None:
        if not self._alt:
            return
        _safe_write("\033[r")
        _safe_write("\033[?7h")          # re-enable autowrap
        _safe_write("\033[?1049l")       # leave alternate screen
        sys.stdout.flush()
        self._alt = False

    def cleanup(self) -> None:
        try:
            self.stop_prompt_pulse()
        except Exception:
            pass
        with _UI_LOCK:
            if self.active:
                _safe_write("\033[r")
            self._leave_alt_locked()
            self.active = False

    # ── setup ─────────────────────────────────────────────────

    def setup(self, splash_rows: int, session: Any = None, style: Any = None) -> None:
        self._session = session
        self._style = style

        if not self._alt:
            self.enter()

        cols, rows = _term_size()

        with _UI_LOCK:
            self._recalc(cols, rows)

            if not self.active:
                self._leave_alt_locked()
                return

            self.raw = []
            self.lines = []
            self.partial = ""
            self.offset = 0

            self._draw_chrome_locked()
            self._render_content_locked()
            self._apply_region_locked()
            # Start walking pulse for prompt
            try:
                self.start_prompt_pulse()
            except Exception:
                pass

    # ── geometry ──────────────────────────────────────────────

    def _recalc(self, cols: int, rows: int) -> None:
        self._cols = cols
        self._rows = rows
        self.info_row = 1
        self.info_border_row = 2
        self.content_top = 1 + self.RESERVED_TOP
        self.border_row = max(1, rows - 1)  # second‑to‑last line
        self.prompt_row = max(1, rows)      # last line
        self.content_bottom = max(self.content_top, rows - self.RESERVED_BOTTOM)
        self.active = (
            rows >= 8
            and cols >= 30
            and self.content_bottom >= self.content_top
        )
        self._last_visible = None

    def _height_locked(self) -> int:
        return max(1, self.content_bottom - self.content_top + 1)

    def _apply_region_locked(self) -> None:
        if not self.active:
            return
        _safe_write(f"\033[{self.content_top};{self.content_bottom}r")
        _safe_write(f"\033[{self.content_bottom};1H")
        sys.stdout.flush()

    # ── viewport content ──────────────────────────────────────

    def _partial_rows_locked(self) -> list[str]:
        """Wrapped display rows of the live, not-yet-newline partial line."""
        if not self.partial:
            return []
        return _wrap_ansi(self.partial, self._cols)

    def _rows_locked(self) -> list[str]:
        """The display rows currently inside the viewport.

        Committed lines are stored pre-wrapped (self.lines), so scrolling
        walks display rows and wheel moves feel smooth even when one
        logical line — a markdown table row, a diff hunk, a long code
        line — spans several screen rows. The live partial line is
        wrapped on the fly and only shown while the viewport is pinned
        to the bottom, so a still-growing line never makes the offset
        arithmetic jump.
        """
        height = self._height_locked()
        committed = self.lines
        if self.offset > 0:
            end = len(committed) - self.offset
            start = max(0, end - height)
            return committed[start:end]
        pool = committed + self._partial_rows_locked()
        return pool[-height:]

    def _commit_lines_locked(self, raws: list[str]) -> None:
        """Append logical lines, storing their wrapped display rows."""
        self.raw.extend(raws)
        width = self._cols
        for raw in raws:
            self.lines.extend(_wrap_ansi(raw, width))
        if len(self.raw) > self.MAX_LINES:
            self.raw = self.raw[-self.MAX_LINES:]
            self._rewrap_locked()

    def _rewrap_locked(self, old_cols: int | None = None) -> None:
        """Rebuild display rows from raw lines (after a resize or trim).

        ``self.offset`` counts display *rows* from the bottom. Changing
        the width changes how many display rows each raw line wraps
        into, so simply rebuilding ``self.lines`` and leaving the old
        numeric offset in place used to jump the viewport to an
        unrelated position: narrowing (more wrap rows per raw line)
        made it look proportionally further back, widening made it jump
        forward. Instead, find which raw line was at the top of the
        viewport before the rewrap (using the *old* width) and keep
        that same raw line at the top after re-wrapping at the new
        width.
        """
        height = self._height_locked()
        old_total = len(self.lines)
        anchor_idx: int | None = None
        anchor_sub = 0
        if self.offset > 0 and self.raw and old_cols:
            top_row = max(0, old_total - self.offset - height)
            consumed = 0
            for idx, raw in enumerate(self.raw):
                n = max(1, len(_wrap_ansi(raw, old_cols)))
                if consumed + n > top_row:
                    anchor_idx = idx
                    anchor_sub = top_row - consumed
                    break
                consumed += n
            else:
                anchor_idx = len(self.raw) - 1
                anchor_sub = 0

        self.lines = []
        for raw in self.raw:
            self.lines.extend(_wrap_ansi(raw, self._cols))

        if anchor_idx is not None:
            consumed = 0
            new_top_row = 0
            for idx, raw in enumerate(self.raw):
                n = max(1, len(_wrap_ansi(raw, self._cols)))
                if idx == anchor_idx:
                    new_top_row = consumed + min(anchor_sub, n - 1)
                    break
                consumed += n
            new_total = len(self.lines)
            max_offset = max(0, new_total - height)
            self.offset = max(0, min(max_offset, new_total - height - new_top_row))

    def write(self, text: str) -> None:
        """Write text into the viewport buffer and re-render."""
        with _UI_LOCK:
            if not self.active:
                _safe_write(text)
                return
            if not text:
                return

            data = self.partial + text.replace("\r", "")
            parts = data.split("\n")
            self.partial = parts.pop()

            if parts:
                self._commit_lines_locked(parts)

            # Throttle rapid streaming fragments to avoid flicker. This
            # class has no timer of its own to force a render later — a
            # throttled fragment stays buffered in self.partial/self.lines
            # until either a later write() lands >= 50ms after the last
            # render, or a caller explicitly calls flush(). Any streaming
            # caller MUST call flush() when it has no more text coming,
            # or the last buffered fragment can be left unrendered.
            now = time.monotonic()
            if now - self._last_render < 0.05 and len(text) < 500:
                return
            self._last_render = now
            self._render_content_locked()

    def flush(self) -> None:
        """Force render of any buffered partial line."""
        with _UI_LOCK:
            if not self.active:
                if self.partial:
                    _safe_write(self.partial)
                return
            self._last_render = 0.0
            self._render_content_locked()

    def clear_content(self) -> None:
        with _UI_LOCK:
            self.raw = []
            self.lines = []
            self.partial = ""
            self.offset = 0
            self._last_visible = None
            if self.active:
                self._render_content_locked()

    def render_content(self) -> None:
        with _UI_LOCK:
            self._render_content_locked()

    def _render_content_locked(self, _sync: bool = True) -> None:
        if not self.active:
            return

        height = self._height_locked()
        max_offset = max(0, len(self.lines) - height)
        if self.offset > max_offset:
            self.offset = max_offset
        visible = self._rows_locked()

        new_visible: list[str] = []
        for i in range(height):
            line = visible[i] if i < len(visible) else ""
            new_visible.append(_fit_line(line, self._cols))

        # Remove scroll region to draw freely.
        _safe_write("\033[r")
        # Synchronized output prevents flicker.
        if _sync:
            _safe_write("\033[?2026h")
        try:
            if self._last_visible is None or len(self._last_visible) != height:
                # Full repaint.
                for i, line in enumerate(new_visible):
                    row = self.content_top + i
                    _safe_write(f"\033[{row};1H\033[2K{line}")
            else:
                # Only redraw changed rows.
                for i, line in enumerate(new_visible):
                    if line != self._last_visible[i]:
                        row = self.content_top + i
                        _safe_write(f"\033[{row};1H\033[2K{line}")

            self._last_visible = new_visible
        finally:
            if _sync:
                _safe_write("\033[?2026l")  # end synchronized output
        sys.stdout.flush()
        self._apply_region_locked()

    def restore_popup_rows(self, count: int) -> None:
        """Repaint frame under popup; idempotent to avoid scroll drift."""
        with _UI_LOCK:
            if not self.active or self._last_visible is None or count <= 0:
                return

            # Full repaint handles all edge cases.
            self._render_content_locked()

    # ── scroll ────────────────────────────────────────────────

    def scroll_up(self, amount: int = 3) -> None:
        with _UI_LOCK:
            if not self.active:
                return
            height = self._height_locked()
            max_offset = max(0, len(self.lines) - height)
            new_offset = max(0, min(max_offset, self.offset + amount))
            if new_offset != self.offset:
                self.offset = new_offset
                self._render_content_locked()
                if self.offset > 0:
                    self.draw_border_status("")

    def scroll_down(self, amount: int = 3) -> None:
        with _UI_LOCK:
            if not self.active:
                return
            height = self._height_locked()
            max_offset = max(0, len(self.lines) - height)
            new_offset = max(0, min(max_offset, self.offset - amount))
            if new_offset != self.offset:
                self.offset = new_offset
                self._render_content_locked()
                if self.offset > 0:
                    self.draw_border_status("")

    def scroll_to_bottom(self) -> None:
        """Snap the viewport back to the newest content (end of a turn)."""
        with _UI_LOCK:
            if not self.active:
                return
            if self.offset:
                self.offset = 0
                self._render_content_locked()
            self.draw_border_status("")

    # ── chrome ────────────────────────────────────────────────

    def _draw_info_bar_locked(self) -> None:
        """Draw top info bar: workspace, model, approval, cache rate."""
        if not self.active:
            return
        enabled = getattr(self._style, "enabled", True)
        st = self._style

        # Build info items.
        llm = self._session.config.get("llm", {}) if self._session and hasattr(self._session, "config") else {}
        model = llm.get("model", "?")
        reasoning = llm.get("reasoning_effort") or "off"
        ws = getattr(getattr(self._session, "sandbox", None), "root", "") if self._session else ""
        if ws:
            ws_short = ws.replace("\\", "/").rstrip("/").split("/")[-1]
        else:
            ws_short = "~"

        # Approval mode.
        approval = getattr(getattr(self._session, "approvals", None), "mode", "auto") if self._session else "auto"

        # Cache hit rate from totals.
        cache_hit = 0
        tokens_in = 0
        if self._session and hasattr(self._session, "totals"):
            cache_hit = self._session.totals.get("cache_hit", 0)
            tokens_in = self._session.totals.get("tokens_in", 0)
        if tokens_in > 0:
            rate = f"{cache_hit * 100 // tokens_in}%"
        else:
            rate = "—"

        # Show model+effort so mid-session changes are visible. Labels sit
        # in ash; the model is the one bone value; only "yolo" approval
        # borrows the crimson accent, because that mode deserves a look.
        model_display = f"{model} ({reasoning})"
        approval_color = theme.BLOOD_BOLD if approval == "yolo" else theme.ASH
        items = []
        if st and enabled:
            items.append(st._wrap(theme.ASH, "WORKSPACE: ") + st._wrap(theme.ASH, ws_short))
            items.append(st._wrap(theme.ASH, "MODEL: ") + st._wrap(theme.BONE, model_display))
            items.append(st._wrap(theme.ASH, "APPROVAL: ") + st._wrap(approval_color, approval))
            items.append(st._wrap(theme.ASH, "CACHE: ") + st._wrap(theme.ASH, rate))
        else:
            items.append(f"WORKSPACE: {ws_short}")
            items.append(f"MODEL: {model_display}")
            items.append(f"APPROVAL: {approval}")
            items.append(f"CACHE: {rate}")

        # Join items with hairline separators.
        sep = _hair(" │ ", enabled)
        info = sep.join(items)
        info = _fit_line(info, self._cols)

        _safe_write(f"\033[{self.info_row};1H\033[2K{info}")

        # Info border — a quiet hairline under the bar.
        border = _hair("─" * max(0, self._cols), enabled)
        _safe_write(f"\033[{self.info_border_row};1H\033[2K{border}")

    def draw_chrome(self) -> None:
        with _UI_LOCK:
            self._draw_chrome_locked()

    def _draw_chrome_locked(self, skip_prompt: bool = False) -> None:
        if not self.active:
            return

        _safe_write("\033[r")

        # Top info bar.
        self._draw_info_bar_locked()

        # Border line; a right-aligned scroll marker rides its edge while
        # the viewport is scrolled up.
        border = self._border_status_row_locked("")
        _safe_write(f"\033[{self.border_row};1H\033[2K{border}")

        # Prompt owned by LineEditor; skip here.

        sys.stdout.flush()
        self._apply_region_locked()

    # ── bottom chrome: literal prompt box ─────────────────────
    #
    # The bottom two rows are a closed box around the prompt (Claude-Code
    # style): the border row (rows-1) is the box's top edge with corners
    # ╭ ─ ╮ and the spinner / transient status riding inside it, and the
    # prompt row (rows, the last screen row) is the box's interior with
    # the bone label and the operator's input. The right edge of the
    # prompt row is closed with a │ so long input never escapes the box.
    # While the viewport is scrolled up, a dim ``↑ n`` marker rides inside
    # the border row, just before the right corner.

    def _wall(self) -> str:
        """Vertical box edge for the prompt row (hairline │)."""
        enabled = getattr(self._style, "enabled", True)
        return _hair("│", enabled)

    def wall_glyph(self) -> str:
        """Styled right wall the editor must reserve and re-emit each draw."""
        return self._wall()

    def _border_status_row_locked(self, text: str = "") -> str:
        """The box top edge: ``╭─ <status> ── ╮``, with the ↑ marker inside."""
        enabled = getattr(self._style, "enabled", True)
        inner = max(0, self._cols - 2)
        marker = ""
        if self.offset > 0:
            marker = f" \u2191{self.offset}"
        mw = _vis(marker)

        out = _hair("╭", enabled)
        if not text and not marker:
            return out + _hair("─" * inner, enabled) + _hair("╮", enabled)

        lead = "─ " if text else "─"
        if text:
            # Keep at least one dash each side of the status text so the
            # row always reads as a border, not a bare status line.
            avail_t = max(0, inner - mw - 4)
            shown = _fit_line(text, avail_t)
            out += _hair(lead, enabled) + shown + _hair(" ", enabled)
        else:
            out += _hair(lead, enabled)
        pad = inner - (_vis(lead) + (_vis(text) + 1 if text else 0) + mw)
        out += _hair("─" * max(0, pad), enabled)
        if marker:
            out += _faint(marker, enabled)
        return out + _hair("╮", enabled)

    def _prompt_row_locked(self, body: str = "") -> str:
        """Full prompt-row content: label + blank interior + right wall.

        ``body`` is the bone ``│ MANTRA >`` label (already carries the left
        edge). The row is padded to the last column and closed with the
        right │ so the box looks shut even before anything is typed. The
        editor redraws this row itself while typing, reserving the wall
        column so input can never escape the box.
        """
        if not body:
            body = self._prompt_body_locked()
        wall = self._wall()
        avail = max(0, self._cols - 1)  # leave the last column for the wall
        body = _fit_line(body, avail)
        return body + " " * max(0, avail - _vis(body)) + wall

    def box_edge_row(self, text: str = "") -> str:
        """Top edge of an expanded multi-line prompt box: ``╭─ <chip> ── ╮``.

        Unlike ``_border_status_row_locked`` this never shows the ``↑ n``
        scroll marker: inside a paste box the marker would describe the
        transcript hidden behind the box, which only confuses. The size
        chip ("· N lines · M chars", with the "… K more above" prefix
        when the box is clipped) rides inside the corners exactly like
        the status text does on the single-line border row.
        """
        enabled = getattr(self._style, "enabled", True)
        inner = max(0, self._cols - 2)
        out = _hair("╭", enabled)
        if not text:
            return out + _hair("─" * inner, enabled) + _hair("╮", enabled)
        lead = "─ "
        avail_t = max(0, inner - 4)
        shown = _fit_line(text, avail_t)
        out += _hair(lead, enabled) + shown + _hair(" ", enabled)
        pad = inner - (_vis(lead) + _vis(shown) + 1)
        out += _hair("─" * max(0, pad), enabled)
        return out + _hair("╮", enabled)

    def draw_border_status(self, text: str = "") -> None:
        with _UI_LOCK:
            if not self.active:
                return
            _safe_write("\033[r")
            line = self._border_status_row_locked(text)
            _safe_write(f"\033[{self.border_row};1H\033[2K{line}")
            sys.stdout.flush()
            self._apply_region_locked()

    def redraw_content_and_chrome(self) -> None:
        """Full repaint of content rows and the bottom border row.

        The prompt editor temporarily covers content and border rows to
        show a multi-line input above the prompt; this restores them
        exactly. It is a full repaint (not a diff) because the terminal
        screen already diverged from the layout's ``_last_visible``
        snapshot under the covered rows.
        """
        with _UI_LOCK:
            if not self.active:
                return
            self._last_visible = None
            self._render_content_locked()
            line = self._border_status_row_locked("")
            _safe_write(f"\033[{self.border_row};1H\033[2K{line}")
            sys.stdout.flush()
            self._apply_region_locked()

    # ── prompt — static label: hairline wall, bone wordmark. It does not
    # change while Channeling/Chanting; only the border row above pulses.
    def _prompt_body_locked(self) -> str:
        st = self._style
        base = "│ MANTRA > "
        if st is None or not getattr(st, "enabled", True):
            return base
        return st._wrap(theme.HAIR, "│ ") + st._wrap(theme.BONE, "MANTRA > ")

    def start_prompt_pulse(self) -> None:
        pass

    def stop_prompt_pulse(self) -> None:
        pass

    def prompt_text(self, body: str = "") -> str:
        with _UI_LOCK:
            if not body:
                body = self._prompt_body_locked()
            if not self.active:
                return body
            # Positioning + label only: the editor appends the typed input
            # after the label and re-emits the box wall itself on every
            # draw, so text written after this string lands inside the box.
            return f"\033[{self.prompt_row};1H\033[2K{body}"

    def draw_prompt(self, body: str = "") -> None:
        with _UI_LOCK:
            if not self.active:
                return
            _safe_write("\033[r")
            self._draw_prompt_locked(body)
            sys.stdout.flush()
            self._apply_region_locked()

    def _draw_prompt_locked(self, body: str = "") -> None:
        if not self.active:
            return
        # Full closed row: label + blank interior + right wall, so the
        # box looks shut even when nothing has been typed yet.
        row = self._prompt_row_locked(body)
        _safe_write(f"\033[{self.prompt_row};1H\033[2K{row}")

    # ── splash ────────────────────────────────────────────────

    def show_splash(self) -> int:
        with _UI_LOCK:
            if not self.active:
                return 0
            enabled = getattr(self._style, "enabled", True)
            width = max(30, min(80, self._cols - 4))
            card = render_card(self._session, width, enabled=enabled)
            height = self._height_locked()
            top_pad = max(0, (height - len(card)) // 2)

            self.raw = []
            self.lines = [""] * top_pad
            self.partial = ""
            self.offset = 0
            self._last_visible = None

            for line in card:
                pad = max(0, (self._cols - _vis(line)) // 2)
                self.lines.append(" " * pad + line)

            self._splash_visible = True
            self._render_content_locked()
            return len(card)

    def hide_splash(self) -> None:
        with _UI_LOCK:
            if not self._splash_visible:
                return
            self._splash_visible = False
            self.raw = []
            self.lines = []
            self.partial = ""
            self.offset = 0
            self._last_visible = None
            if self.active:
                self._render_content_locked()

    def get_line_at_row(self, screen_row: int) -> str | None:
        with _UI_LOCK:
            if not self.active:
                return None
            if not (self.content_top <= screen_row <= self.content_bottom):
                return None
            visible = self._rows_locked()
            idx = screen_row - self.content_top
            if 0 <= idx < len(visible):
                return visible[idx]
            return None

    # ── resize ────────────────────────────────────────────────

    def check_resize(self) -> bool:
        cols, rows = _term_size()
        if cols == self._cols and rows == self._rows:
            return False

        with _UI_LOCK:
            old_cols = self._cols
            old_rows = {self.info_row, self.info_border_row, self.border_row, self.prompt_row}
            old_max = self._rows

            # Clear old chrome before changing geometry.
            _safe_write("\033[r")
            for row in old_rows:
                if 1 <= row <= old_max:
                    _safe_write(f"\033[{row};1H\033[2K")

            self._recalc(cols, rows)

            # Sync any attached prompt renderer with the new geometry in
            # the same locked step, so it can never draw at a stale row
            # while the chrome/content repaint at the new one.
            if self.prompt_sync is not None:
                try:
                    self.prompt_sync()
                except Exception:
                    pass

            if not self.active:
                self._leave_alt_locked()
                return True

            if not self._alt:
                self._enter_locked()

            # Re-wrap stored lines at the new width so content stays
            # readable instead of re-truncating at the old wrap points.
            # Pass the pre-resize width so the viewport keeps showing the
            # same logical line at the top instead of jumping to an
            # unrelated position (see _rewrap_locked).
            if self.active and not self._splash_visible:
                self._rewrap_locked(old_cols)

            # ✅ Wrap entire redraw in synchronized output to prevent flicker
            _safe_write("\033[?2026h")
            try:
                _safe_write("\033[2J\033[H")
                self._draw_chrome_locked(skip_prompt=True)   # Do NOT draw prompt here; editor will
                self._render_content_locked(_sync=False)
                self._apply_region_locked()
            finally:
                _safe_write("\033[?2026l")
            sys.stdout.flush()
            return True

    # ── cursor helpers ────────────────────────────────────────

    def move_to_content(self) -> None:
        with _UI_LOCK:
            if not self.active:
                return
            _safe_write(f"\033[{self.content_bottom};1H")
            sys.stdout.flush()

    def move_to_prompt(self) -> None:
        self.draw_prompt()

    def move_to_dashboard(self) -> None:
        with _UI_LOCK:
            if not self.active:
                return
            _safe_write("\033[1;1H")
            sys.stdout.flush()