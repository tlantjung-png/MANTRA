"""Session manager panel: autosaved sessions, status, resume.

Reuses the review-panel shape (header / list / footer, j/k navigation,
Enter to act, q/Esc to leave) for the console's saved conversations.
Pure logic: takes a state and a size, returns rows to draw; the app
draws them on the same canvas as the diff review.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from core.tui.review import ReviewRow


@dataclass
class SessionEntry:
    name: str
    workspace: str = ""
    model: str = ""
    turns: int = 0
    saved_at: str = ""
    mtime: float = 0.0
    summary: str = ""


@dataclass
class SessionPanelState:
    entries: list[SessionEntry]
    index: int = 0
    offset: int = 0

    @property
    def entry(self) -> SessionEntry | None:
        return self.entries[self.index] if self.entries else None

    def clamp(self, total: int, viewport: int) -> None:
        max_offset = max(0, total - max(1, viewport))
        if self.offset > max_offset:
            self.offset = max_offset

    def step(self, delta: int, total: int, viewport: int) -> None:
        self.offset = max(0, min(max(0, total - max(1, viewport)), self.offset + delta))

    def next(self, delta: int, total: int) -> None:
        if total:
            self.index = max(0, min(total - 1, self.index + delta))


@dataclass
class SessionFrame:
    header: str
    rows: list[ReviewRow]
    footer: str
    total: int


def render_session_panel(state: SessionPanelState, width: int, height: int) -> SessionFrame:
    """Build the drawable frame for the session list."""
    total = len(state.entries)
    viewport = max(1, height - 2)
    start = min(state.offset, max(0, total - viewport))
    visible = state.entries[start: start + viewport]

    rows: list[ReviewRow] = []
    for i, entry in enumerate(visible):
        abs_i = start + i
        sel = abs_i == state.index
        marker = "> " if sel else "  "
        ws = entry.workspace.replace("\\", "/").rstrip("/").split("/")[-1] if entry.workspace else "?"
        when = time.strftime("%m-%d %H:%M", time.localtime(entry.mtime)) if entry.mtime else entry.saved_at
        meta = f"{entry.model or '?'}  ·  {entry.turns}t  ·  {ws}  ·  {when}"
        line1 = f"{marker}{entry.name}"
        room = max(0, width - len(line1) - 2)
        if room > 0:
            line1 += "   " + meta[:room]
        rows.append(ReviewRow(line1[:width], "sel" if sel else "ctx"))
        summary = entry.summary or "(no conversation yet)"
        if len(summary) > max(0, width - 6):
            summary = summary[: max(0, width - 9)] + "..."
        rows.append(ReviewRow("   " + summary, "dim"))

    header = f"sessions ({total}) — Enter to resume"
    footer = "j/k move · PgUp/PgDn page · Enter resume · q/Esc back"
    return SessionFrame(header=header, rows=rows, footer=footer, total=total)
