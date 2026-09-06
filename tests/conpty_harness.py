"""Windows pseudoconsole harness: drive the real console application.

Spawns the console in a true ConPTY of a known size, injects keystrokes
and SGR mouse reports (the exact stream a terminal sends), and records
everything the application paints so tests can assert on it. Skips
automatically on non-Windows.
"""

from __future__ import annotations

import atexit
import ctypes
import os
import threading
import time

import pytest

if os.name != "nt":
    pytest.skip("Windows-only console harness", allow_module_level=True)

k32 = ctypes.WinDLL("kernel32", use_last_error=True)

PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE = 0x00020016
EXTENDED_STARTUPINFO_PRESENT = 0x00080000


class COORD(ctypes.Structure):
    _fields_ = [("X", ctypes.c_short), ("Y", ctypes.c_short)]


class _SmallRect(ctypes.Structure):
    _fields_ = [
        ("Left", ctypes.c_short), ("Top", ctypes.c_short),
        ("Right", ctypes.c_short), ("Bottom", ctypes.c_short),
    ]


class _SecurityAttributes(ctypes.Structure):
    _fields_ = [
        ("nLength", ctypes.c_ulong),
        ("lpSecurityDescriptor", ctypes.c_void_p),
        ("bInheritHandle", ctypes.c_int),
    ]


class _Pipe:
    def __init__(self) -> None:
        r = ctypes.c_void_p()
        w = ctypes.c_void_p()
        sa = _SecurityAttributes()
        sa.nLength = ctypes.sizeof(_SecurityAttributes)
        sa.lpSecurityDescriptor = None
        sa.bInheritHandle = 1  # inheritable so the pseudoconsole can pass them on
        if not k32.CreatePipe(ctypes.byref(r), ctypes.byref(w), ctypes.byref(sa), 0):
            raise OSError(f"CreatePipe failed: {ctypes.get_last_error()}")
        self.h_read, self.h_write = r, w

    def write(self, data: bytes) -> None:
        written = ctypes.c_ulong()
        k32.WriteFile(self.h_write, data, len(data), ctypes.byref(written), None)

    def close(self) -> None:
        for h in (self.h_read, self.h_write):
            if h:
                k32.CloseHandle(h)


k32.CreatePseudoConsole.restype = ctypes.c_long  # HRESULT: 0 is S_OK, negative means failure
k32.CreatePseudoConsole.argtypes = [COORD, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(ctypes.c_void_p)]
k32.ResizePseudoConsole.argtypes = [ctypes.c_void_p, COORD]
k32.ClosePseudoConsole.argtypes = [ctypes.c_void_p]
k32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
k32.CloseHandle.argtypes = [ctypes.c_void_p]
k32.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_ulong]


class _StartupInfo(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.c_ulong),
        ("lpReserved", ctypes.c_wchar_p),
        ("lpDesktop", ctypes.c_wchar_p),
        ("lpTitle", ctypes.c_wchar_p),
        ("dwX", ctypes.c_ulong),
        ("dwY", ctypes.c_ulong),
        ("dwXSize", ctypes.c_ulong),
        ("dwYSize", ctypes.c_ulong),
        ("dwXCountChars", ctypes.c_ulong),
        ("dwYCountChars", ctypes.c_ulong),
        ("dwFillAttribute", ctypes.c_ulong),
        ("dwFlags", ctypes.c_ulong),
        ("wShowWindow", ctypes.c_ushort),
        ("cbReserved2", ctypes.c_ushort),
        ("lpReserved2", ctypes.c_void_p),
        ("hStdInput", ctypes.c_void_p),
        ("hStdOutput", ctypes.c_void_p),
        ("hStdError", ctypes.c_void_p),
    ]


class _StartupInfoEx(ctypes.Structure):
    _fields_ = [("StartupInfo", _StartupInfo), ("lpAttributeList", ctypes.c_void_p)]


class _ProcessInfo(ctypes.Structure):
    _fields_ = [
        ("hProcess", ctypes.c_void_p), ("hThread", ctypes.c_void_p),
        ("dwProcessId", ctypes.c_ulong), ("dwThreadId", ctypes.c_ulong),
    ]


