"""Tests for the in-conversation todo checklist.

There is no /todo command: the checklist is the agent's to grow and
drain inside the conversation, via TODO ADD / TODO DONE lines in its
reply. The operator watches it in the stream. What these tests hold in
place is the mechanism - prompt injection, report application, inline
presentation, and persistence - and the fact that the old command is
gone rather than lurking as an alias.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "."))
_tests_dir = os.path.dirname(os.path.abspath(__file__))
if _tests_dir not in sys.path:
    sys.path.insert(0, _tests_dir)

import core.agent.sessions as sessions  # noqa: E402
from core.console import ConsoleSession, Style  # noqa: E402

from _helpers import messages as _messages  # noqa: E402


class _IsolatedSessionTest(unittest.TestCase):
    """A fresh session in its own settings/session store per test."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        for var in ("MANTRA_SETTINGS", sessions._OVERRIDE_ENV):
            # Restore whatever the harness had set (e.g. the conftest's
            # redirect) rather than popping: deleting the variable would
            # silently send later tests back to the real store.
            prior = os.environ.get(var)
            if prior is not None:
                # addCleanup is LIFO: this runs after the pop below.
                self.addCleanup(os.environ.setdefault, var, prior)
            self.addCleanup(os.environ.pop, var, None)
        os.environ["MANTRA_SETTINGS"] = os.path.join(self.tmp, "config.json")
        os.environ[sessions._OVERRIDE_ENV] = os.path.join(self.tmp, "sessions")

        from core.config import merge_defaults
        from core.scripted import ScriptedLLMClient

        self.session = ConsoleSession(
            config=merge_defaults({}),
            workspace=self.tmp,
            style=Style(enabled=False),
            llm=ScriptedLLMClient([]),
            ask=lambda prompt: "y",  # every approval auto-answered yes
        )

    def _seed(self, *texts: str) -> None:
        """Put items on the checklist directly - no command exists to."""
        self.session.todos = [{"text": t, "done": False} for t in texts]

    def _result(self, text):
        class R:
            # Minimal stand-in for RunResult: only final_message is read.
            final_message = text

        return R()


class TodoInjectionTest(_IsolatedSessionTest):
    """The injection is the point: the checklist shapes every turn."""

    def test_open_todos_reach_the_system_prompt(self):
        self._seed("fix the header")
        prompt = self.session._effective_system_prompt()
        self.assertIn("Session todo list", prompt)
        self.assertIn("- [ ] fix the header", prompt)

    def test_done_items_stay_listed_as_checked(self):
        self._seed("first", "second")
        self.session.todos[0]["done"] = True
        prompt = self.session._effective_system_prompt()
        self.assertIn("- [x] first", prompt)
        self.assertIn("- [ ] second", prompt)

    def test_the_prompt_tells_the_agent_how_to_report(self):
        self._seed("first")
        prompt = self.session._effective_system_prompt()
        self.assertIn("TODO DONE", prompt)
        self.assertIn("TODO ADD", prompt)

    def test_without_todos_the_prompt_is_untouched(self):
        self.assertEqual(
            self.session._effective_system_prompt(), self.session.system_prompt
        )

    def test_the_base_instructions_survive_todos_being_set(self):
        base = self.session.system_prompt
        self._seed("first")
        self.assertTrue(self.session._effective_system_prompt().startswith(base))


