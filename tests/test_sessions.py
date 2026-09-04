"""Tests for saved/resumable sessions.

``MANTRA_SESSIONS`` points the store at a temp directory, so nothing
here can see or disturb the operator's real ``~/.mantra/sessions``.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import mantra.core.sessions as sessions
from mantra.console import ConsoleSession, Style


def _messages(count=2):
    out = [{"role": "system", "content": "you are MANTRA"}]
    for i in range(count):
        out.append({"role": "user", "content": f"question {i}"})
        out.append({"role": "assistant", "content": f"answer {i}"})
    return out


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        # Restore whatever the harness had set (e.g. the conftest's
        # redirect) rather than popping: deleting the variable would
        # silently send later tests back to the real store. Pop first
        # (LIFO), then put the prior value back when there was one.
        prior = os.environ.get(sessions._OVERRIDE_ENV)
        if prior is not None:
            # addCleanup is LIFO: this runs after the pop below, restoring
            # whatever the harness had set.
            self.addCleanup(os.environ.setdefault, sessions._OVERRIDE_ENV, prior)
        self.addCleanup(os.environ.pop, sessions._OVERRIDE_ENV, None)
        os.environ[sessions._OVERRIDE_ENV] = self.tmp

    def test_a_saved_session_round_trips(self):
        sessions.save("alpha", {"messages": _messages(), "workspace": "/w"})
        loaded = sessions.load("alpha")
        self.assertIsNotNone(loaded)
        self.assertEqual(len(loaded["messages"]), 5)
        self.assertEqual(loaded["workspace"], "/w")

    def test_a_missing_session_is_none_not_an_exception(self):
        self.assertIsNone(sessions.load("nope"))

    def test_a_corrupt_file_is_skipped_by_the_listing(self):
        sessions.save("good", {"messages": _messages()})
        with open(os.path.join(self.tmp, "bad.json"), "w", encoding="utf-8") as fh:
            fh.write("{not json")
        names = [item["name"] for item in sessions.list_sessions()]
        self.assertEqual(names, ["good"])

    def test_a_file_without_messages_is_not_a_session(self):
        sessions.save("empty", {"messages": []})
        self.assertEqual(sessions.list_sessions(), [])

    def test_the_listing_is_newest_first(self):
        sessions.save("old", {"messages": _messages()})
        sessions.save("new", {"messages": _messages()})
        os.utime(os.path.join(self.tmp, "old.json"), (1, 1))
        os.utime(os.path.join(self.tmp, "new.json"), (2, 2))
        self.assertEqual([i["name"] for i in sessions.list_sessions()], ["new", "old"])

    def test_the_summary_is_the_first_user_message(self):
        sessions.save("s", {"messages": _messages(), "summary": ""})
        self.assertEqual(sessions.list_sessions()[0]["summary"], "question 0")

    def test_multimodal_content_blocks_are_summarised_too(self):
        blocks = [{"type": "text", "text": "look at this"}]
        sessions.save("m", {"messages": [{"role": "user", "content": blocks}]})
        self.assertEqual(sessions.list_sessions()[0]["summary"], "look at this")

    def test_turns_counts_user_messages(self):
        sessions.save("t", {"messages": _messages(3)})
        self.assertEqual(sessions.list_sessions()[0]["turns"], 3)

    def test_delete_removes_it(self):
        sessions.save("gone", {"messages": _messages()})
        self.assertTrue(sessions.delete("gone"))
        self.assertIsNone(sessions.load("gone"))

    def test_deleting_a_missing_session_is_false(self):
        self.assertFalse(sessions.delete("nope"))

    def test_latest_is_the_newest(self):
        sessions.save("a", {"messages": _messages()})
        sessions.save("b", {"messages": _messages()})
        os.utime(os.path.join(self.tmp, "a.json"), (1, 1))
        os.utime(os.path.join(self.tmp, "b.json"), (2, 2))
        self.assertEqual(sessions.latest()["name"], "b")

    def test_latest_is_none_when_empty(self):
        self.assertIsNone(sessions.latest())


class DeriveNameTest(unittest.TestCase):
    def test_a_workspace_gives_its_directory_name(self):
        name = sessions.derive_name("C:\\Users\\arif-\\K-CHAT")
        self.assertTrue(name.startswith("k-chat-"), name)

    def test_the_name_carries_a_sortable_stamp(self):
        name = sessions.derive_name("C:\\work\\proj")
        self.assertRegex(name, r"^proj-\d{8}-\d{6}$")

    def test_a_bare_name_still_works(self):
        self.assertRegex(sessions.derive_name(), r"^\d{8}-\d{6}$")

    def test_the_model_is_only_a_fallback(self):
        self.assertTrue(sessions.derive_name("", "gpt-4o").startswith("gpt-4o-"))

    def test_two_sessions_in_one_directory_do_not_collide(self):
        first = sessions.derive_name("C:\\work\\proj")
        sessions.save(first, {"messages": _messages()})
        # Same-second names must still diverge via the collision suffix.
        with mock.patch.object(sessions.time, "strftime", return_value="20260101-000000"):
            second = sessions.derive_name("C:\\work\\proj")
            sessions.save(second, {"messages": _messages()})
        self.assertNotEqual(first, second)


class SessionTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.addCleanup(os.environ.pop, "MANTRA_SETTINGS", None)
        prior = os.environ.get(sessions._OVERRIDE_ENV)
        if prior is not None:
            # addCleanup is LIFO: this runs after the pop below, restoring
            # whatever the harness had set.
            self.addCleanup(os.environ.setdefault, sessions._OVERRIDE_ENV, prior)
        self.addCleanup(os.environ.pop, sessions._OVERRIDE_ENV, None)
        os.environ["MANTRA_SETTINGS"] = os.path.join(self.tmp, "config.json")
        os.environ[sessions._OVERRIDE_ENV] = os.path.join(self.tmp, "sessions")

    def _session(self):
        from mantra.config import merge_defaults
        from mantra.core.context import ContextManager
        from mantra.implementations.llm.mock_client import ScriptedLLMClient

        session = ConsoleSession(
            config=merge_defaults({}),
            workspace=self.tmp,
            style=Style(enabled=False),
            llm=ScriptedLLMClient([]),
            ask=lambda prompt: "y",
        )
        session.context = ContextManager(max_messages=50, max_chars=100000)  # room for multi-turn tests
        return session


class AutosaveTest(SessionTestBase):
    def test_nothing_is_saved_before_there_is_a_conversation(self):
        session = self._session()
        session.autosave()
        self.assertEqual(sessions.list_sessions(), [])

    def test_a_turn_saves_a_session(self):
        session = self._session()
        session.context.messages = _messages()
        session.autosave()
        self.assertEqual(len(sessions.list_sessions()), 1)

    def test_the_saved_name_is_remembered_so_it_is_not_forked(self):
        session = self._session()
        session.context.messages = _messages()
        session.autosave()
        first = session.session_name
        session.context.messages += [{"role": "user", "content": "more"}]
        session.autosave()
        self.assertEqual(session.session_name, first)
        self.assertEqual(len(sessions.list_sessions()), 1)

    def test_autosave_is_silent(self):
        session = self._session()
        session.context.messages = _messages()
        with mock.patch.object(session, "_print") as printed:
            session.autosave()
        printed.assert_not_called()

    def test_the_summary_comes_from_the_first_question(self):
        session = self._session()
        session.context.messages = _messages()
        session.autosave()
        self.assertEqual(sessions.latest()["summary"], "question 0")


class ResumeTest(SessionTestBase):
    def test_resuming_restores_the_messages(self):
        session = self._session()
        session.context.messages = _messages(3)
        session.autosave()
        name = session.session_name

        session.context.messages = [{"role": "system", "content": "x"}]
        self.assertTrue(session.resume_session(name))
        self.assertEqual(len(session.context.messages), 7)

    def test_resuming_adopts_the_name_so_it_continues_it(self):
        session = self._session()
        session.context.messages = _messages()
        session.autosave()
        name = session.session_name
        session.session_name = ""
        session.resume_session(name)
        self.assertEqual(session.session_name, name)

    def test_resuming_an_unknown_name_is_false_and_says_so(self):
        session = self._session()
        session.context.messages = _messages()
        session.autosave()
        with mock.patch.object(session, "_print") as printed:
            self.assertFalse(session.resume_session("nope"))
        self.assertTrue(any("no session named" in str(c) for c in printed.call_args_list))

    def test_resuming_with_no_sessions_at_all_says_so(self):
        session = self._session()
        with mock.patch.object(session, "_print") as printed:
            self.assertFalse(session.resume_session("nope"))
        self.assertTrue(any("no saved sessions" in str(c) for c in printed.call_args_list))

    def test_the_turn_counter_follows_the_restored_conversation(self):
        session = self._session()
        session.context.messages = _messages(4)
        session.autosave()
        session.message_count = 0
        session.resume_session(session.session_name)
        self.assertEqual(session.message_count, 4)

    def test_totals_are_restored(self):
        session = self._session()
        session.context.messages = _messages()
        session.totals["tokens_in"] = 999
        session.autosave()
        session.totals["tokens_in"] = 0
        session.resume_session(session.session_name)
        self.assertEqual(session.totals["tokens_in"], 999)

    def test_show_sessions_mentions_the_current_one(self):
        session = self._session()
        session.context.messages = _messages()
        session.autosave()
        printed: list[str] = []
        with mock.patch.object(session, "_print", side_effect=printed.append):
            session.show_sessions()
        shown = " ".join(str(line) for line in printed)
        self.assertIn("(current)", shown)
        self.assertIn(session.session_name, shown)

    def test_picking_from_a_menu_resumes_it(self):
        session = self._session()
        session.context.messages = _messages()
        session.autosave()
        name = session.session_name
        session.context.messages = [{"role": "system", "content": "x"}]
        with mock.patch("mantra.console._menu", return_value=name):
            self.assertTrue(session.pick_session())
        self.assertEqual(len(session.context.messages), 5)

    def test_cancelling_the_menu_resumes_nothing(self):
        session = self._session()
        session.context.messages = _messages()
        session.autosave()
        before = list(session.context.messages)
        with mock.patch("mantra.console._menu", return_value=""):
            self.assertFalse(session.pick_session())
        self.assertEqual(session.context.messages, before)

    def test_picking_with_no_sessions_says_so(self):
        session = self._session()
        with mock.patch.object(session, "_print") as printed:
            self.assertFalse(session.pick_session())
        self.assertTrue(any("no saved sessions" in str(c) for c in printed.call_args_list))


class DispatchTest(SessionTestBase):
    def _dispatch(self, line):
        session = self._session()
        from mantra.console import dispatch

        return dispatch(session, line)

    def test_resume_with_no_args_opens_the_picker(self):
        session = self._session()
        with mock.patch.object(ConsoleSession, "pick_session") as picked:
            from mantra.console import dispatch

            dispatch(session, "/resume")
        picked.assert_called_once()

    def test_resume_list_shows_them(self):
        session = self._session()
        with mock.patch.object(ConsoleSession, "show_sessions") as shown:
            from mantra.console import dispatch

            dispatch(session, "/resume list")
        shown.assert_called_once()

    def test_resume_by_name_resumes(self):
        session = self._session()
        with mock.patch.object(ConsoleSession, "resume_session") as resumed:
            from mantra.console import dispatch

            dispatch(session, "/resume k-chat-20260101-000000")
        resumed.assert_called_once_with("k-chat-20260101-000000")

    def test_resume_is_in_the_command_table(self):
        from mantra.console import SLASH_COMMANDS

        self.assertIn("/resume", [c for c, _ in SLASH_COMMANDS])

    def test_resume_is_in_the_help_text(self):
        from mantra.console import HELP_TEXT

        self.assertIn("/resume", HELP_TEXT)


if __name__ == "__main__":
    unittest.main()
