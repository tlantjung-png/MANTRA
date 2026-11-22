"""Headless visual snapshot of the TUI chip states.

Renders the busy-chip rows (idle, busy, chat/"Enchanting") exactly the
way the TUI composes them and converts the ANSI styling to HTML, so a
human can open the file in any browser and verify the visual on a
machine without a GUI/terminal. This is the closest headless substitute
for a real-browser check of the chip; it does not replace a final look
at the live terminal.

Usage:
    python -m core.tui.snapshot [--out path/to/chip-preview.html]
"""

from __future__ import annotations

import re
import sys
import time
from html import escape as _esc

from core.tui.app import _CHAT_THEME_WORD, TuiApp

_ANSI_SPLIT = re.compile(r"(\033\[[0-9;]*m)")


def _xterm_hex(n: int) -> str:
    """Approximate xterm-256 palette to a hex colour."""
    if n < 16:
        base = (
            "000000", "800000", "008000", "808000",
            "000080", "800080", "008080", "c0c0c0",
            "808080", "ff0000", "00ff00", "ffff00",
            "0000ff", "ff00ff", "00ffff", "ffffff",
        )
        return base[n]
    if n < 232:
        n -= 16
        r, g, b = n // 36, (n // 6) % 6, n % 6
        scale = lambda v: 0 if v == 0 else 55 + 40 * v  # noqa: E731
        return "%02x%02x%02x" % (scale(r), scale(g), scale(b))
    v = 8 + (n - 232) * 10
    return "%02x%02x%02x" % (v, v, v)


def ansi_to_html(text: str) -> str:
    """Convert ANSI SGR sequences to inline-styled HTML spans."""
    out: list[str] = []
    open_spans = 0
    for part in _ANSI_SPLIT.split(text):
        if not part:
            continue
        if part.startswith("\033["):
            codes = part[2:-1].split(";")
            if not codes or codes == [""] or "0" in codes:
                if open_spans:
                    out.append("</span>" * open_spans)
                    open_spans = 0
                continue
            props: list[str] = []
            i = 0
            while i < len(codes):
                code = codes[i]
                if code == "1":
                    props.append("font-weight:bold")
                elif code == "2":
                    props.append("color:#8a8f98")
                elif code == "38" and i + 2 < len(codes) and codes[i + 1] == "5":
                    try:
                        props.append("color:#" + _xterm_hex(int(codes[i + 2])))
                    except ValueError:
                        pass
                    i += 2
                elif code == "39":
                    props.append("color:inherit")
                i += 1
            if props:
                out.append('<span style="%s">' % ";".join(props))
                open_spans += 1
        else:
            out.append(_esc(part))
    if open_spans:
        out.append("</span>" * open_spans)
    return "".join(out)


def _chip_app(**attrs):
    """A minimal object with the attributes _border_text() reads."""
    app = type("ChipApp", (), {})()
    app.busy = False
    app.busy_label = "Channeling"
    app._spinner_i = 0
    app.chat_busy = False
    app.chat_model = ""
    app.chat_budget = None
    app.status_text = ""
    app.counter_text = ""
    app.queued = ""
    app._turn_started = time.monotonic()
    for key, value in attrs.items():
        setattr(app, key, value)
    return app


def chip_rows() -> list[tuple[str, str]]:
    """Return (label, ansi_text) rows for the chip states."""
    now = time.monotonic()
    idle = _chip_app()
    busy = _chip_app(busy=True, busy_label="Chanting", _turn_started=now - 12)
    chat = _chip_app(
        busy=True,
        chat_busy=True,
        chat_model="LongCat-2.5",
        chat_budget=16384,
        _turn_started=now - 45,
        counter_text="1234 tok · 42.1 tok/s",
    )
    border = TuiApp._border_text
    return [
        ("idle (no turn)", border(idle)),
        ("busy turn", border(busy)),
        ("chat · %s" % _CHAT_THEME_WORD, border(chat)),
    ]


def render_page() -> str:
    rows = chip_rows()
    parts = [
        "<!doctype html><html><head><meta charset=\"utf-8\">",
        "<title>MANTRA chip snapshot</title><style>",
        "body{background:#0d0d10;color:#d7dae0;font-family:Consolas,monospace;padding:24px}",
        "h2{color:#7dd3fc;font-size:15px;font-weight:normal;margin:18px 0 4px}",
        ".chip{background:#15151b;border:1px solid #2a2a35;border-radius:6px;",
        "padding:8px 12px;display:inline-block;white-space:pre;min-width:420px}",
        ".dim{color:#8a8f98;font-size:12px;margin:2px 0 0}",
        "</style></head><body><h1>MANTRA busy-chip snapshot</h1>",
    ]
    for label, ansi in rows:
        parts.append(f'<h2>{_esc(label)}</h2><div class="chip">{ansi_to_html(ansi)}</div>')
    parts.append('<div class="dim">generated headlessly — open this file in any browser</div>')
    parts.append("</body></html>")
    return "".join(parts)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    out = "chip-preview.html"
    if argv and argv[0] == "--out":
        if len(argv) < 2:
            print("usage: python -m core.tui.snapshot [--out path.html]", file=sys.stderr)
            return 2
        out = argv[1]
    with open(out, "w", encoding="utf-8") as handle:
        handle.write(render_page())
    print(f"wrote chip snapshot to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
