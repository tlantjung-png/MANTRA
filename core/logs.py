"""JSONL logger: append-only and never raises.

Redaction is enforced at the sink: string values in every payload pass
through the shared helper before serialization. Caller-side redaction stays
as defence in depth. Records are dropped, and counted, when the
inter-process lock cannot be taken or a write fails.
"""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Any

from core.agent.approvals import _redact_sensitive
from core.types import Logger

# Rotate the log aside once it passes ~1 MB; the previous file is kept
# at ``<path>.1`` and replaced on the next rotation.
_ROTATE_BYTES = 1_000_000


def _redact_payload(value: Any) -> Any:
    """Apply the shared redactor to string values at any nesting depth."""
    if isinstance(value, str):
        return _redact_sensitive(value)
    if isinstance(value, dict):
        return {key: _redact_payload(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_payload(item) for item in value]
    return value

# Grace period before a lock is probed for staleness, and a hard ceiling
# past which it is removed even if the holder looks alive (no write holds it
# that long, so an alive pid there is pid-reuse or a wedged writer).
_LOCK_GRACE_SECONDS = 5.0
_LOCK_HARD_CEILING_SECONDS = 60.0

if os.name == "nt":  # pragma: no cover - Windows-only
    import ctypes

    # use_last_error is required: without it the not-found probe can read
    # a stale error and misreport a dead process as alive.
    _KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True)


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
        if os.name == "nt":  # pragma: no cover - Windows-only
            import ctypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            handle = _KERNEL32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, pid
            )
            if handle:
                _KERNEL32.CloseHandle(handle)
                return True
            # Access-denied means the process exists but is protected or
            # owned by another user: treat it as alive. Only a not-found
            # probe (invalid parameter, error 87) counts as dead.
            return ctypes.get_last_error() != 87
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, just not ours to signal
    except OSError:
        return False  # probe unavailable: mtime rule decides


class JsonlLogger(Logger):
    """One JSON per line; never raises.

    Contract: one writer per log file. The inter-process lock also
    tolerates a second writer, but a rotation by one process replaces the
    file while the other's handle keeps appending to the old inode; the
    inode check in ``_ensure_handle`` reopens the new file on the next
    write.
    """

    def __init__(self, path: str) -> None:
        self.path = path
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._lock = threading.Lock()
        self._handle: Any = None
        # Records skipped because the inter-process lock could not be
        # acquired in time, or a write failed. The logger never raises,
        # so this counter is the only visibility into silent loss.
        self._dropped = 0
        # mtime of the lock this process holds, or None. A lock with our pid
        # is released within the write; one still present with the same mtime
        # past the grace period is a crash leftover and can be self-healed.
        self._held_lock_mtime: float | None = None

    def _ensure_handle(self) -> Any:
        """Lazily opened append handle; reopened after a rotation."""
        if self._handle is None:
            self._handle = open(self.path, "a", encoding="utf-8")
            return self._handle
        try:
            current_ino = os.stat(self.path).st_ino
            handle_ino = os.fstat(self._handle.fileno()).st_ino
        except OSError:
            return self._handle
        if current_ino != handle_ino:
            # Another process rotated the log aside while our handle was
            # open; records would land on the unlinked inode, so reopen
            # the file that now carries the log name.
            try:
                self._handle.close()
            except OSError:
                pass
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
        # Redact at the sink so a caller that forgets cannot persist a
        # credential. Payload spreads first so ts/event cannot be spoofed;
        # default=str stringifies values that JSON cannot serialize.
        record = {**_redact_payload(payload), "ts": round(time.time(), 3), "event": event}
        line = json.dumps(record, default=str) + "\n"
        # Inter-process lock avoids interleaved lines: atomic exclusive create
        # is the arbiter, with the holder's pid embedded so a stale lock is
        # broken only when its holder is gone — not while a live process pauses.
        lock_path = self.path + ".lock"

        def _lock_holder_pid() -> int | None:
            try:
                with open(lock_path, "r", encoding="utf-8") as handle:
                    return int(handle.read().strip() or 0)
            except (OSError, ValueError):
                return None

        def _break_stale() -> None:
            """Break a lock whose holder is gone or wedged.

            mtime alone cannot tell a paused-but-live holder from a dead
            one, so the holder's pid is probed for liveness before the
            lock is removed. A lock carrying our own pid is only broken
            when it matches the one this process acquired (recorded
            mtime) and the grace period has passed: a live hold is
            released within the write below, so a still-present lock is
            a crash leftover. Past the hard ceiling the lock is removed
            unconditionally — no legitimate hold lasts that long, so an
            alive pid there is reuse, not a paused writer.
            """
            try:
                stat = os.stat(lock_path)
                age = time.time() - stat.st_mtime
                if age < _LOCK_GRACE_SECONDS:
                    return
                if age >= _LOCK_HARD_CEILING_SECONDS:
                    try:
                        stat2 = os.stat(lock_path)
                        if stat2.st_mtime != stat.st_mtime:
                            return
                        os.remove(lock_path)
                    except OSError:
                        pass
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
                    if time.time() - s.st_mtime >= _LOCK_GRACE_SECONDS:
                        _break_stale()
                except OSError:
                    pass
            except OSError:
                break
        if not acquired:
            # Another process holds the lock: skip and count the record rather
            # than risk corrupt lines. A lock with our own pid is a crash
            # leftover; leaving it drops all later records too.
            with self._lock:
                self._dropped += 1
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
                    self._dropped += 1
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

    @property
    def dropped(self) -> int:
        """Number of records skipped (lock contention or failed write)."""
        return self._dropped

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
