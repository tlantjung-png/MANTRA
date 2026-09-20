# Split out of core/console.py; import it from core.console, never from here.

"""@-mention expansion: turning ``@path`` tokens in the operator's
message into attached file and directory context, with the attachment
budget, dedup, and workspace containment rules."""

from __future__ import annotations

import glob
import os

from core.console_common import MENTION_RE, _MENTION_TRIM

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # Type-only: console.py imports this module back, so a runtime import
    # here would be circular. ruff F821 is otherwise raised on every
    # forward reference in signatures.
    from core.console import ConsoleSession

MAX_ATTACH_CHARS = 20_000
MAX_TOTAL_ATTACH_CHARS = 60_000
MAX_GLOB_HITS = 20
MAX_LISTING_ENTRIES = 100


class MentionsMixin:
    """@-mention resolution and attachment rendering."""

    def expand_mentions(self: "ConsoleSession", text: str) -> tuple[str, list[str]]:
        """Turn ``@path`` tokens into real context the model can see.

        Keeps the operator's wording intact and appends an "Attached
        context" block, which is what every mainstream agent CLI does and
        what the model already understands. Unknown references are left
        alone and reported rather than silently dropped.
        """
        # Normalize full-width variants before matching so ＠ and ／ work
        text_norm = text.replace("＠", "@").replace("／", "/")
        tokens = MENTION_RE.findall(text_norm)
        if not tokens:
            # Also try finding full-width mentions directly if normal found none
            tokens = MENTION_RE.findall(text)
            if not tokens:
                return text, []

        # Normalize: a root given with forward slashes compares unequal to
        # normpath output on Windows, which made every mention "no match".
        # Realpath (not just abspath) so the containment checks below and
        # in _resolve_mention measure against the same canonical root.
        root = os.path.realpath(os.path.abspath(self.sandbox.root))
        blocks: list[str] = []
        attached: list[str] = []
        total = 0
        seen: set[str] = set()
        budget_note_shown = False

        for token in tokens:
            # Strip surrounding quotes and trailing punctuation that is
            # sentence punctuation, not part of the path.
            raw = token.strip().strip("'\"`")
            trimmed = raw.rstrip(_MENTION_TRIM)
            # Also handle "@\"src/app.py\"" style where quotes were part of token
            trimmed = trimmed.strip("'\"`")
            if not trimmed:
                continue
            if total >= MAX_TOTAL_ATTACH_CHARS:
                # Say it once, then stop scanning: one note per skipped
                # mention just spams the transcript.
                if not budget_note_shown:
                    budget_note_shown = True
                    self._note("attachment budget reached - remaining mentions skipped")
                break
            dedup_key = trimmed.lower() if os.name == "nt" else trimmed
            if dedup_key in seen:
                continue
            seen.add(dedup_key)
            # Try several variations to be forgiving
            candidates_to_try = [trimmed]
            if trimmed != token:
                candidates_to_try.append(token.strip("'\"`").rstrip(_MENTION_TRIM))
            # Also try without leading ./ if present
            if trimmed.startswith("./"):
                candidates_to_try.append(trimmed[2:])
            if trimmed.startswith(".\\"):
                candidates_to_try.append(trimmed[2:])
            paths: list[str] = []
            for cand in candidates_to_try:
                paths = self._resolve_mention(cand, root)
                if paths:
                    break
            if not paths:
                # Show the trimmed form in the note so the operator sees
                # what was actually tried, not the raw token with punctuation.
                self._note(f"no match for @{trimmed} (tried {candidates_to_try[0]!r})")
                continue
            for rel in paths:
                if total >= MAX_TOTAL_ATTACH_CHARS:
                    # Say it once, then stop scanning: one note per
                    # skipped mention just spams the transcript.
                    if not budget_note_shown:
                        budget_note_shown = True
                        self._note("attachment budget reached - remaining mentions skipped")
                    break
                full = os.path.join(root, rel)
                # Re-validate at read time: the path could have been
                # swapped for a symlink since the mention was resolved.
                # Resolve again so the containment check sits as close
                # to the open as possible, then render the canonical
                # path itself. Residual TOCTOU window: the symlink could
                # still be swapped between this check and the open below.
                # No dirfd-based read exists on Windows; accepted because
                # the model already controls the workspace contents.
                real = os.path.realpath(full)
                if real != root and not real.startswith(root + os.sep):
                    continue
                block = (
                    self._render_listing(rel, real)
                    if os.path.isdir(real)
                    else self._render_file(rel, real)
                )
                if not block:
                    continue
                blocks.append(block)
                attached.append(rel)
                total += len(block)

        if not blocks:
            return text, []
        return text + "\n\nAttached context:\n\n" + "\n\n".join(blocks), attached

    def _resolve_mention(self: "ConsoleSession", token: str, root: str) -> list[str]:
        """Resolve one mention to workspace-relative paths. Escapes refused."""
        root = os.path.realpath(os.path.abspath(root))
        # Robust trimming: quotes and trailing punctuation, and leading ./
        token = token.strip().strip("'\"`").rstrip(_MENTION_TRIM).strip("'\"`")
        if not token:
            return []
        # Normalize separators to the host's convention before probing.
        candidate = token.replace("/", os.sep).replace("\\", os.sep)
        if "*" in token:
            # Normalize pattern for glob: use forward slashes for root_dir glob
            # which expects POSIX-style patterns on all platforms.
            cand_posix = token.replace("\\", "/").lstrip("/")
            hits = sorted(glob.glob(cand_posix, root_dir=root, recursive=True))
            valid: list[str] = []
            for hit in hits:
                full_hit = os.path.realpath(os.path.join(root, hit))
                if not (full_hit == root or full_hit.startswith(root + os.sep)):
                    continue
                if os.path.isfile(os.path.join(root, hit)):
                    valid.append(hit)
                if len(valid) >= MAX_GLOB_HITS:
                    break
            return valid
        full = os.path.realpath(os.path.join(root, candidate))
        # Never read outside the workspace, however the path was written.
        if full != root and not full.startswith(root + os.sep):
            return []
        return [os.path.relpath(full, root)] if os.path.exists(full) else []

    @staticmethod
    def _render_file(rel: str, full: str) -> str:
        try:
            with open(full, "r", encoding="utf-8", errors="replace") as handle:
                content = handle.read(MAX_ATTACH_CHARS + 1)
        except OSError:
            return ""
        truncated = len(content) > MAX_ATTACH_CHARS
        body = content[:MAX_ATTACH_CHARS].rstrip()
        if truncated:
            body += "\n* [truncated]"
        return f"* @{rel.upper()} *\n{body}"

    @staticmethod
    def _render_listing(rel: str, full: str) -> str:
        try:
            entries = sorted(os.listdir(full))[:MAX_LISTING_ENTRIES]
        except OSError:
            return ""
        lines = [f"* @{rel.upper()} ({len(entries)} entries) *"]
        for entry in entries:
            kind = "DIR " if os.path.isdir(os.path.join(full, entry)) else "FILE"
            lines.append(f"{kind} {entry}")
        return "\n".join(lines)