class TodoReportTest(_IsolatedSessionTest):
    """The agent grows and drains the list from inside its reply."""

    def test_an_agent_report_checks_off_the_matching_item(self):
        self._seed("fix the header")
        self.session._check_todo_completion(
            self._result("Done.\nTODO DONE: fix the header\nAnything else?")
        )
        self.assertEqual(self.session.todos[0]["done"], True)

    def test_reports_match_case_insensitively(self):
        self._seed("fix the header")
        self.session._check_todo_completion(self._result("todo done: Fix The  Header"))
        self.assertEqual(self.session.todos[0]["done"], True)

    def test_a_paraphrase_checks_nothing_off(self):
        self._seed("fix the header")
        self.session._check_todo_completion(self._result("TODO DONE: I fixed it"))
        self.assertEqual(self.session.todos[0]["done"], False)

    def test_an_ordinary_reply_is_not_read_as_a_report(self):
        self._seed("first")
        with mock.patch.object(self.session, "_print") as printed:
            self.session._check_todo_completion(self._result("I made progress."))
        printed.assert_not_called()

    def test_reports_are_ignored_when_the_list_is_empty(self):
        # A TODO DONE for an item nobody added changes nothing - the
        # report simply does not match. (TODO ADD is the one report that
        # works on an empty list: it grows it.)
        with mock.patch.object(self.session, "_print") as printed:
            self.session._check_todo_completion(self._result("TODO DONE: anything"))
        printed.assert_not_called()
        self.assertEqual(self.session.todos, [])

    def test_an_agent_report_adds_a_follow_up_item(self):
        # A task that turned up real follow-up work: the agent adds it
        # silently, and it rides along like any other item.
        self.session._check_todo_completion(
            self._result("The build works, but the linter flags main.py.\nTODO ADD: fix the lint errors in main.py")
        )
        self.assertEqual(
            self.session.todos,
            [{"text": "fix the lint errors in main.py", "done": False}],
        )

    def test_repeated_adds_of_the_same_item_do_not_stack(self):
        self.session._check_todo_completion(self._result("TODO ADD: fix the lint"))
        self.session._check_todo_completion(self._result("TODO ADD: fix the  lint"))
        self.assertEqual(self.session.todos, [{"text": "fix the lint", "done": False}])

    def test_an_add_report_is_case_insensitive(self):
        self.session._check_todo_completion(self._result("todo add: Fix The Header"))
        self.assertEqual(self.session.todos, [{"text": "Fix The Header", "done": False}])

    def test_add_and_done_reports_interleave_in_one_message(self):
        self._seed("old item")
        self.session._check_todo_completion(
            self._result(
                "TODO DONE: old item\n"
                "TODO ADD: new follow-up\n"
                "TODO DONE: new follow-up"
            )
        )
        self.assertEqual(
            [t["text"] for t in self.session.todos], ["old item", "new follow-up"]
        )
        self.assertEqual([t["done"] for t in self.session.todos], [True, True])

    def test_an_item_completed_in_the_same_message_it_is_added(self):
        # The agent lists a done item first and the add that created it
        # after - order in the message must not matter.
        self.session._check_todo_completion(
            self._result(
                "TODO DONE: fix the lint\n"
                "TODO ADD: fix the lint\n"
            )
        )
        self.assertEqual(
            [t["text"] for t in self.session.todos], ["fix the lint"]
        )
        self.assertEqual(self.session.todos[0]["done"], True)

    def test_multiple_reports_check_off_each_item(self):
        self._seed("first", "second")
        self.session._check_todo_completion(
            self._result("TODO DONE: first\nTODO DONE: second\nall set")
        )
        self.assertEqual([t["done"] for t in self.session.todos], [True, True])


class TodoStreamPresentationTest(_IsolatedSessionTest):
    """The report line never reaches the screen; a quiet note does."""

    def test_a_streamed_add_note_carries_the_open_checkbox_and_count(self):
        self._seed("ship the dashboard")
        note = self.session._handle_stream_todo_report(
            "TODO ADD: fix the lint errors in main.py"
        )
        # Open items lead with the ASCII [ ] marker, and the running
        # count of open items is right there in the note.
        self.assertIn("[ ]", note)
        self.assertIn("fix the lint errors in main.py", note)
        self.assertIn("2 open", note)

    def test_a_streamed_done_note_carries_the_done_checkbox(self):
        self._seed("ship the dashboard")
        note = self.session._handle_stream_todo_report(
            "TODO DONE: ship the dashboard"
        )
        self.assertIn("[x]", note)
        self.assertNotIn("[ ]", note)

    def test_a_repeated_stream_report_swallows_its_line(self):
        # The same report arriving twice in one stream applies once and
        # the second line is swallowed (empty note, nothing shown).
        self._seed("fix the header")
        self.session._handle_stream_todo_report("TODO DONE: fix the header")
        second = self.session._handle_stream_todo_report("TODO DONE: fix the header")
        self.assertEqual(second, "")
        self.assertEqual(self.session.todos[0]["done"], True)


class TodoPersistenceTest(_IsolatedSessionTest):
    """The checklist travels with the conversation."""

    def test_todos_survive_a_resume(self):
        self._seed("fix the header", "write tests")
        self.session.todos[0]["done"] = True
        self.session.context.messages = _messages()
        self.session.autosave()
        name = self.session.session_name

        self.session.todos = []
        self.session.resume_session(name)
        self.assertEqual(
            self.session.todos,
            [
                {"text": "fix the header", "done": True},
                {"text": "write tests", "done": False},
            ],
        )

    def test_a_session_without_todos_restores_to_an_empty_list(self):
        self.session.context.messages = _messages()
        self.session.autosave()
        name = self.session.session_name
        self.session.todos = [{"text": "stale", "done": False}]
        self.session.resume_session(name)
        self.assertEqual(self.session.todos, [])

    def test_a_cleared_conversation_keeps_the_checklist(self):
        # Clearing the chat clears the conversation, not the work: the
        # checklist outlives it and rides into the forked session file.
        from core.console import dispatch

        self._seed("still relevant")
        with mock.patch.object(self.session, "_print"):
            dispatch(self.session, "/clear")
        self.assertEqual([t["text"] for t in self.session.todos], ["still relevant"])


class TodoCommandRemovalTest(_IsolatedSessionTest):
    """/todo and /goal are gone, not hidden."""

    def test_removed_commands_are_unknown(self):
        from core.console import HELP_TEXT, SLASH_COMMANDS, dispatch

        for cmd in ("/todo", "/goal"):
            printed: list[str] = []
            with mock.patch.object(self.session, "_print", side_effect=printed.append):
                dispatch(self.session, cmd)
            self.assertTrue(any("unknown command" in str(p) for p in printed), cmd)
            self.assertFalse(any(c == cmd for c, _ in SLASH_COMMANDS), cmd)
            self.assertNotIn(cmd, HELP_TEXT, cmd)


if __name__ == "__main__":
    unittest.main()
