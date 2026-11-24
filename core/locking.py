"""Stale inter-process lock handling shared by the file-based stores.

Settings, sessions, workspace memory, and the JSONL logger
all arbitrate writers with an exclusive-create lock file and must all
answer the same question the same way: when is an abandoned lock safe
to remove? One definition here; each store keeps its own wait/stale
thresholds because they are deliberate (interactive turn path vs. bulk
writes).
"""

from __future__ import annotations

import os
import time
from pathlib import Path


def _owner_token() -> str:
    """Identify the current user for lock ownership checks."""
    uid = getattr(os, "getuid", None)
    if uid is not None:
        return f"uid:{uid()}"
    import getpass

    return f"user:{getpass.getuser()}"


def _lock_owner(lock_path: str | Path) -> str | None:
    """Read the owner token from a lock file, if it carries one."""
    try:
        with open(lock_path, "r", encoding="utf-8") as handle:
            return handle.read().strip() or None
    except (OSError, UnicodeError):
        return None


def break_stale_lock(lock_path: str | Path, stale_seconds: float) -> bool:
    """Remove a lock whose holder is gone. True when removed.

    The mtime is checked twice so a freshly created lock is never
    deleted: a lock that ages past ``stale_seconds`` between the two
    stats is left for the next attempt. A path that is missing at the
    first stat reports False (nothing to break); one that vanishes
    between the stats reports True (another writer cleared it).
    """
    try:
        stat = os.stat(lock_path)
    except OSError:
        return False
    age = time.time() - stat.st_mtime
    if age < stale_seconds:
        return False
    # A lock carrying an explicit owner is only broken by that owner.
    # Legacy locks without a token keep the old mtime-only behaviour.
    owner = _lock_owner(lock_path)
    if owner is not None and owner.startswith("owner:") and owner != f"owner:{_owner_token()}":
        return False
    # Verify mtime hasn't changed since we checked to avoid deleting
    # a lock that was just freshly created by another process.
    try:
        stat2 = os.stat(lock_path)
        if stat2.st_mtime != stat.st_mtime:
            return False
        os.remove(lock_path)
        return True
    except FileNotFoundError:
        return True
    except OSError:
        return False


def acquire_lock(lock_path: str | Path, wait: float, stale: float) -> int | None:
    """Take an exclusive-create lock, retrying briefly on contention.

    Returns the open file descriptor, or None when the lock could not be
    taken in time (or creation failed for a non-conflict reason).
    """
    if os.path.exists(lock_path):
        break_stale_lock(lock_path, stale)
    start = time.monotonic()
    while time.monotonic() - start < wait:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            try:
                os.write(fd, f"owner:{_owner_token()}\n".encode("utf-8"))
            except OSError:
                pass  # ownership is best effort; the lock itself still holds
            return fd
        except FileExistsError:
            time.sleep(0.02)
            try:
                if time.time() - os.stat(lock_path).st_mtime >= stale:
                    break_stale_lock(lock_path, stale)
            except OSError:
                pass
        except OSError:
            return None
    return None


def release_lock(lock_path: str | Path, fd: int | None) -> None:
    """Close and remove a lock taken by :func:`acquire_lock`."""
    if fd is None:
        return
    try:
        os.close(fd)
    except OSError:
        pass
    try:
        os.remove(lock_path)
    except OSError:
        pass
