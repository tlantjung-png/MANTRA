"""Shared process helpers: spawn-group flags, pipe reader, tree kill."""

from __future__ import annotations

import os
import subprocess
import threading
import time

import pytest

from core import procutil


def test_popen_group_kwargs_match_platform() -> None:
    """One platform-appropriate flag set with exactly the expected keys."""
    if os.name == "nt":
        assert procutil.POPEN_GROUP_KWARGS == {
            "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP
        }
    else:
        assert procutil.POPEN_GROUP_KWARGS == {"start_new_session": True}


def test_read_pipe_into_accumulates_until_eof(tmp_path) -> None:
    done = threading.Event()
    buf = bytearray()
    source = tmp_path / "stream.bin"
    source.write_bytes(b"hello world")
    with open(source, "rb") as stream:
        procutil.read_pipe_into(stream, buf, 1_000_000, done)
    assert bytes(buf) == b"hello world"
    assert done.is_set()


def test_read_pipe_into_caps_at_budget() -> None:
    """A chatty stream stops filling at the cap; done is always signalled."""

    class _ChattyStream:
        """A stream that ignores the read-size hint (like a bad pipe)."""

        def __init__(self) -> None:
            self.remaining = 10

        def read(self, size: int) -> bytes:
            if self.remaining <= 0:
                return b""
            self.remaining -= 1
            chunk = b"x" * 4096
            if size < len(chunk):
                return chunk[:size]
            return chunk

    done = threading.Event()
    buf = bytearray()
    procutil.read_pipe_into(_ChattyStream(), buf, 10_000, done)
    assert len(buf) <= 10_000
    assert done.is_set()


def test_read_pipe_into_done_set_on_stream_error() -> None:
    """A broken pipe must not hang the pump thread: done still fires."""

    class _BrokenStream:
        def read(self, size: int) -> bytes:
            raise OSError("broken pipe")

    done = threading.Event()
    buf = bytearray()
    procutil.read_pipe_into(_BrokenStream(), buf, 1_000, done)
    assert done.is_set()
    assert buf == bytearray()


def _spawn_sleeper() -> subprocess.Popen:
    """A long-lived child in its own process group (python is always present)."""
    if os.name == "nt":
        return subprocess.Popen(
            ["python", "-c", "import time; time.sleep(30)"],
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    return subprocess.Popen(
        ["sleep", "30"],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def test_kill_process_tree_terminates_child() -> None:
    """After the kill, the child is reaped; Popen.poll is the authority.

    An OS-level pid probe is deliberately avoided: on Windows a fast-exiting
    child's pid can be reused within the wait window and report false-alive.
    """
    proc = _spawn_sleeper()
    try:
        assert proc.poll() is None  # alive before the kill
    finally:
        procutil.kill_process_tree(proc)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and proc.poll() is None:
        time.sleep(0.05)
    assert proc.poll() is not None


def test_kill_process_tree_survives_bad_handle() -> None:
    """A reaped or bogus pid must be survivable: best effort, no raise."""

    class _DeadProc:
        pid = 999_999_999

        def kill(self) -> None:
            raise OSError("already reaped")

        def wait(self, timeout: float) -> None:
            raise subprocess.TimeoutExpired("dead", timeout)

    procutil.kill_process_tree(_DeadProc())  # must not raise
