"""Shared viewport-offset math for the full-screen TUI surfaces."""

from __future__ import annotations


class ScrollOffsetMixin:
    """clamp/step for a state object that owns an ``offset`` field."""

    offset: int

    def clamp(self, total: int, viewport: int) -> None:
        """Pin the offset after a render (total comes from the renderer)."""
        max_offset = max(0, total - max(1, viewport))
        if self.offset > max_offset:
            self.offset = max_offset

    def step(self, delta: int, total: int, viewport: int) -> None:
        self.offset = max(0, min(max(0, total - max(1, viewport)), self.offset + delta))
