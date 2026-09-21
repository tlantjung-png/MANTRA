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
