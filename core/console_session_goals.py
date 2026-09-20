# Split out of core/console.py; import it from core.console, never from here.

"""Goal and todo state on the session: the standing objective, the
session checklist, and the agent's TODO reports applied to both.

Pure state methods that touch nothing but ``self`` and the reply text,
so they carry no console-module dependency at all.
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


class GoalsTodosMixin:
    """Goal, notes, and the todo checklist on a ConsoleSession."""

    # ---- goal ------------------------------------------------------------

    def set_goal(self: "ConsoleSession", text: str) -> None:
        self.goal = text.strip()
        if not self.goal:
            return
        self._print(self.style.dim(f"  goal set: {self.goal}"))
        self._print(self.style.dim("  /goal to check it · /goal done to clear it"))

    def show_goal(self: "ConsoleSession") -> None:
        if not self.goal:
            self._print(self.style.dim("  no goal set - /goal <what you want done>"))
            return
        self._print(f"  {self.style.bold('goal')} {self.goal}")
        for note in self.goal_notes:
            self._print(self.style.dim(f"    · {note}"))
        if not self.goal_notes:
            self._print(self.style.dim("    (no notes - /goal note <text> to add one)"))

    def clear_goal(self: "ConsoleSession", reason: str = "") -> None:
        if not self.goal:
            self._print(self.style.dim("  no goal set"))
            return
        finished = self.goal
        self.goal = ""
        self.goal_notes = []
        self._print(self.style.dim(f"  goal cleared: {finished}"))
        if reason:
            self._print(self.style.dim(f"  {reason}"))

    def add_goal_note(self: "ConsoleSession", text: str) -> None:
        if not self.goal:
            self._print(self.style.dim("  set a goal first: /goal <what you want done>"))
            return
        self.goal_notes.append(text.strip())
        self._print(self.style.dim(f"  noted ({len(self.goal_notes)} on this goal)"))

    def _check_goal_completion(self: "ConsoleSession", result: "RunResult | None") -> None:
        """Notice an agent that declared the goal met.

        The agent cannot clear the goal itself - only report - so a wrong
        claim costs nothing but a line the operator can ignore.
        """
        if not self.goal or result is None or not result.final_message:
            return
        if "GOAL COMPLETE" not in result.final_message.upper():
            return
        self._print(
            self.style.dim("  the agent reports the goal is met - /goal done to clear it")
        )

    # ---- todos ----------------------------------------------------------

    def _normalise_todo(self: "ConsoleSession", text: str) -> str:
        return re.sub(r"\s+", " ", text.strip()).casefold()

    def _find_todo(self: "ConsoleSession", query: str, open_only: bool = True) -> int | None:
        """Resolve a numbered item (1-based) or a text match to an index.

        Text must match an item's whole text (after normalising
        whitespace and case), never a fragment: a phrase that merely
        sits inside a longer item would check the wrong thing off. To
        pick among similar items or reach a done one, use the number
        shown by /todo. With ``open_only`` (the default) done items are
        never auto-selected - the operator explicitly re-numbers an item
        to reopen it. Removal passes ``open_only=False`` because a done
        item still needs to be findable to drop.
        """
        query = query.strip()
        if query.isdigit():
            index = int(query) - 1
            return index if 0 <= index < len(self.todos) else None
        target = self._normalise_todo(query)
        for index, item in enumerate(self.todos):
            if (not open_only or not item["done"]) and self._normalise_todo(item["text"]) == target:
                return index
        return None

    def add_todo(self: "ConsoleSession", text: str) -> None:
        text = text.strip()
        if not text:
            self._print(self.style.dim("  usage: /todo add <what needs doing>"))
            return
        self.todos.append({"text": text, "done": False})
        self._print(f"  {self.style.brand(str(len(self.todos)) + '.')} {text}")
        self._print(self.style.dim("  /todo to see the list · /todo done <n> when it's done"))

    def show_todos(self: "ConsoleSession") -> None:
        if not self.todos:
            self._print(self.style.dim("  no todos - /todo add <what needs doing>"))
            return
        for index, item in enumerate(self.todos, 1):
            marker = self.style.ash("[ ]") if not item["done"] else self.style.hair("[x]")
            body = item["text"] if not item["done"] else self.style.strike(item["text"])
            self._print(f"  {index:>2} {marker} {body}")
        open_count = sum(1 for t in self.todos if not t["done"])
        if open_count:
            self._print("")
            self._print(self.style.dim(f"  {open_count} open · /todo done <n> to check one off · /todo rm <n> to drop one"))

    def mark_todo_done(self: "ConsoleSession", query: str) -> bool:
        """Mark an item done by 1-based number or text match. Returns True when found."""
        index = self._find_todo(query, open_only=True)
        if index is None:
            # Distinguish "not in the list at all" from "already done".
            existing = self._find_todo(query, open_only=False)
            if existing is not None:
                self._print(self.style.dim(f"  already done: {self.todos[existing]['text']}"))
                return False
            self._print(self.style.dim("  no open todo matches - /todo lists them"))
            return False
        item = self.todos[index]
        if item["done"]:
            self._print(self.style.dim(f"  already done: {item['text']}"))
            return False
        item["done"] = True
        self._print(self.style.dim(f"  done: {item['text']}"))
        remaining = sum(1 for t in self.todos if not t["done"])
        if not remaining:
            self._print(self.style.dim("  all todos done - /todo clear to drop the list"))
        return True

    def rm_todo(self: "ConsoleSession", query: str) -> bool:
        """Drop an item by 1-based number or text match. Returns True when found."""
        index = self._find_todo(query, open_only=False)
        if index is None:
            self._print(self.style.dim("  no todo matches - /todo lists them"))
            return False
        removed = self.todos.pop(index)["text"]
        self._print(self.style.dim(f"  removed: {removed}"))
        return True

    def clear_todos(self: "ConsoleSession") -> None:
        if not self.todos:
            self._print(self.style.dim("  no todos to clear"))
            return
        count = len(self.todos)
        self.todos = []
        self._print(self.style.dim(f"  cleared {count} todos"))

    def _todo_status_snippet(self: "ConsoleSession") -> str:
        """Styled open-item count for the border row while a turn runs.

        Deprecated: no border row exists since the frame shims were
        removed; kept because the test suite still exercises it.
        Empty string when nothing is open, so the spinner row only gains
        the ``[ ] N open`` readout when the checklist actually has work
        left - and it drains live as the agent checks items off.
        """
        open_count = sum(1 for t in self.todos if not t["done"])
        if not open_count:
            return ""
        plural = "" if open_count == 1 else "s"
        return self.style.dim(f"[ ] {open_count} open item{plural}")

    def _check_todo_completion(self: "ConsoleSession", result: "RunResult | None") -> None:
        """Apply the agent's TODO reports from its final message.

        Two reports, each on its own line:

        - ``TODO DONE: <text>`` checks an open item off. Matching is
          exact after normalising whitespace, so the agent echoing an
          item's text is the only thing that checks it off; a paraphrase
          does nothing and the operator can mark it with /todo done <n>.
        - ``TODO ADD: <text>`` appends a new item the agent discovered
          along the way (follow-up work a task turned up). Deduplicated
          against the list verbatim so repeated reports do not stack.

        The agent can add and complete items, but never remove or edit
        them - the operator owns the list.

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
        # ASCII checkboxes, dimmed: an open item carries the [ ] marker
        # (the same mark /todo shows), a finished one the [x] with
        # the text struck through - so a note reads exactly like a row of
        # the checklist, only quieter than the reply around it.
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
