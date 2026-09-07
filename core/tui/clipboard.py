"""Clipboard routes: native OS chains plus an escape-sequence fallback.

Copy tries, in order: the platform's own clipboard tooling (PowerShell or
the windowing API on Windows; pbcopy/xclip/xsel on POSIX) and finally the
OSC 52 escape, which travels through SSH/tmux/containers when no local
clipboard exists.
"""

from __future__ import annotations

import base64
import os


def copy_text(text: str) -> None:
    if not text:
        return
    if os.name == "nt":
        if _copy_windows(text):
            return
    else:
        if _copy_posix(text):
            return
    _copy_osc52(text)


def paste_text() -> str:
    if os.name == "nt":
        return _paste_windows()
    return _paste_posix()


# ── native: Windows ───────────────────────────────────────────────


def _copy_windows(text: str) -> bool:
    import subprocess

    try:
        p = subprocess.Popen(
            ["clip"], stdin=subprocess.PIPE, text=True, encoding="utf-8", errors="replace"
        )
        p.communicate(text, timeout=2)
        if p.returncode == 0:
            return True
    except (OSError, subprocess.SubprocessError):
        # clip.exe missing or timed out; the Win32 chain is tried next.
        pass
    try:
        import ctypes

        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        if not user32.OpenClipboard(0):
            return False
        try:
            if not user32.EmptyClipboard():
                return False
            data = text.encode("utf-16-le") + b"\x00\x00"
            kernel32.GlobalAlloc.restype = ctypes.c_void_p
            kernel32.GlobalLock.restype = ctypes.c_void_p
            kernel32.GlobalFree.restype = ctypes.c_void_p
            handle = kernel32.GlobalAlloc(0x0002, len(data))
            if not handle:
                return False
            ptr = kernel32.GlobalLock(handle)
            if not ptr:
                kernel32.GlobalFree(handle)
                return False
            try:
                ctypes.memmove(ptr, data, len(data))
            finally:
                kernel32.GlobalUnlock(handle)
            if not user32.SetClipboardData(13, handle):  # CF_UNICODETEXT
                kernel32.GlobalFree(handle)
                return False
            return True
        finally:
            user32.CloseClipboard()
    except Exception:
        # ctypes/Win32 surface is heterogeneous (AttributeError on exotic
        # builds, OSError on denied access); any failure just means this
        # route lost, and the OSC 52 fallback still runs.
        return False


def _paste_windows() -> str:
    import subprocess

    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-command", "Get-Clipboard"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=3,
        )
        if out.returncode == 0 and out.stdout:
            txt = out.stdout
            if txt.endswith("\r\n"):
                txt = txt[:-2]
            elif txt.endswith("\n"):
                txt = txt[:-1]
            if txt:
                return txt
    except (OSError, subprocess.SubprocessError):
        # PowerShell missing or timed out; the Win32 chain is tried next.
        pass
    try:
        import ctypes

        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        if not user32.OpenClipboard(0):
            return ""
        try:
            handle = user32.GetClipboardData(13)
            if not handle:
                return ""
            kernel32.GlobalLock.restype = ctypes.c_void_p
            ptr = kernel32.GlobalLock(handle)
            if not ptr:
                return ""
            try:
                return ctypes.wstring_at(ptr) or ""
            finally:
                kernel32.GlobalUnlock(handle)
        finally:
            user32.CloseClipboard()
    except Exception:
        # Same heterogeneity note as the copy chain; an empty string is
        # the "no clipboard" answer the composer already tolerates.
        return ""


# ── native: POSIX ─────────────────────────────────────────────────


def _copy_posix(text: str) -> bool:
    import subprocess

    for cmd in (["pbcopy"], ["xclip", "-selection", "clipboard"], ["xsel", "-b", "-i"]):
        p = None
        try:
            p = subprocess.Popen(cmd, stdin=subprocess.PIPE)
            p.communicate(text.encode("utf-8"), timeout=1)
            if p.returncode == 0:
                return True
        except subprocess.TimeoutExpired:
            if p is not None:
                p.kill()
                p.communicate()
        except (OSError, subprocess.SubprocessError):
            continue  # tool not installed; try the next one
    return False


def _paste_posix() -> str:
    import subprocess

    for cmd in (["pbpaste"], ["xclip", "-o", "-selection", "clipboard"], ["xsel", "-b", "-o"]):
        try:
            out = subprocess.run(cmd, capture_output=True, timeout=1)
            if out.returncode == 0 and out.stdout:
                return out.stdout.decode("utf-8", errors="replace")
        except (OSError, subprocess.SubprocessError):
            continue  # tool not installed; try the next one
    return ""


# ── portable fallback ─────────────────────────────────────────────


def _copy_osc52(text: str) -> None:
    """Ask the terminal itself to set its clipboard.

    Many terminals honour this only for the local session or require an
    explicit opt-in, so it is the last route tried, never the first.
    """
    payload = base64.b64encode(text.encode("utf-8")).decode("ascii")
    from core.term import safe_write

    safe_write(f"\033]52;c;{payload}\x07")
