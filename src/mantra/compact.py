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


def _white(t, e=True): return _ansi("38;5;230", t, e)  # ivory bone (Vampire)
def _grey(t, e=True): return _ansi("38;5;240", t, e)   # smoke grey-wine
def _dim(t, e=True): return _ansi("38;5;240", t, e)     # same dim as border
def _bold(t, e=True): return _ansi("1;38;5;88", t, e)   # burgundy bold
def _gold(t, e=True): return _ansi("38;5;172", t, e)    # antique amber


def _pad(text: str, width: int) -> str:
    diff = width - _vis(text)
    return text + (" " * max(0, diff))


def _shorten(text: str, width: int) -> str:
    if width <= 0:
        return ""
    if _vis(text) <= width:
        return text
    if width == 1:
        return "..."
    result: list[str] = []
    vis = 0
    limit = width - 1
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
    # Vampire aqua for M A N T R A — pulse handled by thread toggling bright/dim
    mantra_pulse = _ansi("1;38;5;51", "M A N T R A", enabled)
    lines = []
    for raw in [
        mantra_pulse,
        _dim("Spells Matter", enabled),
        _dim(ver, enabled),
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
    return _dim(core, enabled)


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

        # Internal viewport buffer.
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

    def _all_lines_locked(self) -> list[str]:
        if self.partial:
            return self.lines + [self.partial]
        return self.lines

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
                self.lines.extend(parts)

            if len(self.lines) > self.MAX_LINES:
                self.lines = self.lines[-self.MAX_LINES:]

            # Throttle rapid streaming fragments, but ensure the buffer
            # always flushes: if caller passes the final fragment, flush()
            # will be called; but while streaming, short throttled fragments
            # must not be lost — force render if throttled too long (200ms)
            now = time.monotonic()
            if now - self._last_render < 0.05 and len(text) < 500:
                # Still buffered in self.partial/lines; schedule deferred
                # render only if not already pending. For correctness, flush
                # is also forced by explicit flush() call after streaming.
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
        lines = self._all_lines_locked()

        max_offset = max(0, len(lines) - height)
        if self.offset > max_offset:
            self.offset = max_offset

        end = len(lines) - self.offset
        start = max(0, end - height)
        visible = lines[start:end]

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
            lines = self._all_lines_locked()
            max_offset = max(0, len(lines) - height)
            self.offset = max(0, min(max_offset, self.offset + amount))
            self._render_content_locked()

    def scroll_down(self, amount: int = 3) -> None:
        with _UI_LOCK:
            if not self.active:
                return
            height = self._height_locked()
            lines = self._all_lines_locked()
            max_offset = max(0, len(lines) - height)
            self.offset = max(0, min(max_offset, self.offset - amount))
            self._render_content_locked()

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

        # Show model+effort so mid-session changes are visible — Vampire: each value distinct but gothic.
        model_display = f"{model} ({reasoning})"
        items = []
        if st and enabled:
            items.append(st._wrap("38;5;240", "WORKSPACE: ") + st._wrap("38;5;172", ws_short))  # amber
            items.append(st._wrap("38;5;240", "MODEL: ") + st._wrap("1;38;5;51", model_display))  # aqua bold
            items.append(st._wrap("38;5;240", "APPROVAL: ") + st._wrap("38;5;29", approval))  # emerald
            items.append(st._wrap("38;5;240", "CACHE: ") + st._wrap("38;5;230", rate))  # ivory
        else:
            items.append(f"WORKSPACE: {ws_short}")
            items.append(f"MODEL: {model_display}")
            items.append(f"APPROVAL: {approval}")
            items.append(f"CACHE: {rate}")

        # Join items with separators.
        sep = _grey(" │ ", enabled)
        info = sep.join(items)
        info = _fit_line(info, self._cols)

        _safe_write(f"\033[{self.info_row};1H\033[2K{info}")

        # Info border.
        border = _grey("─" * max(0, self._cols), enabled)
        _safe_write(f"\033[{self.info_border_row};1H\033[2K{border}")

    def draw_chrome(self) -> None:
        with _UI_LOCK:
            self._draw_chrome_locked()

    def _draw_chrome_locked(self, skip_prompt: bool = False) -> None:
        if not self.active:
            return
        enabled = getattr(self._style, "enabled", True)

        _safe_write("\033[r")

        # Top info bar.
        self._draw_info_bar_locked()

        # Clear border row.
        _safe_write(f"\033[{self.border_row};1H\033[2K")

        # Border line.
        border = _grey("─" * max(0, self._cols), enabled)
        _safe_write(f"\033[{self.border_row};1H{border}")

        # Prompt owned by LineEditor; skip here.

        sys.stdout.flush()
        self._apply_region_locked()

    def draw_border_status(self, text: str = "") -> None:
        with _UI_LOCK:
            if not self.active:
                return
            enabled = getattr(self._style, "enabled", True)
            _safe_write("\033[r")
            if text:
                line = _fit_line(text, self._cols)
                _safe_write(f"\033[{self.border_row};1H\033[2K{line}")
            else:
                border = _grey("─" * max(0, self._cols), enabled)
                _safe_write(f"\033[{self.border_row};1H\033[2K{border}")
            sys.stdout.flush()
            self._apply_region_locked()

    # ── prompt — static gold, tidak berubah saat Channeling/Chanting
    def _prompt_body_locked(self) -> str:
        st = self._style
        base = "│ MANTRA > "
        if st is None or not getattr(st, "enabled", True):
            return base
        return st._wrap("38;5;172", base)  # gold static

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
        if not body:
            body = self._prompt_body_locked()
        body = _fit_line(body, self._cols)
        _safe_write(f"\033[{self.prompt_row};1H\033[2K{body}")

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
            lines = self._all_lines_locked()
            height = self._height_locked()
            max_offset = max(0, len(lines) - height)
            offset = min(self.offset, max_offset)
            end = len(lines) - offset
            start = max(0, end - height)
            visible = lines[start:end]
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
            old_rows = {self.info_row, self.info_border_row, self.border_row, self.prompt_row}
            old_max = self._rows

            # Clear old chrome before changing geometry.
            _safe_write("\033[r")
            for row in old_rows:
                if 1 <= row <= old_max:
                    _safe_write(f"\033[{row};1H\033[2K")

            self._recalc(cols, rows)

            if not self.active:
                self._leave_alt_locked()
                return True

            if not self._alt:
                self._enter_locked()

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