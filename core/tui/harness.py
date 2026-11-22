"""Headless full-UI rendering harness for the MANTRA TUI.

Runs the real ``TuiApp`` (real renderer, fake backend), drives it into
reproducible visual states (idle, busy chat with the themed chip), paints
whole frames, and exports them as plain text and styled HTML — so the
final visual can be checked in any browser on a machine without a GUI or
a real terminal. This is the headless substitute for a browser/screenshot
check of the interface; it does not replace a final look at the live
terminal.

Usage:
    python -m core.tui.harness [--out DIR] [--cols 110] [--rows 32]
"""

from __future__ import annotations

import os
import queue
import sys
import time

from core.config import merge_defaults
from core.console_render import Style
from core.tui.app import TuiApp
from core.tui.snapshot import ansi_to_html


class FakeBackend:
    """Minimal backend: a fixed terminal size, no real TTY."""

    def __init__(self, cols: int = 110, rows: int = 32) -> None:
        self.events: queue.Queue = queue.Queue()
        self.size = (cols, rows)
        self.writes: list[str] = []
        self.stopped = False

    def write(self, text: str) -> None:
        self.writes.append(text)

    def stop(self) -> None:
        self.stopped = True

    def current_size(self) -> tuple[int, int]:
        return self.size

    def emit_resize_if_changed(self) -> None:
        pass


class _StubSession:
    """Session surface the renderer touches; no real console logic needed."""

    def __init__(self) -> None:
        config = merge_defaults({})
        config["logging"] = {"type": "jsonl", "path": "harness-session.jsonl"}
        self.config = config
        self.style = Style(enabled=True)


def build(backend: FakeBackend | None = None):
    """Construct the real TUI app with a stub session and fake backend."""
    backend = backend or FakeBackend()
    session = _StubSession()
    app = TuiApp(session, backend=backend)
    app._init_surface()
    return app, session, backend


def grid_ansi(app) -> str:
    """Reconstruct the painted frame as ANSI text from the cell grid."""
    buf = app.renderer.buffer
    cols, rows = buf.cols, buf.rows
    chars = buf.chars
    styles = buf.styles
    out: list[str] = []
    for y in range(rows):
        line: list[str] = []
        current: tuple[str, ...] = ()
        for x in range(cols):
            i = y * cols + x
            ch = chars[i]
            params = buf.styles_table.params_for(styles[i])
            if params != current:
                if current:
                    line.append("\033[0m")
                if params:
                    line.append("\033[" + ";".join(params) + "m")
                current = params
            line.append(ch)
        if current:
            line.append("\033[0m")
        out.append("".join(line).rstrip())
    return "\n".join(out)


def grid_text(app) -> str:
    """The painted frame as plain text (styles dropped)."""
    buf = app.renderer.buffer
    cols, rows = buf.cols, buf.rows
    chars = buf.chars
    return "\n".join(
        "".join(chars[y * cols : (y + 1) * cols]).rstrip() for y in range(rows)
    )


def render_page(grid: str, title: str, note: str) -> str:
    parts = [
        "<!doctype html><html><head><meta charset=\"utf-8\">",
        f"<title>{title}</title><style>",
        "body{background:#0d0d10;color:#d7dae0;font-family:Consolas,monospace;padding:24px}",
        "h1{font-size:17px;color:#7dd3fc;font-weight:normal}",
        "h2{font-size:13px;color:#8a8f98;font-weight:normal;margin:18px 0 6px}",
        ".screen{background:#0a0a0e;border:1px solid #2a2a35;border-radius:8px;",
        "padding:12px;overflow-x:auto}",
        "pre{font-family:Consolas,monospace;font-size:13px;line-height:1.25;margin:0}",
        ".note{color:#8a8f98;font-size:12px;margin-top:10px}",
        "</style></head><body>",
        f"<h1>{title}</h1>",
        f'<div class="screen"><pre>{ansi_to_html(grid)}</pre></div>',
        f'<div class="note">{note}</div>',
        "</body></html>",
    ]
    return "".join(parts)


def chat_state(app, model: str = "LongCat-2.5", budget: int = 16384) -> None:
    """Put the app into the chat-in-flight visual state."""
    with app.lock:
        app.busy = True
        app.busy_label = "Chanting"
        app._turn_started = time.monotonic() - 45
        app.chat_busy = True
        app.chat_model = model
        app.chat_budget = budget
        app.counter_text = "1234 tok · 42.1 tok/s"
    app.mark_dirty()


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    out_dir = "."
    cols, rows = 110, 32
    i = 0
    while i < len(argv):
        if argv[i] == "--out" and i + 1 < len(argv):
            out_dir = argv[i + 1]
            i += 2
        elif argv[i] == "--cols" and i + 1 < len(argv):
            cols = int(argv[i + 1])
            i += 2
        elif argv[i] == "--rows" and i + 1 < len(argv):
            rows = int(argv[i + 1])
            i += 2
        else:
            print(f"unknown option: {argv[i]}", file=sys.stderr)
            print("usage: python -m core.tui.harness [--out DIR] [--cols N] [--rows N]", file=sys.stderr)
            return 2
    os.makedirs(out_dir, exist_ok=True)
    app, _session, _backend = build(FakeBackend(cols=cols, rows=rows))
    app.feed_output("You: bikin nailvin.html lebih bagus, stunning, dark, elegant\n")
    app.feed_output("▸ Enchanter: membaca nailvin.html…\n")
    chat_state(app)
    app.render_frame()
    preview = os.path.join(out_dir, "preview.html")
    grid_path = os.path.join(out_dir, "grid.txt")
    with open(preview, "w", encoding="utf-8") as handle:
        handle.write(
            render_page(
                grid_ansi(app),
                "MANTRA TUI — headless render (busy chat)",
                "generated headlessly by core.tui.harness — open in any browser",
            )
        )
    with open(grid_path, "w", encoding="utf-8") as handle:
        handle.write(grid_text(app) + "\n")
    print(f"wrote {preview}")
    print(f"wrote {grid_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
