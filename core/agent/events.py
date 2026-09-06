"""Sync event bus for hooks and observability."""

from __future__ import annotations

import sys
from typing import Any, Callable

EventHandler = Callable[[str, dict[str, Any]], None]


class EventBus:
    """Fan-out events; isolate observer errors from the emitter."""

    def __init__(self) -> None:
        self._handlers: list[EventHandler] = []

    def subscribe(self, handler: EventHandler) -> None:
        self._handlers.append(handler)

    def unsubscribe(self, handler: EventHandler) -> None:
        """Remove a handler previously added with subscribe()."""
        if handler in self._handlers:
            self._handlers.remove(handler)

    def emit(self, event: str, payload: dict[str, Any]) -> None:
        for handler in list(self._handlers):
            try:
                handler(event, payload)
            except Exception as exc:  # noqa: BLE001 - observer isolation
                # One broken observer must not kill the fan-out, but a
                # silently swallowed failure hides real bugs: report the
                # handler name and a bounded error snippet to stderr.
                name = getattr(handler, "__name__", repr(handler))
                print(
                    f"[events] handler {name} failed on {event!r}: {str(exc)[:200]}",
                    file=sys.stderr,
                )
