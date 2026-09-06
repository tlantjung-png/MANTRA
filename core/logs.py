"""JSONL logger: append-only, never raises."""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Any

from core.types import Logger

# Rotate the log aside once it passes ~1 MB; the previous file is kept
# at ``<path>.1`` and replaced on the next rotation.
_ROTATE_BYTES = 1_000_000


def _pid_alive(pid: int) -> bool:
    """True only when the pid is verified alive.

    ``os.kill(pid, 0)`` is exact on POSIX. On Windows os.kill with a
    non-zero signal terminates the target process, so existence is
    probed with OpenProcess instead; an unavailable probe reports False
    so the caller falls back to the mtime rule.
    """
    if pid <= 0:
        return False
    try:
        if os.name == "nt":
            import ctypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            handle = ctypes.windll.kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, pid
            )
            if handle:
                ctypes.windll.kernel32.CloseHandle(handle)
                return True
            return False
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, just not ours to signal
    except OSError:
        return False  # probe unavailable: mtime rule decides


class JsonlLogger(Logger):
    """One JSON per line; never raises."""

    def __init__(self, path: str) -> None:
        self.path = path
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._lock = threading.Lock()
        self._handle: Any = None
        # mtime of the inter-process lock this process holds, or None. A
        # lock carrying our own pid is released within the write below; one
        # still present with the same mtime past the grace period is a
        # leftover from a crash and can be self-healed.
        self._held_lock_mtime: float | None = None

    def _ensure_handle(self) -> Any:
        """Lazily opened append handle; reopened after a rotation."""
        if self._handle is None:
            self._handle = open(self.path, "a", encoding="utf-8")
        return self._handle

    def _rotate(self) -> None:
        """Size-based rotation, run under the inter-process lock.

        Once the log passes ~1 MB it is moved aside to ``<path>.1`` (any
        previous backup is replaced) so the log directory stays bounded.
        """
        try:
            if os.path.getsize(self.path) < _ROTATE_BYTES:
                return
        except OSError:
            return  # no file yet: nothing to rotate
        if self._handle is not None:
            try:
                self._handle.close()
            except OSError:
                pass
            self._handle = None
        try:
            os.replace(self.path, self.path + ".1")
        except OSError:
            pass

    def log(self, event: str, payload: dict[str, Any]) -> None:
        # Payload spreads last, so it can override ts/event; default=str
        # stringifies values that JSON cannot serialize.
        record = {"ts": round(time.time(), 3), "event": event, **payload}
        line = json.dumps(record, default=str) + "\n"
        # Inter-process lock to avoid interleaved lines. Use atomic exclusive
        # create as the arbiter; the holder's pid is embedded so a stale
        # lock is only ever broken when its holder is gone — never while a
        # live process merely paused past the mtime grace.
        lock_path = self.path + ".lock"

        def _lock_holder_pid() -> int | None:
            try:
                with open(lock_path, "r", encoding="utf-8") as handle:
                    return int(handle.read().strip() or 0)
            except (OSError, ValueError):
                return None

        def _break_stale() -> None:
            """Break a lock whose holder is gone.

            mtime alone cannot tell a paused-but-live holder from a dead
            one, so the holder's pid is probed for liveness before the
            lock is removed. A lock carrying our own pid is only broken
            when it matches the one this process acquired (recorded
            mtime) and the grace period has passed: a live hold is
            released within the write below, so a still-present lock is
            a crash leftover.
            """
            try:
                stat = os.stat(lock_path)
                if time.time() - stat.st_mtime < 5.0:
                    return
                pid = _lock_holder_pid()
                if pid == os.getpid():
                    if (
                        self._held_lock_mtime is not None
                        and stat.st_mtime == self._held_lock_mtime
                    ):
                        os.remove(lock_path)
                    return
                if pid is not None and pid > 0 and _pid_alive(pid):
                    return  # holder alive, just paused past the grace
                try:
                    stat2 = os.stat(lock_path)
                    if stat2.st_mtime != stat.st_mtime:
                        return
                    os.remove(lock_path)
                except OSError:
                    pass
            except OSError:
                pass

        _break_stale()
        acquired = False
        fd = None
        start = time.monotonic()
        while time.monotonic() - start < 0.5:
            try:
                fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                acquired = True
                try:
                    os.write(fd, f"{os.getpid()}\n".encode("ascii"))
                except OSError:
                    pass
                try:
                    self._held_lock_mtime = os.stat(lock_path).st_mtime
                except OSError:
                    pass
                break
            except FileExistsError:
                time.sleep(0.02)
                try:
                    s = os.stat(lock_path)
                    if time.time() - s.st_mtime >= 5.0:
                        _break_stale()
                except OSError:
                    pass
            except OSError:
                break
        if not acquired:
            # Another process holds the inter-process lock: skip this
            # record rather than risk interleaved, corrupt lines. A lock
            # recording our own pid cannot be a live hold from this
            # process — the holder removes it within the write below —
            # so it must be a crash leftover; leaving it would drop every
            # later record too.
            try:
                if _lock_holder_pid() == os.getpid():
                    os.remove(lock_path)
            except OSError:
                pass
            return
        try:
            with self._lock:
                self._rotate()
                try:
                    handle = self._ensure_handle()
                    handle.write(line)
                    handle.flush()
                except OSError:
                    pass
        finally:
            self._held_lock_mtime = None
            if acquired and fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
                try:
                    os.remove(lock_path)
                except OSError:
                    pass

    def close(self) -> None:
        """Flush and release the append handle. Idempotent; never raises."""
        with self._lock:
            if self._handle is None:
                return
            try:
                self._handle.flush()
            except OSError:
                pass
            try:
                self._handle.close()
            except OSError:
                pass
            self._handle = None
