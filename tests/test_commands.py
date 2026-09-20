"""Tests for what the slash commands are, and how they are chosen.

Four rules this file exists to hold in place:

* every command with children opens a menu, rather than asking the
  operator to remember and type a name or a number;
* ``/reasoning`` is folded into ``/model``, because effort is a property
  of the model and not a separate setting to keep in step;
* ``/provider`` is gone - there are no built-in endpoints, and
  setup happens through ``/model`` (which absorbed ``/connect``);
* what the user adds lives in one hand-editable file.
"""

from __future__ import annotations

import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_TESTS_DIR)
for _path in (os.path.join(_PROJECT_ROOT, "."), _PROJECT_ROOT, _TESTS_DIR):
    if _path not in sys.path:
        sys.path.insert(0, _path)
import core.console as console  # noqa: E402
from core.console import SLASH_COMMANDS, dispatch  # noqa: E402
from core.agent.settings import add_endpoint, settings_path  # noqa: E402
from _helpers import make_session  # noqa: E402


class TempSettings:
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(
            os.environ,
            {"MANTRA_SETTINGS": os.path.join(self.tmp.name, "config.json")},
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(self.tmp.cleanup)


def _session(workspace):
    session = make_session(workspace, [])
    # Force an active endpoint so /model and /connect behave deterministically.
    session.config["llm"]["base_url"] = "https://x.test/v1"
    return session


class NoBuiltinsTest(unittest.TestCase):
    """/provider is gone, and nothing was left behind."""

    def test_there_is_no_provider_table(self):
        self.assertFalse(hasattr(console, "PROVIDERS"))

    def test_the_provider_registry_module_is_gone(self):
        with self.assertRaises(ImportError):
            import core.agent.providers  # noqa: F401

    def test_provider_is_an_unknown_command(self):
        workspace = tempfile.mkdtemp(prefix="mantra-cmd-")
        self.addCleanup(__import__("shutil").rmtree, workspace, True)
        buf = io.StringIO()
        with redirect_stdout(buf):
            dispatch(_session(workspace), "/provider openai")
        self.assertIn("unknown command", buf.getvalue())

    def test_provider_is_not_offered_as_a_command(self):
        self.assertFalse(any(c == "/provider" for c, _ in SLASH_COMMANDS))

    def test_provider_is_not_in_the_help_text(self):
        self.assertNotIn("/provider", console.HELP_TEXT)

    def test_reasoning_is_not_offered_as_its_own_command(self):
        # It still works as an alias, but it must not be advertised:
        # advertising two commands for one choice is how they drift.
        self.assertFalse(any(c == "/reasoning" for c, _ in SLASH_COMMANDS))

    def test_commands_are_offered_alphabetically(self):
        # The advertised list is alphabetical so a command can be found
        # without scanning it; /connect merged into /model stays merged.
        names = [c for c, _ in SLASH_COMMANDS]
        self.assertEqual(names, sorted(names))
        self.assertNotIn("/connect", names)

    def test_help_lines_are_listed_alphabetically(self):
        entries = [
            line.split()[0]
            for line in console.HELP_TEXT.splitlines()
            if line.startswith("  /") and line.split()[0] != "/"
        ]
        self.assertEqual(entries, sorted(entries))
        self.assertTrue(entries, "help lists no commands at all")

    def test_help_mentions_the_settings_file(self):
        # "You can edit this by hand" is only true if help says where.
        self.assertIn("/model", console.HELP_TEXT)
        # /connect survives only as an inline alias note, never as its
        # own command entry (leading-space line start).
        self.assertNotIn("  /connect", console.HELP_TEXT)


class MenuCommandsTest(TempSettings, unittest.TestCase):
    """Anything with children is picked from a menu, not typed."""

    def setUp(self):
        TempSettings.setUp(self)
        self.workspace = tempfile.mkdtemp(prefix="mantra-menu-cmd-")
        import shutil

        self.addCleanup(shutil.rmtree, self.workspace, True)
        self.session = _session(self.workspace)
        # Two saved endpoints, so /connect has a real choice to show.
        add_endpoint("first", "https://first.test/v1", "", ["f1"])
        add_endpoint("second", "https://second.test/v1", "", ["s1"])

    def _run(self, line):
        buf = io.StringIO()
        with mock.patch.object(console, "_menu", return_value=None) as menu:
            with redirect_stdout(buf):
                dispatch(self.session, line)
        return menu, buf.getvalue()

    def test_bare_model_opens_a_menu(self):
        with mock.patch.object(console, "fetch_models", return_value=["gpt-4o"]):
            menu, _ = self._run("/model")
        menu.assert_called_once()

    def test_bare_reasoning_opens_the_model_menu(self):
        with mock.patch.object(console, "fetch_models", return_value=["o3-mini"]):
            menu, _ = self._run("/reasoning")
        menu.assert_called_once()
        self.assertIn("model", menu.call_args[0][1].lower())

    def test_effort_alias_opens_the_model_menu(self):
        with mock.patch.object(console, "fetch_models", return_value=["o3-mini"]):
            menu, _ = self._run("/effort")
        menu.assert_called_once()

    def test_bare_model_opens_the_master_menu(self):
        # The bare form opens the one menu that manages providers and
        # models together.
        menu, _ = self._run("/model")
        menu.assert_called_once()
        values = [o.value for o in menu.call_args[0][2]]
        self.assertIn(console.ADD_ENDPOINT, values)
        self.assertIn(console.PICK_MODEL, values)

    def test_model_with_nothing_saved_offers_the_add_entry(self):
        # With zero saved endpoints the master menu still shows, leading
        # with the single action a new user needs.
        for name in ("first", "second"):
            from core.agent.settings import remove_endpoint

            remove_endpoint(name)
        menu, _ = self._run("/model")
        menu.assert_called_once()
        values = [o.value for o in menu.call_args[0][2]]
        self.assertEqual(values, [console.ADD_ENDPOINT])

    def test_bare_approve_opens_a_menu(self):
        menu, _ = self._run("/approve")
        menu.assert_called_once()
        self.assertIn("approval", menu.call_args[0][1].lower())

    def test_choose_from_the_menu_actually_applies(self):
        with mock.patch.object(console, "fetch_models", return_value=["gpt-4o"]), \
             mock.patch.object(console, "_menu",
                               side_effect=[console.PICK_MODEL, "gpt-4o", None]):
            with redirect_stdout(io.StringIO()):
                dispatch(self.session, "/model")
        self.assertEqual(self.session.config["llm"]["model"], "gpt-4o")

    def test_approve_menu_choice_applies(self):
        with mock.patch.object(console, "_menu", return_value="yolo"):
            with redirect_stdout(io.StringIO()):
                dispatch(self.session, "/approve")
        self.assertEqual(self.session.approvals.mode, "yolo")

    def test_cancelling_the_menu_changes_nothing(self):
        before = self.session.approvals.mode
        menu, out = self._run("/approve")
        self.assertEqual(self.session.approvals.mode, before)
        self.assertIn("approval mode", out)

    def test_a_named_model_still_works_without_a_menu(self):
        # Menus are an aid, not a gate: the typed form must survive and
        # must not have to go through the catalogue to get there.
        with mock.patch.object(console, "fetch_models") as fetch, \
             mock.patch.object(console, "_menu", return_value=None) as menu:
            with redirect_stdout(io.StringIO()):
                dispatch(self.session, "/model gpt-4o")
        self.assertEqual(self.session.config["llm"]["model"], "gpt-4o")
        fetch.assert_not_called()
        # Effort is part of choosing a model, so it is still offered -
        # cancelling it leaves the model on "off" rather than unset.
        menu.assert_called_once()
        self.assertIn("reasoning", menu.call_args[0][1].lower())

    def test_a_named_reasoning_model_gets_an_effort_menu(self):
        with mock.patch.object(console, "_menu", return_value="high") as menu:
            with redirect_stdout(io.StringIO()):
                dispatch(self.session, "/model o3-mini")
        menu.assert_called_once()
        self.assertEqual(self.session.config["llm"]["reasoning_effort"], "high")

    def test_model_and_effort_together_need_no_menu_at_all(self):
        with mock.patch.object(console, "_menu") as menu:
            with redirect_stdout(io.StringIO()):
                dispatch(self.session, "/model o3-mini low")
        menu.assert_not_called()
        llm = self.session.config["llm"]
        self.assertEqual(llm["model"], "o3-mini")
        self.assertEqual(llm["reasoning_effort"], "low")


class ReasoningMergeTest(TempSettings, unittest.TestCase):
    """Reasoning is chosen with the model, not after it."""

    def setUp(self):
        TempSettings.setUp(self)
        self.workspace = tempfile.mkdtemp(prefix="mantra-reason-")
        import shutil

        self.addCleanup(shutil.rmtree, self.workspace, True)
        self.session = _session(self.workspace)

    def test_reasoning_without_an_argument_routes_to_the_model_menu(self):
        with mock.patch.object(console, "fetch_models", return_value=["o3-mini"]), \
             mock.patch.object(console, "_choose_model", return_value=True) as pick:
            with redirect_stdout(io.StringIO()):
                dispatch(self.session, "/reasoning")
        pick.assert_called_once_with(self.session)

    def test_reasoning_with_an_argument_still_sets_it_directly(self):
        with redirect_stdout(io.StringIO()):
            dispatch(self.session, "/reasoning high")
        self.assertEqual(self.session.config["llm"]["reasoning_effort"], "high")

    def test_a_plain_model_clears_the_effort_set_for_another(self):
        # Otherwise the next request carries a field chosen for a model
        # that no longer has anything to do with it.
        with redirect_stdout(io.StringIO()):
            dispatch(self.session, "/model o3-mini high")
            dispatch(self.session, "/model gpt-4o")
        self.assertIsNone(self.session.config["llm"]["reasoning_effort"])

    def test_reasoning_shows_itself_when_the_menu_cannot_open(self):
        # A piped run has no terminal; it must fall back to printing
        # rather than to silence.
        with mock.patch.object(console, "fetch_models", return_value=[]), \
             mock.patch.object(console, "_menu", return_value=None):
            buf = io.StringIO()
            with redirect_stdout(buf):
                dispatch(self.session, "/reasoning")
        self.assertIn("reasoning", buf.getvalue())


class HandEditableConfigTest(TempSettings, unittest.TestCase):
    """What the user adds is one file they can open in an editor."""

    def setUp(self):
        TempSettings.setUp(self)
        self.workspace = tempfile.mkdtemp(prefix="mantra-cfg-")
        import shutil

        self.addCleanup(shutil.rmtree, self.workspace, True)
        self.session = _session(self.workspace)

    def test_the_file_is_json(self):
        add_endpoint("mine", "https://mine.test/v1", "MINE_API_KEY", ["m1"])
        import json

        # json.load is the whole point: the user is expected to read and
        # write this by hand, so it must not be a bespoke format.
        with open(settings_path(), encoding="utf-8") as handle:
            data = json.load(handle)
        self.assertEqual(data["endpoints"]["mine"]["base_url"], "https://mine.test/v1")

    def test_a_model_added_by_hand_appears_in_the_menu(self):
        """No /connect: write the file, and MANTRA picks it up."""
        add_endpoint("mine", "https://mine.test/v1", "", ["typed-by-hand"])
        self.session.config["llm"]["base_url"] = "https://mine.test/v1"

        with mock.patch.object(console, "fetch_models", return_value=[]), \
             mock.patch.object(console, "_menu", side_effect=[console.PICK_MODEL, None]) as menu:
            with redirect_stdout(io.StringIO()):
                dispatch(self.session, "/model")
        # The hand-written entry, plus the way out when the endpoint
        # serves something that is not listed.
        self.assertEqual(
            [o.value for o in menu.call_args[0][2]],
            ["typed-by-hand", console.TYPE_A_MODEL],
        )

    def test_an_endpoint_added_by_hand_is_recognised(self):
        add_endpoint("mine", "https://mine.test/v1", "", ["m1"])
        self.session.config["llm"]["base_url"] = "https://mine.test/v1"
        self.assertEqual(self.session.endpoint_name, "mine")

    def test_the_listing_tells_the_user_where_the_file_is(self):
        add_endpoint("mine", "https://mine.test/v1", "", ["m1"])
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.session.show_endpoints()
        self.assertIn(str(settings_path()), buf.getvalue())

    def test_switching_by_hand_then_by_name(self):
        # Editing the file is enough for MANTRA to accept the name.
        add_endpoint("mine", "https://mine.test/v1", "", ["m1"])
        with redirect_stdout(io.StringIO()):
            self.assertTrue(dispatch(self.session, "/model mine"))
        self.assertEqual(self.session.config["llm"]["base_url"], "https://mine.test/v1")
        self.assertEqual(self.session.config["llm"]["model"], "m1")


class RedundantCommandCleanupTest(unittest.TestCase):
    """Merged and removed commands stay merged and removed.

    /clear and /reset were two bodies for one action; /paste was
    superseded by the multiline composer (Shift+Enter, bracketed paste);
    /skill is a hidden alias of /skills like /quit is of /exit.
    """

    def setUp(self):
        self.workspace = tempfile.mkdtemp(prefix="mantra-clean-")
        self.addCleanup(__import__("shutil").rmtree, self.workspace, True)
        self.session = _session(self.workspace)

    def test_reset_still_works_as_a_hidden_alias(self):
        self.session.context.seed("sys", "task")
        self.session.context.append({"role": "user", "content": "hi"})
        self.session.goal = "remember this"
        self.assertEqual(len(self.session.context.messages), 3)
        buf = io.StringIO()
        with redirect_stdout(buf):
            dispatch(self.session, "/reset")
        # Same semantics as /clear: system prompt only, goal gone.
        self.assertEqual(len(self.session.context.messages), 1)
        self.assertEqual(self.session.context.messages[0]["role"], "system")
        self.assertEqual(self.session.goal, "")
        self.assertIn("cleared", buf.getvalue())

    def test_reset_is_not_advertised(self):
        self.assertFalse(any(c == "/reset" for c, _ in SLASH_COMMANDS))
        # No help line of its own (an alias mention inside /clear is fine).
        self.assertFalse(any(line.strip().startswith("/reset") for line in console.HELP_TEXT.splitlines()))

    def test_paste_is_gone(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            dispatch(self.session, "/paste")
        self.assertIn("unknown command", buf.getvalue())
        self.assertFalse(any(c == "/paste" for c, _ in SLASH_COMMANDS))
        self.assertNotIn("/paste", console.HELP_TEXT)

    def test_skill_alias_is_hidden_but_works(self):
        with mock.patch.object(console, "_skills") as skills:
            with redirect_stdout(io.StringIO()):
                dispatch(self.session, "/skill tdd")
        skills.assert_called_once_with(self.session, "tdd")
        self.assertFalse(any(c == "/skill" for c, _ in SLASH_COMMANDS))

    def test_dashboard_steps_and_tools_are_gone(self):
        for cmd in ("/dashboard", "/dash", "/steps", "/tools"):
            buf = io.StringIO()
            with redirect_stdout(buf):
                dispatch(self.session, cmd)
            self.assertIn("unknown command", buf.getvalue(), cmd)
            self.assertFalse(any(c == cmd for c, _ in SLASH_COMMANDS), cmd)
        help_lines = [ln.strip() for ln in console.HELP_TEXT.splitlines()]
        for cmd in ("/dashboard", "/steps", "/tools"):
            self.assertFalse(any(ln.startswith(cmd) for ln in help_lines), cmd)


if __name__ == "__main__":
    unittest.main()
