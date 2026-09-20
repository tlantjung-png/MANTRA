# Split out of core/console.py; import it from core.console, never from here.

"""Tool-output presentation: the boxes, panes and pager the console draws.

One responsibility: turn a tool observation (a command's stdout, a diff,
a file read) into styled transcript rows that fit the viewport, with the
overflow queued for the empty-Enter pager instead of flooding the screen
mid-run. Nothing here decides *what* to show - only how it is framed.
"""

from __future__ import annotations

import difflib
import os

from core import theme
from core.console_render import _sanitize_output
from core.term import visible_len

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # Type-only: console.py imports this module back, so a runtime import
    # here would be circular. ruff F821 is otherwise raised on every
    # forward reference in signatures.
    from core.console import ConsoleSession


class BoxRenderingMixin:
    """Boxes, before/after panes, and the overflow pager."""

    # ---- geometry ---------------------------------------------------------

    def _output_cols(self: "ConsoleSession") -> int:
        """Current viewport width, or a sane default when not on a TUI."""
        try:
            layout = getattr(self, "layout", None)
            cols = getattr(layout, "_cols", 0)
            if layout is not None and getattr(layout, "active", False) and cols >= 30:
                return int(cols)
        except Exception:
            pass  # duck-typed bridge in an unexpected shape; use the fallback
        return self._OUTPUT_COLS_FALLBACK

    def _row_cost(self: "ConsoleSession", line: str) -> int:
        """Approx wrapped screen rows one line will occupy inside a box."""
        cols = max(1, self._output_cols())
        width = visible_len(line) + 2  # '│ ' gutter
        return max(1, (width + cols - 1) // cols)

    def _rows_of(self: "ConsoleSession", lines: list[str]) -> int:
        return sum(self._row_cost(ln) for ln in lines)

    def _head_lines(self: "ConsoleSession", lines: list[str], budget: int) -> tuple[list[str], int]:
        """Take from the start of a stream until the row budget is spent."""
        shown: list[str] = []
        used = 0
        for ln in lines:
            cost = self._row_cost(ln)
            if used + cost > budget and shown:
                break
            shown.append(ln)
            used += cost
        return shown, len(lines) - len(shown)

    # ---- single boxes -----------------------------------------------------

    def _box(self: "ConsoleSession", title: str, body: list[str]) -> str:
        """Assemble a titled box with '│ ' gutter rows."""
        out = [self.style._wrap(theme.HAIR, "┌ ") + self.style._wrap(theme.BONE, title)]
        out.extend(body)
        out.append(self.style._wrap(theme.HAIR, "└" + "─" * 38))
        return "\n".join(out)

    def _row(self: "ConsoleSession", ln: str) -> str:
        """One styled '│ ' gutter row inside a tool-output box."""
        if ln.startswith("exit_code:"):
            code = -1
            try:
                code = int(ln.split(":", 1)[1].split()[0])
            except (ValueError, IndexError):
                pass  # malformed exit_code line: keep the error colour
            color = theme.SAGE if code == 0 else theme.EMBER
            return self.style._wrap(color, "│ " + ln)
        if ln.startswith(("stdout:", "stderr:", "log:", "Note:")):
            return self.style._wrap(theme.FAINT, "│ " + ln)
        if ln.startswith("+") and not ln.startswith("+++"):
            return self.style._wrap(theme.DIFF_ADD, "│ " + ln)
        if ln.startswith("-") and not ln.startswith("---"):
            return self.style._wrap(theme.DIFF_REMOVE, "│ " + ln)
        if ln.startswith(("@@", "index ", "diff --git", "--- ", "+++ ")):
            return self.style._wrap(theme.FAINT, "│ " + ln)
        return "│ " + ln

    def _format_diff(self: "ConsoleSession", diff_text: str, max_lines: int = 60, title: str = "") -> str:
        """Colour a unified diff inside a small box, optionally titled."""
        if not diff_text:
            return ""
        lines = diff_text.splitlines()
        total = len(lines)
        if len(lines) > max_lines:
            lines = lines[:max_lines]
            lines.append(self.style.dim(f"... ({total - max_lines} more lines)"))
        if title:
            top = self.style._wrap(theme.HAIR, "┌ ") + self.style._wrap(theme.BONE, title)
        else:
            top = self.style._wrap(theme.HAIR, "┌" + "─" * 38)
        out = [top]
        for line in lines:
            if line.startswith("+"):
                out.append(self.style._wrap(theme.HAIR, "│ ") + self.style._wrap(theme.DIFF_ADD, line))
            elif line.startswith("-"):
                out.append(self.style._wrap(theme.HAIR, "│ ") + self.style._wrap(theme.DIFF_REMOVE, line))
            else:
                out.append(self.style._wrap(theme.HAIR, "│ ") + self.style._wrap(theme.FAINT, line))
        out.append(self.style._wrap(theme.HAIR, "└" + "─" * 38))
        return "\n".join(out)

    # ---- before/after diff panes ---------------------------------------
    #
    # Structured unified diffs (agent edits, /diff, git-diff boxes) render
    # as two stacked panes per hunk instead of a +/- line stream: first
    # the before-state with removed lines in a soft dusty red, then the
    # after-state with added lines in a soft sage green - text colour
    # only, no backgrounds. A context line exists on both sides, so it is
    # shown only once - in the pane whose change sits nearest - which
    # keeps each pane anchored without doubling the code.

    def _pane_row(self: "ConsoleSession", body: str, code: str | None) -> str:
        """One '│ ' gutter row; *code* colours the changed text softly."""
        gutter = self.style._wrap(theme.HAIR, "│ ")
        if not code:
            return gutter + body
        return gutter + self.style._wrap(code, body)

    def _pane_chip(self: "ConsoleSession", label: str) -> str:
        """A small 'old' / 'new' marker row that opens a pane."""
        return self.style._wrap(theme.HAIR, "│ ") + self.style._wrap(theme.ASH, label)

    def _diff_pane_rows(self: "ConsoleSession", diff_text: str, max_lines: int = 60) -> list[str] | None:
        """Styled old/new pane rows for a unified diff.

        Returns None when *diff_text* is not a parseable unified diff (no
        hunks, or foreign content before the first hunk), so callers can
        fall back to the plain diff renderer.
        """
        groups: list[tuple[str | None, str | None, list[list[str]]]] = []
        cur: tuple[str | None, str | None, list[list[str]]] | None = None
        hunk: list[str] | None = None
        seen_hunk = False
        for ln in diff_text.splitlines():
            if ln.startswith("--- "):
                cur = [ln[4:].strip(), None, []]
                groups.append(cur)
                hunk = None
            elif ln.startswith("+++ "):
                if cur is None:
                    cur = [None, None, []]
                    groups.append(cur)
                cur[1] = ln[4:].strip()
            elif ln.startswith("@@"):
                if cur is None:
                    cur = [None, None, []]
                    groups.append(cur)
                hunk = []
                cur[2].append(hunk)
                seen_hunk = True
            elif hunk is not None and ln[:1] in (" ", "-", "+"):
                hunk.append(ln)
            elif not seen_hunk and ln and not ln.startswith(
                ("diff ", "index ", "new file", "deleted file", "old mode", "new mode", "similarity ", "rename ", "Binary ")
            ):
                # Foreign content before any hunk (command output, notes):
                # this is not a diff we should pane-ify.
                return None
        if not any(hs for _, _, hs in groups):
            return None

        def _file_base(label: str | None) -> str | None:
            """'a/src/x.py' -> 'src/x.py'; 'x.py (after)' -> 'x.py'."""
            if not label:
                return None
            base = label.replace("\\", "/")
            if base.startswith(("a/", "b/")):
                base = base[2:]
            for suffix in (" (before)", " (after)"):
                if base.endswith(suffix):
                    base = base[: -len(suffix)]
            return base or None

        rows: list[str] = []
        for old_lbl, new_lbl, hunks in groups:
            # A per-file chip before a group's first hunk. Agent-edit diffs
            # already name the file in their box title (labels end in
            # "(before)"), so only git-style output gets the chip.
            file_base = _file_base(old_lbl or new_lbl)
            git_style = not ((old_lbl or "").endswith(" (before)") and (new_lbl or "").endswith(" (after)"))
            if file_base and git_style:
                rows.append(self._pane_chip(file_base))
            for hunk_lines in hunks:
                seq: list[tuple[str, str]] = []
                for ln in hunk_lines:
                    seq.append((ln[0], ln[1:]))
                minus_idx = [i for i, (kind, _) in enumerate(seq) if kind == "-"]
                plus_idx = [i for i, (kind, _) in enumerate(seq) if kind == "+"]
                old: list[tuple[str, bool]] = []
                new: list[tuple[str, bool]] = []
                for i, (kind, body) in enumerate(seq):
                    if kind == "-":
                        old.append((body, True))
                    elif kind == "+":
                        new.append((body, True))
                    else:
                        # Context is identical on both sides: show it once,
                        # in whichever pane holds the change nearest it.
                        d_old = min((abs(i - j) for j in minus_idx), default=10**9)
                        d_new = min((abs(i - j) for j in plus_idx), default=10**9)
                        if d_old <= d_new:
                            old.append((body, False))
                        else:
                            new.append((body, False))
                if old:
                    rows.append(self._pane_chip("old"))
                    rows.extend(self._pane_row(b, theme.DIFF_REMOVE if changed else None) for b, changed in old)
                if new:
                    rows.append(self._pane_chip("new"))
                    rows.extend(self._pane_row(b, theme.DIFF_ADD if changed else None) for b, changed in new)
        total = len(rows)
        if total > max_lines:
            rows = rows[:max_lines]
            rows.append(
                self.style._wrap(theme.HAIR, "│ ")
                + self.style._wrap(theme.FAINT, f"… {total - max_lines} more diff lines")
            )
        return rows

    # ---- the overflow pager ----------------------------------------------

    def _render_diff_pages(self: "ConsoleSession", title: str, rows: list[str], lead: list[str] | None = None) -> str:
        """Box for pre-styled diff-pane rows, paged like other tool boxes."""
        lead_rows = [self._row(ln) for ln in (lead or [])]
        if self._rows_of(lead_rows) + self._rows_of(rows) <= self._TOOL_OUTPUT_BUDGET_ROWS:
            return self._box(title, lead_rows + rows)
        preview, _ = self._head_lines(rows, self._READ_PAGE_ROWS)
        remaining = rows[len(preview):]
        if remaining:
            with self._pager_lock:
                self._pending_pages.append((title, remaining))
                self._pending_styled.append(True)
        shown = lead_rows + preview
        shown.append(
            self.style.dim(
                f"│ … {len(remaining)} more rows — press Enter (empty prompt) to page through the diff"
            )
        )
        return self._box(title, shown)

    def _render_paged_box(self: "ConsoleSession", title: str, content: list[str], lead: list[str] | None = None) -> str:
        """Box for any tool output that may exceed a screenful.

        Fits the budget → shown whole. Overflows → a compact first page
        and the remainder queued (title + lines) for the empty-Enter
        pager, so nothing is lost and the viewport is never flooded
        mid-run - reads, commands, diffs and background logs alike.
        """
        lead_rows = lead or []
        if self._rows_of(lead_rows) + self._rows_of(content) <= self._TOOL_OUTPUT_BUDGET_ROWS:
            rows = [self._row(ln) for ln in lead_rows] + [self._row(ln) for ln in content]
            return self._box(title, rows)
        preview, _ = self._head_lines(content, self._READ_PAGE_ROWS)
        remaining = content[len(preview):]
        if remaining:
            with self._pager_lock:
                self._pending_pages.append((title, remaining))
                self._pending_styled.append(False)
        rows = [self._row(ln) for ln in lead_rows] + [self._row(ln) for ln in preview]
        rows.append(
            self.style.dim(
                f"│ … {len(remaining)} more lines — press Enter (empty prompt) to page through the output"
            )
        )
        return self._box(title, rows)

    # ---- the edit preview -------------------------------------------------

    def _file_text(self: "ConsoleSession", rel: str) -> str | None:
        """Full text of a workspace-relative file, or None when unreadable."""
        if not rel:
            return None
        # The path can arrive from model-supplied tool arguments, so the
        # join is confined like every other workspace read: a "..", an
        # absolute path, or a symlink must not widen the snapshot read
        # beyond the workspace.
        try:
            root = os.path.realpath(self.workspace)
            full = os.path.realpath(os.path.join(root, rel))
            if not (full == root or full.startswith(root + os.sep)):
                return None
        except OSError:
            return None
        try:
            if not os.path.isfile(full):
                return None
            if os.path.getsize(full) > 1_000_000:
                return None
            with open(full, "r", encoding="utf-8", errors="replace") as fh:
                return fh.read()
        except OSError:
            return None

    def _render_file_change(self: "ConsoleSession", tool: str, result: str) -> str:
        """Readable result of edit_file / write_file.

        edit_file gets a coloured before/after diff against the snapshot
        taken when the tool_call arrived; write_file shows a syntax
        preview of the new file. Falls back to the tool's own message
        when the file is missing or too large to snapshot, so the
        operator always sees the file that was just edited, capped and
        wrapped instead of dumped raw.
        """
        from core.console_render import _syntax_highlight

        path = self._last_edit_path or ""
        # The path is model-supplied tool-argument text, so the display
        # form must not carry ANSI escapes into box titles or diff labels.
        display = _sanitize_output(path)
        new = self._file_text(path) if path else None
        if new is None:
            if isinstance(result, str) and result.strip():
                return self._format_diff(result.strip(), max_lines=20)
            return ""
        old = self._edit_snapshots.get(path)
        if tool == "write_file" and old is None:
            # A brand-new file: syntax preview, capped so a long file
            # stays readable.
            lines = new.splitlines()
            total = len(lines)
            if total > 120:
                lines = lines[:120]
            out = [self.style._wrap(theme.HAIR, "┌ ") + self.style._wrap(theme.BONE, f"wrote {display} ({total} lines)")]
            for line in lines:
                out.append(_syntax_highlight(line, self.style))
            if total > 120:
                out.append(self.style.dim(f"  ... {total - 120} more lines"))
            out.append(self.style._wrap(theme.HAIR, "└" + "─" * 38))
            return "\n".join(out)
        body = "\n".join(
            difflib.unified_diff(
                (old or "").splitlines(),
                new.splitlines(),
                fromfile=display + " (before)",
                tofile=display + " (after)",
                lineterm="",
            )
        )
        if not body.strip():
            return ""
        return self._format_diff(body, max_lines=60, title=f"{tool} {display}")
