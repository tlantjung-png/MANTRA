# Split out of core/console.py; import it from core.console, never from here.

"""The session todo checklist: the agent's own TODO reports, applied to
the live list.

Pure state methods that touch nothing but ``self`` and the reply text,
so they carry no console-module dependency at all. There is no command
surface: the checklist grows and drains from inside the conversation,
via TODO ADD / TODO DONE lines in the agent's reply.
"""

from __future__ import annotations

import re

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # Type-only: console.py imports this module back, so a runtime import
    # here would be circular. ruff F821 is otherwise raised on every
    # forward reference in signatures.
    from core.agent.loop import RunResult
    from core.console import ConsoleSession


class TodosMixin:
    """The session todo checklist on a ConsoleSession."""

    def _normalise_todo(self: "ConsoleSession", text: str) -> str:
        return re.sub(r"\s+", " ", text.strip()).casefold()

    def _check_todo_completion(self: "ConsoleSession", result: "RunResult | None") -> None:
        """Apply the agent's TODO reports from its final message.

        Two reports, each on its own line:

        - ``TODO DONE: <text>`` checks an open item off. Matching is
          exact after normalising whitespace, so the agent echoing an
          item's text is the only thing that checks it off; a paraphrase
          does nothing.
        - ``TODO ADD: <text>`` appends a new item the agent discovered
          along the way (follow-up work a task turned up). Deduplicated
          against the list verbatim so repeated reports do not stack.

        Reports already applied inline by the streaming hook are skipped
        here: their state change happened as the reply streamed and their
        note is already on screen, so the end-of-turn pass only handles
        what the stream never saw (non-streamed replies, reports whose
        line fell outside the stream path).
        """
        if result is None or not result.final_message:
            return
        # Two passes, adds first: an item added and completed in the same
        # message must check off regardless of which line came first.
        reports = []
        for line in result.final_message.splitlines():
            head, _, rest = line.partition(":")
            head = head.strip().upper()
            reported = self._normalise_todo(rest)
            if not reported:
                continue
            if head in ("TODO DONE", "TODO ADD"):
                reports.append((head, rest.strip(), reported))
        # Adds first, then completions; reports already applied inline
        # by the streaming hook are skipped inside _apply_todo_report.
        for head, text, reported in reports:
            if head != "TODO ADD":
                continue
            if self._apply_todo_report(head, text, reported):
                self._print(self.style.dim(f"  todo added ({len(self.todos)}): {text}"))
        for head, text, reported in reports:
            if head != "TODO DONE":
                continue
            if self._apply_todo_report(head, text, reported):
                self._print(self.style.dim(f"  checked off: {text}"))

    def _handle_stream_todo_report(self: "ConsoleSession", line: str) -> str:
        """Stream hook: apply a TODO report the moment its line arrives.

        The raw ``TODO ADD: …`` protocol text never reaches the screen.
        Instead the change is applied to the live list and a quiet note is
        returned in its place, so the operator watches the checklist grow
        and drain inside the streamed reply rather than reading a marker
        line or waiting for the turn to end.
        """
        head, _, text = line.partition(":")
        reported = self._normalise_todo(text)
        if not reported:
            return ""
        # Normalise the case: the report pattern is case-insensitive, so
        # "todo done: ..." must reach the applier, which compares exact
        # uppercase heads.
        applied = self._apply_todo_report(head.strip().upper(), text.strip(), reported)
        if not applied:
            # Already applied inline earlier in this stream (or a done
            # item re-reported): return empty so the renderer swallows
            # the raw protocol line instead of showing it again.
            return ""
        # ASCII checkboxes, dimmed: an open item carries the [ ] marker,
        # a finished one the [x] with the text struck through - so a note
        # reads exactly like a row of the checklist, only quieter than
        # the reply around it.
        if head.strip().upper() == "TODO DONE":
            return self.style.dim(
                self.style.hair("[x]") + " " + self.style.strike(text.strip())
            )
        open_count = sum(1 for t in self.todos if not t["done"])
        plural = "" if open_count == 1 else "s"
        return self.style.dim(
            "[ ] " + text.strip()
            + f" ({open_count} open item{plural})"
        )

    def _apply_todo_report(self: "ConsoleSession", head: str, text: str, reported: str) -> bool:
        """Apply one TODO ADD / TODO DONE report. Shared by both paths.

        Returns True when a change was applied (an item added or checked
        off). Callers decide how to announce it - the stream path returns
        a styled note inline, the end-of-turn path prints after the turn.
        Either way the state change happens once: reports already applied
        inline are recorded in ``_turn_todo_reports`` so the end-of-turn
        pass never re-adds, re-checks, or re-announces them.
        """
        if (head, reported) in self._turn_todo_reports:
            return False  # already applied inline while the reply streamed
        if head == "TODO ADD":
            if any(self._normalise_todo(t["text"]) == reported for t in self.todos):
                return False  # already tracked - do not stack duplicates
            self.todos.append({"text": text, "done": False})
            self._turn_todo_reports.append(("TODO ADD", reported))
            return True
        if head == "TODO DONE":
            for item in self.todos:
                if item["done"]:
                    continue
                if self._normalise_todo(item["text"]) == reported:
                    item["done"] = True
                    self._turn_todo_reports.append(("TODO DONE", reported))
                    return True
        return False
