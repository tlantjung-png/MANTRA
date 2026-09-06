"""Full-screen diff review: file sidebar, line-number gutters, split/stack.

Adapts review-first terminal diff viewer ideas (sidebar navigation,
line-number gutters, responsive split/stack layout, a slot for inline
notes) directly into the console's own canvas. Pure logic: takes a
ReviewState and a size, returns rows to draw; no terminal I/O here.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from core.diffparse import FileDiff, Hunk, parse_diff

_SIDEBAR_MAX = 28
_SIDEBAR_MIN = 14
_SPLIT_MIN_WIDTH = 110  # below this the pane stacks instead of splitting
_GUTTER_W = 4           # line-number gutter width per side


@dataclass
class ReviewState:
    """Scroll and navigation state for the review surface."""

    files: list[FileDiff]
    index: int = 0
    offset: int = 0
    split: bool = True
    notes: dict[str, list[str]] = field(default_factory=dict)

    @property
    def file(self) -> FileDiff:
        return self.files[self.index]

    @property
    def file_count(self) -> int:
        return len(self.files)

    def clamp(self, total: int, viewport: int) -> None:
        """Pin the offset after a render (total comes from the renderer)."""
        max_offset = max(0, total - max(1, viewport))
        if self.offset > max_offset:
            self.offset = max_offset

    def step(self, delta: int, total: int, viewport: int) -> None:
        self.offset = max(0, min(max(0, total - max(1, viewport)), self.offset + delta))

    def next_file(self, delta: int) -> None:
        count = len(self.files)
        if count:
            self.index = (self.index + delta) % count
            self.offset = 0


@dataclass
class ReviewRow:
    text: str
    style: str  # add | del | ctx | dim | head | sel | sep


@dataclass
class ReviewFrame:
    sidebar: list[ReviewRow]
    sidebar_w: int
    header: str
    body: list[ReviewRow]
    footer: str
    total: int
    split: bool


def _hunk_header(h: Hunk) -> str:
    old_n = sum(1 for l in h.lines if l.kind in ("del", "ctx"))
    new_n = sum(1 for l in h.lines if l.kind in ("add", "ctx"))
    old = f"-{h.old_start},{old_n}" if old_n != 1 else f"-{h.old_start}"
    new = f"+{h.new_start},{new_n}" if new_n != 1 else f"+{h.new_start}"
    return f"@@ {old} {new} @@"


def _stacked_rows(file: FileDiff) -> list[ReviewRow]:
    """One row per hunk line with old/new gutters; ``...`` between hunks."""
    rows: list[ReviewRow] = []
    prev_old_end: int | None = None
    prev_new_end: int | None = None
    for hunk in file.hunks:
        if prev_old_end is not None and (
            hunk.old_start - prev_old_end > 1 or hunk.new_start - prev_new_end > 1
        ):
            rows.append(ReviewRow("...", "dim"))
        rows.append(ReviewRow(_hunk_header(hunk), "sep"))
        for line in hunk.lines:
            old = f"{line.old_no:>{_GUTTER_W}}" if line.old_no is not None else " " * _GUTTER_W
            new = f"{line.new_no:>{_GUTTER_W}}" if line.new_no is not None else " " * _GUTTER_W
            rows.append(ReviewRow(f"{old} {new} {line.text}", line.kind))
        if hunk.lines:
            prev_old_end = max((l.old_no for l in hunk.lines if l.old_no is not None), default=hunk.old_start)
            prev_new_end = max((l.new_no for l in hunk.lines if l.new_no is not None), default=hunk.new_start)
    return rows


def _split_pairs(hunk: Hunk) -> list[tuple[str, int | None, int | None, str, str]]:
    """Pair removed and added runs so a change reads side by side."""
    pairs: list[tuple[str, int | None, int | None, str, str]] = []
    lines = hunk.lines
    n = len(lines)
    i = 0
    while i < n:
        line = lines[i]
        if line.kind == "ctx":
            pairs.append(("ctx", line.old_no, line.new_no, line.text, line.text))
            i += 1
        elif line.kind == "del":
            j = i
            while j < n and lines[j].kind == "del":
                j += 1
            k = j
            while k < n and lines[k].kind == "add":
                k += 1
            dels = lines[i:j]
            adds = lines[j:k]
            for idx in range(max(len(dels), len(adds))):
                d = dels[idx] if idx < len(dels) else None
                a = adds[idx] if idx < len(adds) else None
                pairs.append(("pair", d.old_no if d else None, a.new_no if a else None,
                              d.text if d else "", a.text if a else ""))
            i = k
        elif line.kind == "add":
            pairs.append(("pair", None, line.new_no, "", line.text))
            i += 1
        else:
            i += 1
    return pairs


def _split_rows(file: FileDiff, left_w: int, right_w: int) -> list[ReviewRow]:
    """Two-column rows: old on the left, new on the right, shared gutter."""
    rows: list[ReviewRow] = []
    for hunk in file.hunks:
        rows.append(ReviewRow(_hunk_header(hunk), "sep"))
        for kind, old_no, new_no, ltext, rtext in _split_pairs(hunk):
            lg = f"{old_no:>{_GUTTER_W}}" if old_no is not None else " " * _GUTTER_W
            rg = f"{new_no:>{_GUTTER_W}}" if new_no is not None else " " * _GUTTER_W
            left = (f"{lg} {ltext}")[:left_w].ljust(left_w)
            right = (f"{rg} {rtext}")[:right_w].ljust(right_w)
            style = "pair" if ltext and rtext else "del" if ltext else "add"
            rows.append(ReviewRow(left + "│ " + right, style))
    return rows


def _sidebar(state: ReviewState, width: int, height: int) -> list[ReviewRow]:
    rows: list[ReviewRow] = []
    for i, f in enumerate(state.files[: max(1, height - 2)]):
        stats = f" +{f.added} -{f.removed}" if f.added or f.removed else ""
        name = f.path
        if len(name) > width - 1:
            name = name[: width - 4] + "..."
        rows.append(ReviewRow(
            ("> " if i == state.index else "  ") + name + stats,
            "sel" if i == state.index else "dim",
        ))
    return rows


def render_review(state: ReviewState, width: int, height: int) -> ReviewFrame:
    """Build the drawable frame for the current review state."""
    sidebar_w = max(_SIDEBAR_MIN, min(_SIDEBAR_MAX, 10 + max((len(f.path) for f in state.files), default=8)))
    sidebar_w = min(sidebar_w, max(10, width // 3))
    main_w = max(20, width - sidebar_w)
    use_split = state.split and main_w >= _SPLIT_MIN_WIDTH

    file = state.file
    if use_split:
        half = (main_w - 3) // 2
        body = _split_rows(file, max(12, half), max(12, main_w - half - 3))
    else:
        body = _stacked_rows(file)
    # Inline notes slot: agent/operator notes for the current file render
    # dimmed above the hunks when present.
    notes = state.notes.get(file.path)
    if notes:
        body = [ReviewRow("notes:", "dim")] + [ReviewRow("  " + n, "dim") for n in notes] + body
    total = len(body)

    viewport = max(1, height - 2)
    start = min(state.offset, max(0, total - viewport))
    body = body[start: start + viewport]

    stats = f"(+{file.added} -{file.removed})" if file.added or file.removed else "(unchanged)"
    header = f"{file.path}  {stats}   [{state.index + 1}/{state.file_count}]"
    footer = ("split │ j/k move · PgUp/PgDn page · Tab/h/l files · s view · q/Esc back"
              if use_split else
              "stack │ j/k move · PgUp/PgDn page · Tab/h/l files · s view · q/Esc back")
    return ReviewFrame(
        sidebar=_sidebar(state, sidebar_w, height),
        sidebar_w=sidebar_w,
        header=header,
        body=body,
        footer=footer,
        total=total,
        split=use_split,
    )
