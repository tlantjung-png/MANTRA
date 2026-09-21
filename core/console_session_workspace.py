# Split out of core/console.py; import it from core.console, never from here.

"""Workspace inspection on the session: git helpers, the workspace
listing, the diff view, undo, and the /cost figures."""

from __future__ import annotations

import json
import os
import subprocess

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # Type-only: console.py imports this module back, so a runtime import
    # here would be circular. ruff F821 is otherwise raised on every
    # forward reference in signatures.
    from core.console import ConsoleSession


class WorkspaceMixin:
    """Git-backed inspection and the cost report."""

    def _git(self: "ConsoleSession", *args: str) -> str:
        try:
            completed = subprocess.run(
                ["git", *args], cwd=self.workspace,
                capture_output=True, text=True, timeout=30,
            )
        except (OSError, subprocess.SubprocessError):
            return ""
        return completed.stdout if completed.returncode == 0 else ""

    def _git_ok(self: "ConsoleSession", *args: str) -> bool:
        """Run git and report success; stdout alone cannot distinguish it."""
        try:
            completed = subprocess.run(
                ["git", *args], cwd=self.workspace,
                capture_output=True, text=True, timeout=30,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return completed.returncode == 0

    def show_workspace(self: "ConsoleSession") -> None:
        root = self.sandbox.root
        self._print(f"workspace: {root}")
        try:
            entries = sorted(os.listdir(root))[:50] if os.path.isdir(root) else []
        except OSError as exc:
            self._print(self.style.ember(f"  cannot list workspace: {exc}"))
            return
        for entry in entries:
            try:
                full = os.path.join(root, entry)
                self._print(("> " if os.path.isdir(full) else "") + entry)
            except OSError:
                self._print(entry)
        if not entries:
            self._print("(empty)")

    def show_diff(self: "ConsoleSession") -> None:
        stat = self._git("diff", "--stat")
        status = self._git("status", "--short")
        if not stat and not status:
            self._print("(no uncommitted changes)")
            return
        if status:
            self._print(status.rstrip())
        if stat:
            self._print(stat.rstrip())
        diff = self._git("diff")
        if diff:
            # Same before/after panes as the live edit previews and the
            # agent's git-diff boxes.
            rows = self._diff_pane_rows(diff, max_lines=400)
            if rows is not None:
                self._print(self._box("git diff", rows))
                return
            lines = diff.splitlines()
            cap = 400  # plain-text fallback: same cap as the boxed panes
            self._print("\n".join(lines[:cap]))
            if len(lines) > cap:
                self._print(f"... ({len(lines) - cap} more lines)")

    def undo_changes(self: "ConsoleSession") -> None:
        status = self._git("status", "--porcelain")
        if not status:
            self._print("(nothing to undo - working tree is clean)")
            return
        self._print(f"{len(status.strip().splitlines())} file(s) would be reverted:")
        self._print(status.rstrip())
        try:
            if self.ui is not None:
                answer = self.ui.ask_line('type "yes" to revert tracked changes').strip()
            else:
                answer = input('type "yes" to revert tracked changes: ').strip()
        except (KeyboardInterrupt, EOFError):
            self._print("cancelled")
            return
        if answer.lower() != "yes":
            self._print("cancelled")
            return
        self._print("reverted" if self._git_ok("checkout", "--", ".") else "revert failed")

    def show_cost(self: "ConsoleSession") -> None:
        t = self.totals
        tokens_in = t['tokens_in']
        tokens_out = t['tokens_out']
        cache_hit = t['cache_hit']
        context_tokens = self.context.tokens
        context_chars = self.context.chars

        # Derived metrics.
        cache_rate = (cache_hit * 100 // tokens_in) if tokens_in > 0 else 0
        cache_saved = cache_hit // 2  # ~50% discount
        obs_saved = int(t.get("observation_saved", 0) or 0)

        self._print(f"turns        {t['turns']}")
        self._print(f"tokens in    {tokens_in}")
        self._print(f"tokens out   {tokens_out}")
        # Cache analytics: show hit rate and estimated savings.
        if tokens_in > 0 and cache_hit > 0:
            self._print(f"cache hit    {cache_hit} ({cache_rate}% of prompt)")
            self._print(f"cache saved  {cache_saved} tokens (~50% discount)")
        else:
            self._print(f"cache hit    {cache_hit}")
        self._print(f"tool errors  {t['tool_errors']}")
        # Observation reshaping: characters the dense context copy removed
        # across the session. Only shown when the mechanism is on and has
        # actually removed something.
        if obs_saved > 0:
            self._print(f"obs saved   {obs_saved} chars (reshaped for the model)")
        # Rolling digest: how many evictions were folded into a summary, and
        # how many summariser calls failed. Both are shown only when non-zero,
        # so a session that never evicted is not decorated with zeros.
        digest_turns = int(t.get("digest_turns", 0) or 0)
        if digest_turns > 0:
            self._print(
                f"digested    {digest_turns} eviction(s) summarised "
                f"({int(t.get('digest_chars', 0) or 0)} chars)"
            )
        digest_failures = int(t.get("digest_failures", 0) or 0)
        if digest_failures > 0:
            self._print(f"digest fail {digest_failures} summariser call(s) failed")
        self._print(f"context      ~{context_tokens} tokens ({context_chars} chars)")
        # Per-turn cache trend (last 5 turns).
        if self.turn_history:
            recent = self.turn_history[-5:]
            self._print("")
            self._print("  turn  in      out     cached  rate")
            self._print("  " + "─" * 40)
            for entry in recent:
                tin = entry['tokens_in']
                tout = entry['tokens_out']
                ch = entry['cache_hit']
                cr = entry['cache_rate']
                self._print(
                    f"  {entry['turn']:<6}{tin:<8}{tout:<8}{ch:<8}{cr}%"
                )
