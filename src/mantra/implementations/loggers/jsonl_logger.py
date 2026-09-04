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
        # create as the arbiter; stale handling verifies mtime to avoid
        # deleting a lock that was just created.
        lock_path = self.path + ".lock"
        try:
            stat = os.stat(lock_path)
            if time.time() - stat.st_mtime >= 5.0:
                try:
                    stat2 = os.stat(lock_path)
                    if stat2.st_mtime == stat.st_mtime:
                        os.remove(lock_path)
                except OSError:
                    pass
        except OSError:
            pass
        acquired = False
        fd = None
        start = time.monotonic()
        while time.monotonic() - start < 0.5:
            try:
                fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                acquired = True
                break
            except FileExistsError:
                time.sleep(0.02)
                try:
                    s = os.stat(lock_path)
                    if time.time() - s.st_mtime >= 5.0:
                        # Verify mtime unchanged before removal
                        try:
                            s2 = os.stat(lock_path)
                            if s2.st_mtime == s.st_mtime:
                                os.remove(lock_path)
                        except OSError:
                            pass
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
