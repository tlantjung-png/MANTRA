"""JSONL logger: append-only, never raises."""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Any

from mantra.interfaces.logger import Logger


class JsonlLogger(Logger):
    """One JSON per line; never raises."""

    def __init__(self, path: str) -> None:
        self.path = path
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._lock = threading.Lock()

    def log(self, event: str, payload: dict[str, Any]) -> None:
        # Payload spreads last, so it can override ts/event; default=str
        # stringifies values that JSON cannot serialize.
        record = {"ts": round(time.time(), 3), "event": event, **payload}
        line = json.dumps(record, default=str) + "\n"
        # Inter-process lock to avoid interleaved lines. Use atomic exclusive
        # create as the arbiter; the holder's pid is embedded so a stale
        # lock is only ever broken by a different process — a process can
        # never delete a lock one of its own threads just created.
        lock_path = self.path + ".lock"

        def _lock_holder_pid() -> int | None:
            try:
                with open(lock_path, "r", encoding="utf-8") as handle:
                    return int(handle.read().strip() or 0)
            except (OSError, ValueError):
                return None

        def _break_stale() -> None:
            try:
                stat = os.stat(lock_path)
                if time.time() - stat.st_mtime < 5.0:
                    return
                pid = _lock_holder_pid()
                if pid == os.getpid():
                    # Our own process may hold the lock on another thread.
                    return
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
            # record rather than risk interleaved, corrupt lines. The
            # logger never raises; losing one record under contention is
            # the documented trade.
            return
        try:
            with self._lock:
                try:
                    with open(self.path, "a", encoding="utf-8") as handle:
                        handle.write(line)
                except OSError:
                    pass
        finally:
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
        pass
