"""Windows pseudoconsole harness: drive the real console application.

Spawns the console in a true Windows console session of a known size,
injects keystrokes and SGR mouse reports (the exact stream a terminal
sends), and records everything the application paints so tests can
assert on it. Skips automatically on non-Windows.

Two transports, selected at import time:

- ``CreatePseudoConsole`` (the documented API) is probed once with a
  tiny child. On healthy hosts it is used directly.
- ``conhost.exe --headless -- <child>`` is the fallback. Some hosts
  (certain headless services, sandboxed CI agents) create
  pseudoconsole sessions whose conhost never wires up: children attach
  and run, but no output ever arrives on the pipes and the session
  emits nothing even when closed. Hosting the child under conhost
  directly with pipe stdio produces the same VT stream on those hosts.

Both transports expose the same interface: ``send``/``text``/``wait_for``
for I/O, ``resize`` when the underlying session supports it, and typed
input helpers. The probe adds a few seconds of startup cost once per
process, never per test.
"""

from __future__ import annotations

import atexit
import ctypes
import os
import subprocess
import threading
import time

import pytest

if os.name != "nt":
    pytest.skip("Windows-only console harness", allow_module_level=True)

k32 = ctypes.WinDLL("kernel32", use_last_error=True)

PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE = 0x00020016
EXTENDED_STARTUPINFO_PRESENT = 0x00080000
STARTF_USESTDHANDLES = 0x00000100
HANDLE_FLAG_INHERIT = 0x0001

# All kernel32 calls below carry explicit argtypes/restype: without them
# ctypes converts wide strings and pointers incorrectly and the child
# process never runs (silent exit 1, no stderr through the ConPTY).
BOOL = ctypes.c_int
DWORD = ctypes.c_ulong
HANDLE = ctypes.c_void_p
LPVOID = ctypes.c_void_p
LPCWSTR = ctypes.c_wchar_p
LPWSTR = ctypes.c_wchar_p
LONG = ctypes.c_long


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

    def set_inherit(self, inheritable: bool) -> None:
        flag = HANDLE_FLAG_INHERIT if inheritable else 0
        k32.SetHandleInformation(self.h_read, HANDLE_FLAG_INHERIT, flag)
        k32.SetHandleInformation(self.h_write, HANDLE_FLAG_INHERIT, flag)

    def close_read(self) -> None:
        if self.h_read:
            k32.CloseHandle(self.h_read)
            self.h_read = ctypes.c_void_p()

    def close_write(self) -> None:
        if self.h_write:
            k32.CloseHandle(self.h_write)
            self.h_write = ctypes.c_void_p()

    def close(self) -> None:
        self.close_read()
        self.close_write()


k32.CreatePipe.argtypes = [ctypes.POINTER(HANDLE), ctypes.POINTER(HANDLE), ctypes.POINTER(_SecurityAttributes), DWORD]
k32.CreatePipe.restype = BOOL
k32.WriteFile.argtypes = [HANDLE, LPVOID, DWORD, ctypes.POINTER(DWORD), LPVOID]
k32.WriteFile.restype = BOOL
k32.ReadFile.argtypes = [HANDLE, LPVOID, DWORD, ctypes.POINTER(DWORD), LPVOID]
k32.ReadFile.restype = BOOL
k32.PeekNamedPipe.argtypes = [HANDLE, LPVOID, DWORD, LPVOID, ctypes.POINTER(DWORD), LPVOID]
k32.PeekNamedPipe.restype = BOOL
k32.CloseHandle.argtypes = [HANDLE]
k32.CloseHandle.restype = BOOL
k32.GetStdHandle.argtypes = [DWORD]
k32.GetStdHandle.restype = HANDLE
k32.GetConsoleMode.argtypes = [HANDLE, ctypes.POINTER(DWORD)]
k32.GetConsoleMode.restype = BOOL
k32.SetConsoleMode.argtypes = [HANDLE, DWORD]
k32.SetConsoleMode.restype = BOOL
k32.GetExitCodeProcess.argtypes = [HANDLE, ctypes.POINTER(DWORD)]
k32.GetExitCodeProcess.restype = BOOL
k32.SetHandleInformation.argtypes = [HANDLE, DWORD, DWORD]
k32.SetHandleInformation.restype = BOOL
k32.WaitForSingleObject.argtypes = [HANDLE, DWORD]
k32.WaitForSingleObject.restype = DWORD
k32.TerminateProcess.argtypes = [HANDLE, DWORD]
k32.TerminateProcess.restype = BOOL
k32.InitializeProcThreadAttributeList.argtypes = [LPVOID, DWORD, DWORD, ctypes.POINTER(ctypes.c_size_t)]
k32.InitializeProcThreadAttributeList.restype = BOOL
k32.UpdateProcThreadAttribute.argtypes = [LPVOID, DWORD, DWORD, LPVOID, ctypes.c_size_t, LPVOID, LPVOID]
k32.UpdateProcThreadAttribute.restype = BOOL
k32.DeleteProcThreadAttributeList.argtypes = [LPVOID]
k32.DeleteProcThreadAttributeList.restype = None
k32.CreatePseudoConsole.restype = ctypes.c_long  # HRESULT: 0 is S_OK, negative means failure
k32.CreatePseudoConsole.argtypes = [COORD, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(ctypes.c_void_p)]
k32.ResizePseudoConsole.argtypes = [ctypes.c_void_p, COORD]
k32.ResizePseudoConsole.restype = ctypes.c_long
k32.ClosePseudoConsole.argtypes = [ctypes.c_void_p]


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


