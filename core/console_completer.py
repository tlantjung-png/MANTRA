# Split out of core/console.py; import it from core.console, never from here.

"""Completion for the console: slash commands, @paths, models,
skills, and workflows."""

from __future__ import annotations

import os
import threading
import time

import core.agent.skills as skills
import core.agent.workflows as workflows
from core.agent.models import is_reasoning_model
from core.agent.settings import endpoints as known_endpoints
from core.console_common import (
    MAX_INDEX_ENTRIES,
    SLASH_COMMANDS,
    _MENTION_TRIM,
    _SKIP_DIRS,
    _strip_leading_invisible,
)
from core.tui.composer import Completion


class ConsoleCompleter:
    """Suggests slash commands after ``/`` and workspace paths after ``@``."""

    def __init__(self, session: "ConsoleSession") -> None:
        self.session = session
        self._entries: list[str] = []
        self._indexed = False
        self._cache_root = ""
        self._cache_time = 0.0
        self._indexing = False  # a background rebuild is in flight

    def begin(self) -> None:
        """Re-index the workspace once per prompt, not once per keystroke.

        The walk runs on a background daemon thread (after the first
        synchronous index) so the presenter thread never blocks on a
        large tree; complete() reads the last-known cache until the
        rebuild lands.
        """
        root = os.path.abspath(self.session.sandbox.root)
        now = time.monotonic()
        if self._indexed and root == self._cache_root and (now - self._cache_time) < 0.5:
            return
        self._cache_root = root
        self._cache_time = now

        def _walk() -> list[str]:
            entries: list[str] = []
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS)
                relative_dir = os.path.relpath(dirpath, root)
                base = "" if relative_dir == "." else relative_dir.replace(os.sep, "/")
                for name in sorted(dirnames):
                    entries.append(f"{base}/{name}/" if base else f"{name}/")
                for name in sorted(filenames):
                    entries.append(f"{base}/{name}" if base else name)
                if len(entries) >= MAX_INDEX_ENTRIES:
                    break
            return entries[:MAX_INDEX_ENTRIES]

        if not self._indexed:
            # First index stays synchronous so the very first @
            # completion has a cache to read.
            try:
                self._entries = _walk()
            except OSError:
                self._entries = []
            self._indexed = True
            return
        if self._indexing:
            return  # a rebuild is already in flight; skip this tick
        self._indexing = True

        def _rebuild() -> None:
            try:
                entries = _walk()
            except OSError:
                # Silent fallback: keep the previous entries instead of
                # clearing; @ completion stays on the stale cache.
                self._indexing = False
                return
            self._entries = entries
            self._indexing = False

        threading.Thread(target=_rebuild, daemon=True, name="console-completer-index").start()

    def complete(self, buffer: str, cursor: int):
        # The editor primes this once per prompt, but a caller using the
        # completer directly should not get silence for forgetting to.
        if not self._indexed:
            self.begin()
        # A cursor past the end must not index off the string; clamp
        # rather than trusting every caller to pass a sane position.
        cursor = max(0, min(cursor, len(buffer)))
        start = cursor
        # Invisible characters (BOM, zero-width, NBSP, ideographic space)
        # never start a token; strip them while scanning for the boundary.
        _ZW = "\ufeff\u200b\u200c\u200d\u00a0\u3000"
        while start > 0 and not buffer[start - 1].isspace() and buffer[start - 1] not in _ZW:
            start -= 1
        # Skip leading zero-width that may have been left at token start
        while start < cursor and buffer[start] in _ZW:
            start += 1
        token = buffer[start:cursor]
        # Strip trailing punctuation from the token for completion purposes
        # so "@src/app.py," still completes to the file.
        trim_token = token.rstrip(_MENTION_TRIM)
        # If trimming changed the token, adjust end position accordingly
        trim_end = cursor - (len(token) - len(trim_token)) if trim_token != token else cursor
        # Also handle full-width variants that some keyboards produce
        if trim_token.startswith("＠"):
            return self._complete_path(start, trim_end, trim_token[1:])
        if trim_token.startswith("@"):
            return self._complete_path(start, trim_end, trim_token[1:])
        # Slash commands: allow leading whitespace and invisible chars, but only when the slash
        # token is the first non-space token (so "hello /help" does not
        # trigger, but "  /help" does). Strip trailing punctuation so
        # "/help," still offers completions.
        # Handle both normal and full-width slash, and be forgiving of invisible leading chars
        slash_token = trim_token
        if slash_token.startswith("／") or slash_token.startswith("\uff0f"):
            slash_token = "/" + slash_token[1:]
        if slash_token.startswith("/") and _strip_leading_invisible(buffer[:start]).strip() == "":
            return self._complete_command(trim_end, slash_token, start)
        # Sub-command completions: use lstrip so leading spaces don't break
        # them (operator may indent). Keep start-based token for replacement.
        stripped = buffer.lstrip()
        if stripped.startswith("/model "):
            return self._complete_model(cursor, token)
        if stripped.startswith("/skills") or stripped.startswith("/skill "):
            c = self._complete_skills(buffer, start, cursor, token)
            if c:
                return c
        if stripped.strip() in ("/skills", "/skill"):
            c = self._complete_skills(buffer, start, cursor, token)
            if c:
                return c
        if stripped.startswith("/workflow"):
            c = self._complete_workflow(buffer, start, cursor, token)
            if c:
                return c
        return None

    def _complete_model(self, cursor: int, token: str):
        known = getattr(self.session, "known_models", None) or []
        lowered = token.lower()
        matches = [m for m in known if m.lower().startswith(lowered)]
        if not matches:
            # /model also manages providers: offer the subcommands and
            # the saved endpoint names alongside model names.
            sub = [s for s in ("list", "remove", "key") if s.startswith(lowered)]
            eps = [n for n in sorted(known_endpoints().keys()) if n.startswith(lowered)]
            matches = sub + eps
            if not matches:
                return None
            return Completion(items=matches, start=cursor - len(token), end=cursor)
        matches = matches[:50]
        labels = [m + ("   reasons" if is_reasoning_model(m) else "") for m in matches]
        return Completion(
            items=matches, start=cursor - len(token), end=cursor, labels=labels
        )

    def _complete_command(self, cursor: int, token: str, start: int = 0):
        matches = [name for name, _ in SLASH_COMMANDS if name.startswith(token)]
        if not matches:
            return None
        labels = []
        for name in matches:
            description = next((d for n, d in SLASH_COMMANDS if n == name), "")
            labels.append(f"{name}  {description}")
        return Completion(items=matches, start=start, end=cursor, labels=labels)

    def _complete_path(self, start: int, cursor: int, query: str):
        if not self._entries:
            return None
        needle = query.lower().replace("\\", "/")
        prefix = [e for e in self._entries if e.lower().startswith(needle)]
        inside = [e for e in self._entries if needle and needle in e.lower()]
        ordered = prefix + [e for e in inside if e not in prefix]
        matches = ordered[:50]
        if not matches:
            return None
        return Completion(
            items=["@" + m for m in matches], start=start, end=cursor, labels=matches
        )

    def _complete_skills(self, buffer: str, start: int, cursor: int, token: str):
        # EASY: /skills space [anything] → shows matching skills, filtered by name/type
        # No other commands, no menu — just skills.
        prefix = buffer[:start]
        parts = prefix.strip().split()
        if not parts or parts[0] not in ("/skills", "/skill"):
            return None
        # Only the first argument after /skills is completed; ignore deeper args
        if len(parts) > 1:
            return None
        known = skills.list_skills()
        if not known:
            return None
        lowered = token.lower()
        if not lowered:
            # "/skills " + Tab → show all skills
            items = sorted(s.name for s in known)[:50]
            labels = []
            for n in items:
                sk = skills.get(n)
                desc = (sk.description or "")[:40]
                labels.append(f"{n}  {desc}" if desc else n)
            return Completion(items=items, start=start, end=cursor, labels=labels)
        # Filter by name OR description/type (case-insensitive substring)
        # For very short queries (<3 chars) only match name to avoid noisy description hits
        matches: list[str] = []
        for sk in known:
            if lowered in sk.name.lower():
                matches.append(sk.name)
            elif len(lowered) >= 3 and lowered in (sk.description or "").lower():
                matches.append(sk.name)
        if not matches:
            return None
        # Prefix matches first, then others — both alphabetically
        prefix_hits = [n for n in matches if n.lower().startswith(lowered)]
        other_hits = [n for n in matches if n not in prefix_hits]
        ordered = sorted(prefix_hits) + sorted(other_hits)
        ordered = ordered[:50]
        labels = []
        for n in ordered:
            sk = skills.get(n)
            desc = (sk.description or "")[:40] if sk else ""
            labels.append(f"{n}  {desc}" if desc else n)
        return Completion(items=ordered, start=start, end=cursor, labels=labels)

    def _complete_workflow(self, buffer: str, start: int, cursor: int, token: str):
        prefix = buffer[:start]
        parts = prefix.strip().split()
        if not parts or parts[0] != "/workflow":
            return None
        subcommands = ["list", "show", "create", "launch", "run", "start", "remove", "delete", "rm"]
        workflow_names = [w["name"] for w in workflows.list_workflows()]
        if len(parts) == 1:
            candidates = subcommands + workflow_names
            lowered = token.lower()
            matches = [c for c in candidates if c.lower().startswith(lowered)]
            if not matches and lowered:
                matches = [c for c in candidates if lowered in c.lower()]
            if not matches:
                return None
            return Completion(items=matches[:50], start=start, end=cursor, labels=matches[:50])
        elif len(parts) == 2:
            sub = parts[1].lower()
            if sub in ("show", "launch", "run", "start", "remove", "delete", "rm"):
                lowered = token.lower()
                matches = [n for n in workflow_names if n.lower().startswith(lowered)]
                if not matches and lowered:
                    matches = [n for n in workflow_names if lowered in n.lower()]
                if not matches:
                    return None
                return Completion(items=matches[:50], start=start, end=cursor, labels=matches[:50])
        return None