class ConPTY:
    """A child console application of a known size, driven by tests."""

    def __init__(self, cols: int = 100, rows: int = 30, cwd: str | None = None,
                 args: list[str] | None = None) -> None:
        self.cols, self.rows = cols, rows
        self._in = _Pipe()
        self._out = _Pipe()
        self._hpc = ctypes.c_void_p()
        hr = k32.CreatePseudoConsole(
            COORD(cols, rows), self._in.h_read, self._out.h_write, 0, ctypes.byref(self._hpc)
        )
        if hr < 0:  # HRESULT: S_OK (0) means created, negative means failure
            raise OSError(f"CreatePseudoConsole failed: 0x{hr & 0xFFFFFFFF:08X}")

        attr_size = ctypes.c_size_t()
        k32.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(attr_size))
        attr = ctypes.create_string_buffer(attr_size.value)
        if not k32.InitializeProcThreadAttributeList(attr, 1, 0, ctypes.byref(attr_size)):
            raise OSError("InitializeProcThreadAttributeList failed")
        if not k32.UpdateProcThreadAttribute(
            attr, 0, PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE,
            ctypes.byref(self._hpc), ctypes.sizeof(ctypes.c_void_p), None, None,
        ):
            raise OSError("UpdateProcThreadAttribute failed")

        si = _StartupInfoEx()
        si.StartupInfo.cb = ctypes.sizeof(_StartupInfoEx)
        si.lpAttributeList = ctypes.cast(attr, ctypes.c_void_p)
        pi = _ProcessInfo()
        cmd = " ".join(args or [sys_executable(), "-m", "core.console"])
        self._cmd_buffer = ctypes.create_unicode_buffer(cmd)
        # bInheritHandles must be TRUE: the pseudoconsole's internal
        # handles reach the child only through handle inheritance.
        ok = k32.CreateProcessW(
            None, self._cmd_buffer, None, None, True,
            EXTENDED_STARTUPINFO_PRESENT, attr, cwd or os.getcwd(),
            ctypes.byref(si.StartupInfo), ctypes.byref(pi),
        )
        k32.DeleteProcThreadAttributeList(attr)
        if not ok:
            raise OSError(f"CreateProcess failed: {ctypes.get_last_error()}")
        self.pid = pi.dwProcessId
        self._hprocess = pi.hProcess
        self._hthread = pi.hThread

        self.output: list[str] = []
        self._lock = threading.Lock()
        self._alive = True
        self._reader = threading.Thread(target=self._pump, daemon=True)
        self._reader.start()

    # ── I/O ───────────────────────────────────────────────────

    def _pump(self) -> None:
        buf = ctypes.create_string_buffer(65536)
        got = ctypes.c_ulong()
        while self._alive:
            ok = k32.ReadFile(self._out.h_read, buf, 65536, ctypes.byref(got), None)
            if not ok or not got.value:
                break
            with self._lock:
                self.output.append(buf.raw[: got.value].decode("utf-8", errors="replace"))

    def send(self, data: str) -> None:
        self._in.write(data.encode("utf-8"))

    def text(self) -> str:
        with self._lock:
            return "".join(self.output)

    def wait_for(self, needle: str, timeout: float = 10.0) -> str:
        deadline = time.monotonic() + timeout
        seen = ""
        while time.monotonic() < deadline:
            seen = self.text()
            if needle in seen:
                return seen
            time.sleep(0.05)
        raise AssertionError(
            f"did not see {needle!r} within {timeout}s; tail:\n{seen[-1500:]}"
        )

    def resize(self, cols: int, rows: int) -> None:
        self.cols, self.rows = cols, rows
        k32.ResizePseudoConsole(self._hpc, COORD(cols, rows))

    # ── input helpers (what a terminal sends) ─────────────────

    def type_text(self, text: str) -> None:
        self.send(text)

    def enter(self) -> None:
        self.send("\r")

    def mouse(self, button: int, col: int, row: int, press: bool) -> None:
        self.send(f"\x1b[<{button};{col};{row}{'M' if press else 'm'}")

    def drag(self, points: list[tuple[int, int]], button: int = 0) -> None:
        first = points[0]
        self.mouse(button, first[0], first[1], True)
        for x, y in points[1:]:
            # SGR motion reports carry button bit 32; the backend decodes
            # those as drag events (contract pinned in test_tui.py).
            self.mouse(button | 32, x, y, True)
        last = points[-1]
        self.mouse(button, last[0], last[1], False)

    def wheel(self, direction: int, col: int, row: int) -> None:
        self.send(f"\x1b[<{direction};{col};{row}M")

    # ── teardown ──────────────────────────────────────────────

    def close(self) -> None:
        self._alive = False
        # Reap the child before destroying the pseudoconsole: closing a
        # ConPTY while its client is still attached can deadlock. Wait
        # briefly for exit, force-terminate a stuck client, then release
        # the process and thread handles so no zombie or open handle
        # outlives the harness. Idempotent: close_all() may call this
        # again, and the second pass must not re-close stale handles.
        if self._hprocess:
            if k32.WaitForSingleObject(self._hprocess, 5000) == 0x102:  # WAIT_TIMEOUT
                k32.TerminateProcess(self._hprocess, 1)
                k32.WaitForSingleObject(self._hprocess, 2000)
        if self._hpc:
            try:
                k32.ClosePseudoConsole(self._hpc)
            except Exception:
                pass
            self._hpc = None
        if self._hthread:
            k32.CloseHandle(self._hthread)
            self._hthread = None
        if self._hprocess:
            k32.CloseHandle(self._hprocess)
            self._hprocess = None
        # Close the output pipe's write end so the pump thread's blocked
        # ReadFile sees EOF; closing the read handle first would deadlock
        # (CloseHandle waits for the in-flight read to finish).
        if self._out.h_write:
            k32.CloseHandle(self._out.h_write)
            self._out.h_write = None
        self._reader.join(timeout=2.0)
        self._in.close()
        self._out.close()


def sys_executable() -> str:
    import sys

    return sys.executable


_POOL: list[ConPTY] = []


def make_console(**kwargs) -> ConPTY:
    pty = ConPTY(**kwargs)
    _POOL.append(pty)
    return pty


def close_all() -> None:
    for pty in _POOL:
        try:
            pty.close()
        except Exception:
            pass
    _POOL.clear()


atexit.register(close_all)
