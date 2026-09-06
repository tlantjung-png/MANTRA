"""Shared terminal primitives: width, size, raw mode."""

from __future__ import annotations

import os
import re
import sys
from contextlib import contextmanager

# Bracketed (CSI) and OS-command (OSC) escape forms. An OSC with no
# terminator strips to the end of the string, so unterminated
# sequences cannot leak into the visible-width math either. The CSI
# parameter class includes ':' for ISO-8613-6 colon-form SGR
# sequences (\033[38:2::255:0:0m).
_ANSI_RE = re.compile(r"\033\[[0-9;?:]*[ -/]*[@-~]|\033][^\x07\x1b]*(?:\x07|\x1b\\)?")


def _char_width(ch: str) -> int:
    """Width of one char; CJK/emoji count as 2, zero-width chars as 0."""
    import unicodedata

    if unicodedata.combining(ch):
        return 0
    if unicodedata.category(ch) in ("Mn", "Me", "Cf"):
        # Nonspacing/enclosing marks and format characters (ZWJ,
        # variation selectors) occupy no columns.
        return 0
    eaw = unicodedata.east_asian_width(ch)
    if eaw in ("W", "F"):
        return 2
    return 1


def visible_len(text: str) -> int:
    """Printed width ignoring escapes; wide chars count as 2."""
    return sum(_char_width(c) for c in _ANSI_RE.sub("", text))


def selection_in_progress() -> bool:
    """True while the terminal host is performing a native selection.

    On Windows, GetConsoleSelectionInfo reports an in-progress click-drag
    selection (CONSOLE_SELECTION_IN_PROGRESS). The streaming renderer
    defers repaints while this is true so the drag is never disturbed.
    On POSIX the terminal emulator owns selection and exposes no API to
    observe it from inside the process, so this returns False.
    """
    if os.name != "nt":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        class _Coord(ctypes.Structure):
            _fields_ = [("X", wintypes.SHORT), ("Y", wintypes.SHORT)]

        class _SelectionInfo(ctypes.Structure):
            _fields_ = [
                ("dwFlags", wintypes.DWORD),
                ("dwSelectionAnchor", _Coord),
                ("srSelection", wintypes.SMALL_RECT),
            ]

        info = _SelectionInfo()
        ok = ctypes.windll.kernel32.GetConsoleSelectionInfo(ctypes.byref(info))
        # CONSOLE_SELECTION_IN_PROGRESS = 0x0001
        return bool(ok and (info.dwFlags & 0x0001))
    except Exception:
        return False


def term_size() -> tuple[int, int]:
    """Visible size (cols, rows); Windows-aware."""
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes

            class _Coord(ctypes.Structure):
                _fields_ = [("X", wintypes.SHORT), ("Y", wintypes.SHORT)]

            class _Rect(ctypes.Structure):
                _fields_ = [
                    ("Left", wintypes.SHORT),
                    ("Top", wintypes.SHORT),
                    ("Right", wintypes.SHORT),
                    ("Bottom", wintypes.SHORT),
                ]

            class _ConsoleInfo(ctypes.Structure):
                _fields_ = [
                    ("Size", _Coord),
                    ("Cursor", _Coord),
                    ("Attributes", wintypes.WORD),
                    ("Window", _Rect),
                    ("MaximumWindowSize", _Coord),
                ]

            info = _ConsoleInfo()
            handle = ctypes.windll.kernel32.GetStdHandle(-11)
            if handle not in (0, -1):
                ok = ctypes.windll.kernel32.GetConsoleScreenBufferInfo(
                    handle, ctypes.byref(info)
                )
                if ok:
                    cols = max(1, info.Window.Right - info.Window.Left + 1)
                    rows = max(1, info.Window.Bottom - info.Window.Top + 1)
                    return cols, rows
        except Exception:
            pass
    try:
        import shutil

        s = shutil.get_terminal_size()
        return max(1, s.columns), max(1, s.lines)
    except Exception:
        return 80, 24


def ansi_strip(text: str) -> str:
    return _ANSI_RE.sub("", text)


@contextmanager
def raw_mode():
    """Raw mode for single-key input; no-op on Windows."""
    if os.name == "nt":
        yield
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


@contextmanager
def cbreak_mode():
    """Per-character input without echo; SIGINT still delivered. No-op on Windows.

    ``raw_mode`` (tty.setraw) also switches ISIG off, so Ctrl+C would
    arrive as a literal byte. cbreak only drops line buffering and echo.
    """
    if os.name == "nt":
        yield
        return
    import termios
    import tty

    fd = sys.stdin.fileno()
    original = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        yield
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, original)


def is_interactive() -> bool:
    """True when stdin and stdout are terminals."""
    try:
        return bool(sys.stdin.isatty() and sys.stdout.isatty())
    except (ValueError, OSError):  # pragma: no cover - closed streams
        return False


def safe_write(text: str) -> None:
    """Write to stdout without ever raising on unencodable characters.

    Model output, typed input and hand-authored labels can all contain
    characters a narrow locale cannot encode (e.g. "→" under latin-1).
    The stream's own encoding is kept - a latin-1 terminal must keep
    receiving latin-1 bytes - and only the characters that do not fit
    are replaced, so output degrades instead of crashing the console.
    """
    out = sys.stdout
    try:
        out.write(text)
    except UnicodeEncodeError:
        enc = getattr(out, "encoding", None) or "utf-8"
        try:
            out.write(
                text.encode(enc, errors="replace").decode(enc, errors="replace")
            )
        except Exception:  # pragma: no cover - closed or exotic stream
            pass


_utf8_configured = False


def force_utf8_output() -> None:
    """Make stdout/stderr encoding-safe; use UTF-8 on Windows consoles. Idempotent.

    Windows consoles start in cp1252: the console codepage and the Python
    stream encoding must both switch to UTF-8 for wide glyphs to render.
    POSIX keeps the locale encoding and only replaces unencodable
    characters. Redirected output has no console, so only the stream
    half applies.
    """
    global _utf8_configured
    if _utf8_configured:
        return
    _utf8_configured = True
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.windll.kernel32
            kernel32.SetConsoleOutputCP(65001)
            kernel32.SetConsoleCP(65001)
            # Enable VT output processing on the output handle. Without
            # it, legacy conhost prints ANSI escapes literally - including
            # the ESC[?2004h bracketed-paste request the editor emits - so
            # pastes would never be wrapped and cursor/color control would
            # garble the TUI. Windows Terminal parses VT regardless, so
            # this only ever helps (conhost) and is a no-op there. The
            # other mode flags are preserved untouched.
            try:
                handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
                mode = wintypes.DWORD()
                if (
                    handle not in (None, 0, -1)
                    and kernel32.GetConsoleMode(handle, ctypes.byref(mode))
                ):
                    if not mode.value & 0x0004:  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
                        kernel32.SetConsoleMode(handle, mode.value | 0x0004)
            except Exception:  # pragma: no cover - non-console runtime
                pass
        except Exception:  # pragma: no cover - non-console or exotic runtime
            pass
        target_encoding = "utf-8"
    else:
        target_encoding = None
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            if target_encoding is not None:
                reconfigure(encoding=target_encoding, errors="replace")
            else:
                reconfigure(errors="replace")
        except Exception:  # pragma: no cover - read-only stream
            pass