k32.CreateProcessW.argtypes = [
    LPCWSTR, LPWSTR, LPVOID, LPVOID, BOOL, DWORD, LPVOID, LPCWSTR,
    LPVOID, LPVOID,  # _StartupInfoEx (ConPTY) or _StartupInfo (headless); _ProcessInfo
]
k32.CreateProcessW.restype = BOOL

_CONHOST = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "System32", "conhost.exe")


def _spawn_pseudoconsole(cols: int, rows: int, cmd: str, cwd: str,
                         pty_in: "_Pipe", pty_out: "_Pipe") -> tuple[ctypes.c_void_p, _ProcessInfo]:
    """Create a ConPTY session and the child attached to it."""
    hpc = ctypes.c_void_p()
    hr = k32.CreatePseudoConsole(COORD(cols, rows), pty_in.h_read, pty_out.h_write, 0, ctypes.byref(hpc))
    if hr < 0:  # HRESULT: S_OK (0) means created, negative means failure
        raise OSError(f"CreatePseudoConsole failed: 0x{hr & 0xFFFFFFFF:08X}")

    attr_size = ctypes.c_size_t()
    k32.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(attr_size))
    attr = ctypes.create_string_buffer(attr_size.value)
    if not k32.InitializeProcThreadAttributeList(attr, 1, 0, ctypes.byref(attr_size)):
        k32.ClosePseudoConsole(hpc)
        raise OSError("InitializeProcThreadAttributeList failed")
    if not k32.UpdateProcThreadAttribute(
        attr, 0, PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE,
        ctypes.byref(hpc), ctypes.sizeof(ctypes.c_void_p), None, None,
    ):
        k32.DeleteProcThreadAttributeList(attr)
        k32.ClosePseudoConsole(hpc)
        raise OSError("UpdateProcThreadAttribute failed")

    si = _StartupInfoEx()
    si.StartupInfo.cb = ctypes.sizeof(_StartupInfoEx)
    si.lpAttributeList = ctypes.cast(attr, ctypes.c_void_p)
    pi = _ProcessInfo()
    cmd_buffer = ctypes.create_unicode_buffer(cmd)
    # bInheritHandles must be TRUE: the pseudoconsole's internal
    # handles reach the child only through handle inheritance.
    ok = k32.CreateProcessW(
        None, cmd_buffer, None, None, True,
        EXTENDED_STARTUPINFO_PRESENT, None, cwd,
        ctypes.byref(si), ctypes.byref(pi),
    )
    k32.DeleteProcThreadAttributeList(attr)
    if not ok:
        k32.ClosePseudoConsole(hpc)
        raise OSError(f"CreateProcess failed: {ctypes.get_last_error()}")
    return hpc, pi


def _spawn_conhost_headless(cols: int, rows: int, cmd: str, cwd: str,
                            pty_in: "_Pipe", pty_out: "_Pipe") -> tuple[None, _ProcessInfo]:
    """Host the child under conhost --headless with pipe stdio.

    Equivalent transport to ConPTY on hosts where the pseudoconsole
    session never wires up. The console size is conveyed through the
    child's terminal queries; the fixed buffer size only bounds the
    render, it does not affect what the child writes.
    """
    # The parent's ends must not leak into the child: only conhost and
    # its client see the child-side ends.
    pty_in.set_inherit(False)
    pty_out.set_inherit(False)
    k32.SetHandleInformation(pty_in.h_read, HANDLE_FLAG_INHERIT, HANDLE_FLAG_INHERIT)
    k32.SetHandleInformation(pty_out.h_write, HANDLE_FLAG_INHERIT, HANDLE_FLAG_INHERIT)

    si = _StartupInfo()
    si.cb = ctypes.sizeof(_StartupInfo)
    si.dwFlags = STARTF_USESTDHANDLES
    si.hStdInput = pty_in.h_read
    si.hStdOutput = pty_out.h_write
    si.hStdError = pty_out.h_write
    pi = _ProcessInfo()
    wrapped = subprocess.list2cmdline([_CONHOST, "--headless", "--"]) + " " + cmd
    cmd_buffer = ctypes.create_unicode_buffer(wrapped)
    ok = k32.CreateProcessW(
        None, cmd_buffer, None, None, True,
        STARTF_USESTDHANDLES, None, cwd,
        ctypes.byref(si), ctypes.byref(pi),
    )
    # Restore parent-end non-inheritance so later children cannot
    # accidentally inherit these handles.
    pty_in.set_inherit(False)
    pty_out.set_inherit(False)
    if not ok:
        raise OSError(f"CreateProcess(conhost --headless) failed: {ctypes.get_last_error()}")
    return None, pi


