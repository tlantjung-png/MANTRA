"""Shared harness for the terminal-application suites.

The backend is faked with a scripted event queue and a recording writer,
so frames can be asserted straight off the cell grid — no ANSI parsing,
no real terminal. Every ``test_tui_*`` module (and the suites that borrow
its fakes: review, fixes, surfaces) imports the helpers from here, so
there is exactly one place that owns them.
"""

from __future__ import annotations

import atexit
import os
import queue
import shutil
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "."))
_tests_dir = os.path.dirname(os.path.abspath(__file__))
if _tests_dir not in sys.path:
    sys.path.insert(0, _tests_dir)

import core.tui.app as app_module  # noqa: E402
from core.tui.app import TuiApp  # noqa: E402
from core.tui.buffer import Buffer  # noqa: E402

from _helpers import make_session  # noqa: E402

# Re-exported so callers keep ``from tui_harness import app_module``.
__all__ = [
    "Buffer",
    "BufferTest",
    "FakeBackend",
    "TuiApp",
    "_TEMP_WORKSPACES",
    "_chip_cell_params",
    "_chip_row_text",
    "_cleanup_temp_workspaces",
    "_make_app",
    "app_module",
    "grid_rows",
    "wait_until",
]


class FakeBackend:
    def __init__(self, cols: int = 100, rows: int = 30):
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


def grid_rows(buf: Buffer) -> list[str]:
    out = []
    for y in range(buf.rows):
        out.append("".join(buf.chars[y * buf.cols : (y + 1) * buf.cols]).rstrip())
    return out


def _chip_row_text(app):
    """Painted text of the chip row (bottom content row)."""
    return "".join(
        app.renderer.buffer.chars[
            (app._content_top + app._content_height - 1) * app.renderer.buffer.cols :
            (app._content_top + app._content_height) * app.renderer.buffer.cols
        ]
    )


def _chip_cell_params(app, glyph="↓"):
    """SGR params of the cell under the chip's first glyph."""
    row_start = (app._content_top + app._content_height - 1) * app.renderer.buffer.cols
    row = app.renderer.buffer.chars[row_start : row_start + app.renderer.buffer.cols]
    x = row.index(glyph)
    style_id = app.renderer.buffer.styles[row_start + x]
    return app.renderer.buffer.styles_table.params_for(style_id)


def wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def _make_app(script, backend=None):
    workspace = os.path.join(
        os.environ.get("TEMP", tempfile.gettempdir()),
        f"mantra-tui-{os.getpid()}-{threading.get_ident()}",
    )
    os.makedirs(workspace, exist_ok=True)
    _TEMP_WORKSPACES.append(workspace)
    session = make_session(workspace, script)
    backend = backend or FakeBackend()
    app = TuiApp(session, backend=backend)
    app._init_surface()
    app._show_welcome()
    return app, session, backend


# Workspaces _make_app (and the popup test) create under TEMP; the atexit
# hook removes them on process exit so cross-module _make_app users
# (test_fix, test_review) never leave stray directories behind (each one
# may contain a git repo). AppIntegrationTest.tearDown still clears them
# eagerly within this module's own runs.
_TEMP_WORKSPACES: list[str] = []


def _cleanup_temp_workspaces() -> None:
    for ws in list(_TEMP_WORKSPACES):
        shutil.rmtree(ws, ignore_errors=True)
    _TEMP_WORKSPACES.clear()


atexit.register(_cleanup_temp_workspaces)


class BufferTest(unittest.TestCase):
    """A placeholder base so suites can keep ``unittest.TestCase`` imports.

    Real buffer/widget tests live in the topical suites (test_tui_buffer,
    test_tui_composer, ...); nothing should subclass this.
    """


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
