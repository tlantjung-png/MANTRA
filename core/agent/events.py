"""Sync event bus for hooks and observability."""

from __future__ import annotations

import sys
import threading
from typing import Any, Callable

EventHandler = Callable[[str, dict[str, Any]], None]


class EventBus:
    """Fan-out events; isolate observer errors from the emitter."""

    def __init__(self) -> None:
        self._handlers: list[EventHandler] = []
        # Subscription changes and emission can race (the TUI and the turn
        # worker both reach the bus); a snapshot under the lock keeps emit
        # deterministic and the failure count observable rather than only
        # printed to stderr.
        self._lock = threading.RLock()
        self.failures = 0

    def subscribe(self, handler: EventHandler) -> None:
        with self._lock:
            self._handlers.append(handler)

    def unsubscribe(self, handler: EventHandler) -> None:
        """Remove a handler previously added with subscribe()."""
        with self._lock:
            if handler in self._handlers:
                self._handlers.remove(handler)

    def emit(self, event: str, payload: dict[str, Any]) -> None:
        with self._lock:
            handlers = tuple(self._handlers)
        for handler in handlers:
            try:
                handler(event, payload)
            except Exception as exc:  # noqa: BLE001 - observer isolation
                # One broken observer must not kill the fan-out; report it
                # to stderr and count it so a metric can be exposed.
                with self._lock:
                    self.failures += 1
                name = getattr(handler, "__name__", repr(handler))
                print(
                    f"[events] handler {name} failed on {event!r}: {str(exc)[:200]}",
                    file=sys.stderr,
                )