def _probe_pseudoconsole_works() -> bool:
    """True when a CreatePseudoConsole session delivers child output.

    Spawns a throwaway interpreter that prints one marker and exits.
    A broken host (conhost spawned but never wired) yields zero bytes,
    which is exactly the failure mode the fallback exists for.
    """
    try:
        pty_in, pty_out = _Pipe(), _Pipe()
    except OSError:
        return False
    hpc = None
    proc = None
    try:
        marker_cmd = subprocess.list2cmdline([
            sys_executable(), "-c", "print('PTY_PROBE_OK'); import time; time.sleep(1)"
        ])
        hpc, proc = _spawn_pseudoconsole(80, 24, marker_cmd, os.getcwd(), pty_in, pty_out)
        buf = ctypes.create_string_buffer(4096)
        got = ctypes.c_ulong()
        deadline = time.monotonic() + 8.0
        # Peek, then read only what is available: a blocking ReadFile here
        # would hang forever on exactly the broken hosts this probe exists
        # to detect (no data, no EOF, session alive).
        while time.monotonic() < deadline:
            avail = ctypes.c_ulong(0)
            if k32.PeekNamedPipe(pty_out.h_read, None, 0, None, ctypes.byref(avail), None) and avail.value:
                if k32.ReadFile(pty_out.h_read, buf, min(avail.value, 4096), ctypes.byref(got), None) and got.value:
                    if b"PTY_PROBE_OK" in buf.raw[: got.value]:
                        return True
            else:
                code = ctypes.c_ulong(0)
                k32.GetExitCodeProcess(proc.hProcess, ctypes.byref(code))
                if code.value != 259:  # STILL_ACTIVE: child exited, nothing delivered
                    return False
            time.sleep(0.05)
        return False
    except OSError:
        return False
    finally:
        if proc is not None:
            k32.TerminateProcess(proc.hProcess, 1)
            k32.WaitForSingleObject(proc.hProcess, 2000)
            k32.CloseHandle(proc.hThread)
            k32.CloseHandle(proc.hProcess)
        if hpc is not None:
            try:
                k32.ClosePseudoConsole(hpc)
            except Exception:
                pass
        pty_in.close()
        pty_out.close()

class _ConsoleSession:
    """A child console session of a known size, driven by tests."""

    _transport: str = "pseudoconsole"

    def __init__(self, cols: int = 100, rows: int = 30, cwd: str | None = None,
                 args: list[str] | None = None) -> None:
        self.cols, self.rows = cols, rows
        self._in = _Pipe()
        self._out = _Pipe()
        cmd = subprocess.list2cmdline(args or [sys_executable(), "-m", "core.console"])
        cwd = cwd or os.getcwd()
        if self._transport == "pseudoconsole":
            self._hpc, self._pi = _spawn_pseudoconsole(cols, rows, cmd, cwd, self._in, self._out)
        else:
            self._hpc, self._pi = _spawn_conhost_headless(cols, rows, cmd, cwd, self._in, self._out)
        self.pid = self._pi.dwProcessId
        self._hprocess = self._pi.hProcess
        self._hthread = self._pi.hThread

        self.output: list[str] = []
        self._lock = threading.Lock()
        self._alive = True
        self._reader = threading.Thread(target=self._pump, daemon=True)
        self._reader.start()

    # I/O

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
        if self._hpc is not None:
            k32.ResizePseudoConsole(self._hpc, COORD(cols, rows))
        # The headless transport has no size handle: resize is a no-op
        # there. Tests that assert on wrapping must use the probed
        # default size.

    # Input helpers: what a terminal sends.

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

    # Teardown.

    def close(self) -> None:
        self._alive = False
        # Reap the child before destroying the session: closing a
        # console while its client is still attached can deadlock. Wait
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
        self._out.close_write()
        self._reader.join(timeout=2.0)
        self._in.close()
        self._out.close()


# Alias kept so existing imports (`from conpty_harness import ConPTY`)
# keep working after the transport split.
ConPTY = _ConsoleSession


def sys_executable() -> str:
    import sys

    return sys.executable


# ── transport selection (once per process) ────────────────────
# The probe costs a few seconds on the first import. Results are
# deterministic per host, so one probe serves the whole run.
if os.name == "nt":
    if _probe_pseudoconsole_works():
        _ConsoleSession._transport = "pseudoconsole"
    else:
        _ConsoleSession._transport = "conhost-headless"


_POOL: list[_ConsoleSession] = []


def make_console(**kwargs) -> _ConsoleSession:
    pty = _ConsoleSession(**kwargs)
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
