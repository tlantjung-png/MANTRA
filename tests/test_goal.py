"""Tests for /goal and /todo - what a session is working toward.

The claim that matters: a goal or a todo item set on turn one still
shapes turn ten. Everything else is bookkeeping.
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
from core.console import ConsoleSession, Style, _goal, _todo  # noqa: E402

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


class GoalTest(_IsolatedSessionTest):

    # ---- setting and showing -----------------------------------------

    def test_a_goal_is_set_from_the_whole_line(self):
        _goal(self.session, "ship the dashboard")
        self.assertEqual(self.session.goal, "ship the dashboard")

    def test_a_multi_word_goal_keeps_its_spaces(self):
        _goal(self.session, "make the container fill the frame")
        self.assertEqual(self.session.goal, "make the container fill the frame")

    def test_an_empty_invocation_shows_the_goal(self):
        _goal(self.session, "ship it")
        with mock.patch.object(self.session, "show_goal") as shown:
            _goal(self.session, "")
        shown.assert_called_once()

    def test_showing_with_no_goal_says_so(self):
        printed: list[str] = []
        with mock.patch.object(self.session, "_print", side_effect=printed.append):
            self.session.show_goal()
        self.assertTrue(any("no goal set" in str(p) for p in printed))

    def test_showing_prints_the_goal(self):
        _goal(self.session, "fix the borders")
        printed: list[str] = []
        with mock.patch.object(self.session, "_print", side_effect=printed.append):
            self.session.show_goal()
        self.assertIn("fix the borders", " ".join(str(p) for p in printed))

    # ---- the injection is the point ----------------------------------

    def test_the_goal_reaches_the_system_prompt(self):
        _goal(self.session, "ship the dashboard")
        self.assertIn("ship the dashboard", self.session._effective_system_prompt())

    def test_without_a_goal_the_prompt_is_untouched(self):
        self.assertEqual(
            self.session._effective_system_prompt(), self.session.system_prompt
        )

    def test_the_goal_outlives_individual_turns(self):
        # The whole reason this exists: turn ten must still be aiming at
        # what turn one was told to do. Run two real turns, not a faked
        # message_count bump.
        from core.logs import JsonlLogger
        from core.scripted import ScriptedLLMClient, final_response

        self.session.logger = JsonlLogger(os.path.join(self.tmp, "session.jsonl"))
        _goal(self.session, "ship the dashboard")
        self.session.llm = ScriptedLLMClient(
            [final_response("turn one done"), final_response("turn two done")]
        )
        self.session.handle("turn one")
        self.session.handle("turn two")
        self.assertIn("ship the dashboard", self.session._effective_system_prompt())

    def test_the_prompt_tells_the_agent_to_say_when_it_is_done(self):
        _goal(self.session, "x")
        self.assertIn("GOAL COMPLETE", self.session._effective_system_prompt())

    def test_the_base_instructions_survive_the_goal_being_set(self):
        base = self.session.system_prompt
        _goal(self.session, "y")
        self.assertTrue(self.session._effective_system_prompt().startswith(base))

    # ---- notes --------------------------------------------------------

    def test_a_note_is_recorded(self):
        _goal(self.session, "ship it")
        _goal(self.session, "note use the light frame")
        self.assertEqual(self.session.goal_notes, ["use the light frame"])

    def test_notes_reach_the_system_prompt(self):
        _goal(self.session, "ship it")
        _goal(self.session, "note use the light frame")
        self.assertIn("use the light frame", self.session._effective_system_prompt())

    def test_a_note_without_a_goal_is_refused(self):
        printed: list[str] = []
        with mock.patch.object(self.session, "_print", side_effect=printed.append):
            _goal(self.session, "note orphan")
        self.assertEqual(self.session.goal_notes, [])
        self.assertTrue(any("set a goal first" in str(p) for p in printed))

    def test_an_empty_note_is_a_usage_message_not_a_blank_note(self):
        _goal(self.session, "ship it")
        _goal(self.session, "note")
        self.assertEqual(self.session.goal_notes, [])

    def test_notes_are_shown_with_the_goal(self):
        _goal(self.session, "ship it")
        _goal(self.session, "note first")
        _goal(self.session, "note second")
        printed: list[str] = []
        with mock.patch.object(self.session, "_print", side_effect=printed.append):
            self.session.show_goal()
        shown = " ".join(str(p) for p in printed)
        self.assertIn("first", shown)
        self.assertIn("second", shown)

    # ---- clearing -----------------------------------------------------

    def test_done_clears_the_goal(self):
        _goal(self.session, "ship it")
        _goal(self.session, "done")
        self.assertEqual(self.session.goal, "")

    def test_clear_and_drop_also_clear(self):
        for word in ("clear", "drop"):
            _goal(self.session, "ship it")
            _goal(self.session, word)
            self.assertEqual(self.session.goal, "", word)

    def test_clearing_clears_the_notes_too(self):
        _goal(self.session, "ship it")
        _goal(self.session, "note a note")
        _goal(self.session, "done")
        self.assertEqual(self.session.goal_notes, [])

    def test_clearing_a_goal_that_was_never_set_says_so(self):
        printed: list[str] = []
        with mock.patch.object(self.session, "_print", side_effect=printed.append):
            self.session.clear_goal()
        self.assertTrue(any("no goal set" in str(p) for p in printed))

    def test_cleared_the_goal_leaves_the_prompt_alone(self):
        _goal(self.session, "ship it")
        _goal(self.session, "done")
        self.assertEqual(
            self.session._effective_system_prompt(), self.session.system_prompt
        )

    # ---- completion detection -----------------------------------------

    def _result(self, text):
        class R:
            # Minimal stand-in for RunResult: only final_message is read.
            final_message = text

        return R()

    def test_an_agent_reporting_completion_is_surfaced(self):
        _goal(self.session, "ship it")
        printed: list[str] = []
        with mock.patch.object(self.session, "_print", side_effect=printed.append):
            self.session._check_goal_completion(self._result("GOAL COMPLETE: shipped"))
        self.assertTrue(any("reports the goal is met" in str(p) for p in printed))

    def test_completion_detection_is_case_insensitive(self):
        _goal(self.session, "ship it")
        printed: list[str] = []
        with mock.patch.object(self.session, "_print", side_effect=printed.append):
            self.session._check_goal_completion(self._result("Goal complete."))
        self.assertTrue(any("reports the goal is met" in str(p) for p in printed))

    def test_an_ordinary_reply_is_not_read_as_completion(self):
        _goal(self.session, "ship it")
        with mock.patch.object(self.session, "_print") as printed:
            self.session._check_goal_completion(self._result("I made some progress."))
        printed.assert_not_called()

    def test_completion_is_ignored_when_no_goal_is_set(self):
        with mock.patch.object(self.session, "_print") as printed:
            self.session._check_goal_completion(self._result("GOAL COMPLETE"))
        printed.assert_not_called()

    def test_the_agent_clears_nothing_itself(self):
        # Reporting is not clearing: a wrong claim must not lose the goal.
        _goal(self.session, "ship it")
        self.session._check_goal_completion(self._result("GOAL COMPLETE"))
        self.assertEqual(self.session.goal, "ship it")

    # ---- persistence ---------------------------------------------------

    def test_the_goal_survives_a_resume(self):
        _goal(self.session, "ship the dashboard")
        _goal(self.session, "note use the light frame")
        self.session.context.messages = _messages()
        self.session.autosave()
        name = self.session.session_name

        self.session.goal = ""
        self.session.goal_notes = []
        self.session.resume_session(name)
        self.assertEqual(self.session.goal, "ship the dashboard")
        self.assertEqual(self.session.goal_notes, ["use the light frame"])

    def test_a_session_without_a_goal_restores_to_no_goal(self):
        self.session.context.messages = _messages()
        self.session.autosave()
        name = self.session.session_name
        self.session.goal = "stale"
        self.session.resume_session(name)
        self.assertEqual(self.session.goal, "")

    # ---- dispatch ------------------------------------------------------

    def test_goal_is_in_the_command_table(self):
        from core.console import SLASH_COMMANDS

        self.assertIn("/goal", [c for c, _ in SLASH_COMMANDS])

    def test_goal_is_in_the_help_text(self):
        from core.console import HELP_TEXT

        self.assertIn("/goal", HELP_TEXT)

    def test_dispatch_routes_to_the_goal_handler(self):
        from core.console import dispatch

        with mock.patch("core.console._goal") as handled:
            dispatch(self.session, "/goal do the thing")
        handled.assert_called_once_with(self.session, "do the thing")


class TodoListTest(_IsolatedSessionTest):
    """The /todo checklist: add, show, check off, remove, clear, persist."""

    # ---- add / show ------------------------------------------------

    def test_an_item_is_added_whole_from_the_line(self):
        _todo(self.session, "add fix the header spacing")
        self.assertEqual(self.session.todos, [{"text": "fix the header spacing", "done": False}])

    def test_new_is_an_alias_for_add(self):
        _todo(self.session, "new write the tests")
        self.assertEqual(self.session.todos, [{"text": "write the tests", "done": False}])

    def test_an_empty_add_is_a_usage_message(self):
        printed: list[str] = []
        with mock.patch.object(self.session, "_print", side_effect=printed.append):
            _todo(self.session, "add")
        self.assertEqual(self.session.todos, [])
        self.assertTrue(any("usage" in str(p) for p in printed))

    def test_bare_todo_shows_the_list(self):
        _todo(self.session, "add first")
        with mock.patch.object(self.session, "show_todos") as shown:
            _todo(self.session, "")
        shown.assert_called_once_with()

    def test_showing_with_no_todos_says_so(self):
        printed: list[str] = []
        with mock.patch.object(self.session, "_print", side_effect=printed.append):
            self.session.show_todos()
        self.assertTrue(any("no todos" in str(p) for p in printed))

    def test_showing_prints_each_item(self):
        _todo(self.session, "add first")
        _todo(self.session, "add second")
        printed: list[str] = []
        with mock.patch.object(self.session, "_print", side_effect=printed.append):
            self.session.show_todos()
        joined = " ".join(str(p) for p in printed)
        self.assertIn("first", joined)
        self.assertIn("second", joined)

    # ---- done / rm --------------------------------------------------

    def test_done_by_number_marks_the_item(self):
        _todo(self.session, "add first")
        _todo(self.session, "add second")
        _todo(self.session, "done 2")
        self.assertEqual(self.session.todos[0]["done"], False)
        self.assertEqual(self.session.todos[1]["done"], True)

    def test_done_by_full_text_marks_the_item(self):
        _todo(self.session, "add fix the header")
        _todo(self.session, "done fix the header")
        self.assertEqual(self.session.todos[0]["done"], True)

    def test_done_by_text_is_exact_not_a_fragment(self):
        # Exact text match only: a typo'd fragment must not silently
        # mark a longer item done.
        _todo(self.session, "add fix the header and the footer")
        _todo(self.session, "done the header")
        _todo(self.session, "done footer")
        self.assertEqual(self.session.todos[0]["done"], False)

    def test_done_by_number_of_a_done_item_is_refused(self):
        _todo(self.session, "add first")
        _todo(self.session, "done 1")
        printed: list[str] = []
        with mock.patch.object(self.session, "_print", side_effect=printed.append):
            _todo(self.session, "done 1")
        self.assertTrue(any("already done" in str(p) for p in printed))

    def test_done_bare_opens_no_picker_when_nothing_is_open(self):
        _todo(self.session, "add first")
        _todo(self.session, "done 1")
        printed: list[str] = []
        with mock.patch.object(self.session, "_print", side_effect=printed.append):
            _todo(self.session, "done")
        self.assertTrue(any("nothing open" in str(p) for p in printed))

    def test_rm_by_number_drops_the_item(self):
        _todo(self.session, "add keep")
        _todo(self.session, "add drop")
        _todo(self.session, "rm 2")
        self.assertEqual([t["text"] for t in self.session.todos], ["keep"])

    def test_rm_removes_done_items_by_text_too(self):
        _todo(self.session, "add fix the header")
        _todo(self.session, "done 1")
        _todo(self.session, "rm fix the header")
        self.assertEqual(self.session.todos, [])

    def test_bare_rm_drops_only_done_items(self):
        _todo(self.session, "add finished")
        _todo(self.session, "add still open")
        _todo(self.session, "done 1")
        _todo(self.session, "rm")
        self.assertEqual([t["text"] for t in self.session.todos], ["still open"])

    def test_clear_empties_the_list(self):
        _todo(self.session, "add first")
        _todo(self.session, "add second")
        _todo(self.session, "clear")
        self.assertEqual(self.session.todos, [])

    # ---- the injection is the point ---------------------------------

    def test_open_todos_reach_the_system_prompt(self):
        _todo(self.session, "add fix the header")
        prompt = self.session._effective_system_prompt()
        self.assertIn("Session todo list", prompt)
        self.assertIn("- [ ] fix the header", prompt)

    def test_done_items_stay_listed_as_checked(self):
        _todo(self.session, "add first")
        _todo(self.session, "add second")
        _todo(self.session, "done 1")
        prompt = self.session._effective_system_prompt()
        self.assertIn("- [x] first", prompt)
        self.assertIn("- [ ] second", prompt)

    def test_the_prompt_tells_the_agent_how_to_report_done_items(self):
        _todo(self.session, "add first")
        prompt = self.session._effective_system_prompt()
        self.assertIn("TODO DONE", prompt)
        self.assertIn("TODO ADD", prompt)

    def test_without_todos_the_prompt_is_untouched(self):
        self.assertEqual(
            self.session._effective_system_prompt(), self.session.system_prompt
        )

    def test_the_base_instructions_survive_todos_being_set(self):
        base = self.session.system_prompt
        _todo(self.session, "add first")
        self.assertTrue(self.session._effective_system_prompt().startswith(base))

    def test_goal_and_todos_coexist_in_the_prompt(self):
        _goal(self.session, "ship it")
        _todo(self.session, "add write the tests")
        prompt = self.session._effective_system_prompt()
        self.assertIn("Standing goal", prompt)
        self.assertIn("Session todo list", prompt)

    # ---- agent-reported completion ----------------------------------

    def _result(self, text):
        class R:
            # Minimal stand-in for RunResult: only final_message is read.
            final_message = text

        return R()

    def test_an_agent_report_checks_off_the_matching_item(self):
        _todo(self.session, "add fix the header")
        self.session._check_todo_completion(
            self._result("Done.\nTODO DONE: fix the header\nAnything else?")
        )
        self.assertEqual(self.session.todos[0]["done"], True)

    def test_reports_match_case_insensitively(self):
        _todo(self.session, "add fix the header")
        self.session._check_todo_completion(self._result("todo done: Fix The  Header"))
        self.assertEqual(self.session.todos[0]["done"], True)

    def test_a_paraphrase_checks_nothing_off(self):
        _todo(self.session, "add fix the header")
        self.session._check_todo_completion(self._result("TODO DONE: I fixed it"))
        self.assertEqual(self.session.todos[0]["done"], False)

    def test_an_ordinary_reply_is_not_read_as_a_report(self):
        _todo(self.session, "add first")
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
        # silently, and it rides along like an operator-added item.
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
        _todo(self.session, "add old item")
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

    # ---- inline presentation -------------------------------------------

    def test_a_streamed_add_note_carries_the_open_checkbox_and_count(self):
        _todo(self.session, "add ship the dashboard")
        note = self.session._handle_stream_todo_report(
            "TODO ADD: fix the lint errors in main.py"
        )
        # Open items lead with the ASCII [ ] marker, and the running
        # count of open items is right there in the note.
        self.assertIn("[ ]", note)
        self.assertIn("fix the lint errors in main.py", note)
        self.assertIn("2 open", note)

    def test_a_streamed_done_note_carries_the_done_checkbox(self):
        _todo(self.session, "add ship the dashboard")
        note = self.session._handle_stream_todo_report(
            "TODO DONE: ship the dashboard"
        )
        self.assertIn("[x]", note)
        self.assertNotIn("[ ]", note)

    def test_a_repeated_stream_report_swallows_its_line(self):
        # The same report arriving twice in one stream applies once and
        # the second line is swallowed (empty note, nothing shown).
        _todo(self.session, "add fix the header")
        self.session._handle_stream_todo_report("TODO DONE: fix the header")
        second = self.session._handle_stream_todo_report("TODO DONE: fix the header")
        self.assertEqual(second, "")
        self.assertEqual(self.session.todos[0]["done"], True)

    def test_status_snippet_counts_only_open_items(self):
        _todo(self.session, "add first")
        _todo(self.session, "add second")
        _todo(self.session, "done 1")
        snippet = self.session._todo_status_snippet()
        self.assertIn("[ ]", snippet)
        self.assertIn("1 open item", snippet)

    def test_status_snippet_is_empty_when_nothing_is_open(self):
        _todo(self.session, "add only")
        _todo(self.session, "done 1")
        self.assertEqual(self.session._todo_status_snippet(), "")

    def test_multiple_reports_check_off_each_item(self):
        _todo(self.session, "add first")
        _todo(self.session, "add second")
        self.session._check_todo_completion(
            self._result("TODO DONE: first\nTODO DONE: second\nall set")
        )
        self.assertEqual([t["done"] for t in self.session.todos], [True, True])

    # ---- persistence ---------------------------------------------------

    def test_todos_survive_a_resume(self):
        _todo(self.session, "add fix the header")
        _todo(self.session, "add write tests")
        _todo(self.session, "done 1")
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

    # ---- dispatch ------------------------------------------------------

    def test_todo_is_in_the_command_table(self):
        from core.console import SLASH_COMMANDS

        self.assertIn("/todo", [c for c, _ in SLASH_COMMANDS])

    def test_todo_is_in_the_help_text(self):
        from core.console import HELP_TEXT

        self.assertIn("/todo", HELP_TEXT)

    def test_dispatch_routes_to_the_todo_handler(self):
        from core.console import dispatch

        with mock.patch("core.console._todo") as handled:
            dispatch(self.session, "/todo add fix the header")
        handled.assert_called_once_with(self.session, "add fix the header")


if __name__ == "__main__":
    unittest.main()
