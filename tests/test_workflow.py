"""Tests for /workflow - saved sequences of steps.

``MANTRA_WORKFLOWS`` points the store at a temp file, so nothing here
can touch the operator's real workflows.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "."))

import core.agent.workflows as workflows
from core.console import ConsoleSession, Style, _workflow


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.addCleanup(os.environ.pop, workflows._OVERRIDE_ENV, None)
        os.environ[workflows._OVERRIDE_ENV] = os.path.join(self.tmp, "workflows.json")

    def test_a_workflow_round_trips(self):
        workflows.create("ship", ["read the diff", "run the tests"])
        self.assertEqual(workflows.get("ship")["steps"], ["read the diff", "run the tests"])

    def test_a_missing_workflow_is_none(self):
        self.assertIsNone(workflows.get("nope"))

    def test_names_are_slugged_so_spaces_are_not_a_problem(self):
        workflows.create("My Flow", ["a"])
        self.assertIsNotNone(workflows.get("my flow"))
        self.assertIsNotNone(workflows.get("My-Flow"))

    def test_creating_again_replaces_rather_than_duplicating(self):
        workflows.create("ship", ["one"])
        workflows.create("ship", ["two", "three"])
        self.assertEqual(len(workflows.list_workflows()), 1)
        self.assertEqual(workflows.get("ship")["steps"], ["two", "three"])

    def test_blank_steps_are_dropped(self):
        workflows.create("w", ["  ", "real", ""])
        self.assertEqual(workflows.get("w")["steps"], ["real"])

    def test_an_empty_workflow_is_refused(self):
        ok, message = workflows.create("w", [])
        self.assertFalse(ok)
        self.assertIn("at least one step", message)

    def test_a_nameless_workflow_is_refused(self):
        ok, _ = workflows.create("!!!", ["a"])
        self.assertFalse(ok)

    def test_too_many_steps_is_refused(self):
        ok, message = workflows.create("w", ["x"] * (workflows._MAX_STEPS + 1))
        self.assertFalse(ok)
        self.assertIn("too many", message)

    def test_the_listing_is_alphabetical(self):
        workflows.create("zebra", ["a"])
        workflows.create("apple", ["a"])
        self.assertEqual([w["name"] for w in workflows.list_workflows()], ["apple", "zebra"])

    def test_delete_removes_it(self):
        workflows.create("gone", ["a"])
        self.assertTrue(workflows.delete("gone"))
        self.assertIsNone(workflows.get("gone"))

    def test_deleting_a_missing_one_is_false(self):
        self.assertFalse(workflows.delete("nope"))

    def test_a_corrupt_file_reads_as_empty_instead_of_raising(self):
        with open(os.environ[workflows._OVERRIDE_ENV], "w", encoding="utf-8") as fh:
            fh.write("{ this is not json")
        self.assertEqual(workflows.list_workflows(), [])

    def test_a_file_of_the_wrong_shape_reads_as_empty(self):
        with open(os.environ[workflows._OVERRIDE_ENV], "w", encoding="utf-8") as fh:
            json.dump(["a", "list"], fh)
        self.assertEqual(workflows.list_workflows(), [])

    def test_entries_without_steps_are_not_listed(self):
        with open(os.environ[workflows._OVERRIDE_ENV], "w", encoding="utf-8") as fh:
            json.dump({"version": 1, "workflows": {"bad": {"name": "bad"}}}, fh)
        self.assertEqual(workflows.list_workflows(), [])

    def test_the_file_is_hand_editable_json(self):
        workflows.create("w", ["a"])
        with open(os.environ[workflows._OVERRIDE_ENV], encoding="utf-8") as fh:
            raw = json.load(fh)
        self.assertEqual(raw["workflows"]["w"]["steps"], ["a"])

    def test_a_hand_added_workflow_is_picked_up(self):
        with open(os.environ[workflows._OVERRIDE_ENV], "w", encoding="utf-8") as fh:
            json.dump(
                {"version": 1, "workflows": {"manual": {"name": "manual", "steps": ["x"]}}},
                fh,
            )
        self.assertEqual(workflows.get("manual")["steps"], ["x"])


class CommandTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        for var in ("MANTRA_SETTINGS", workflows._OVERRIDE_ENV):
            prior = os.environ.get(var)
            if prior is not None:
                # addCleanup is LIFO: this runs after the pop below,
                # restoring whatever the harness had set.
                self.addCleanup(os.environ.setdefault, var, prior)
            self.addCleanup(os.environ.pop, var, None)
        os.environ["MANTRA_SETTINGS"] = os.path.join(self.tmp, "config.json")
        os.environ[workflows._OVERRIDE_ENV] = os.path.join(self.tmp, "workflows.json")

        from core.config import merge_defaults
        from core.scripted import ScriptedLLMClient

        self.session = ConsoleSession(
            config=merge_defaults({}),
            workspace=self.tmp,
            style=Style(enabled=False),
            llm=ScriptedLLMClient([]),
            ask=lambda prompt: "y",
        )
        self.printed: list[str] = []
        self.session._print = self.printed.append

    def _shown(self) -> str:
        return " ".join(str(line) for line in self.printed)

    # ---- show ---------------------------------------------------------

    def test_show_with_nothing_saved_says_so(self):
        _workflow(self.session, "show")
        self.assertIn("no workflows yet", self._shown())

    def test_show_lists_workflows_with_their_step_counts(self):
        workflows.create("ship", ["one", "two"])
        _workflow(self.session, "show")
        self.assertIn("ship", self._shown())
        self.assertIn("2 steps", self._shown())

    def test_a_one_step_workflow_is_singular(self):
        workflows.create("w", ["one"])
        _workflow(self.session, "show")
        self.assertIn("1 step", self._shown())
        self.assertNotIn("1 steps", self._shown())

    def test_show_by_name_lists_the_steps(self):
        workflows.create("ship", ["read the diff", "run the tests"])
        _workflow(self.session, "show ship")
        shown = self._shown()
        self.assertIn("read the diff", shown)
        self.assertIn("run the tests", shown)

    def test_show_by_unknown_name_says_so(self):
        _workflow(self.session, "show nope")
        self.assertIn("no workflow named", self._shown())

    def test_bare_workflow_lists(self):
        workflows.create("w", ["a"])
        _workflow(self.session, "")
        self.assertIn("w", self._shown())

    # ---- create -------------------------------------------------------

    def test_create_reads_steps_until_the_dot(self):
        with mock.patch("core.console._read_multiline", return_value="first\nsecond"):
            _workflow(self.session, "create ship")
        self.assertEqual(workflows.get("ship")["steps"], ["first", "second"])

    def test_create_reports_what_it_made(self):
        with mock.patch("core.console._read_multiline", return_value="only"):
            _workflow(self.session, "create ship")
        self.assertIn("created 'ship'", self._shown())
        self.assertIn("1 step", self._shown())

    def test_creating_again_says_updated(self):
        workflows.create("ship", ["a"])
        self.printed.clear()
        with mock.patch("core.console._read_multiline", return_value="b"):
            _workflow(self.session, "create ship")
        self.assertIn("updated", self._shown())

    def test_create_without_a_name_is_a_usage_message(self):
        _workflow(self.session, "create")
        self.assertIn("usage", self._shown())

    def test_create_with_no_steps_is_refused(self):
        with mock.patch("core.console._read_multiline", return_value=""):
            _workflow(self.session, "create ship")
        self.assertIsNone(workflows.get("ship"))

    # ---- launch -------------------------------------------------------

    def test_launch_runs_every_step_in_order(self):
        workflows.create("ship", ["one", "two", "three"])
        seen: list[str] = []
        # Returns a truthy object so every step counts as completed; a
        # plain None return would read as "step did not complete".
        with mock.patch.object(
            self.session, "handle", side_effect=lambda text: seen.append(text) or object()
        ):
            _workflow(self.session, "launch ship")
        self.assertEqual(seen, ["one", "two", "three"])

    def test_launch_says_what_it_is_running(self):
        workflows.create("ship", ["one"])
        with mock.patch.object(self.session, "handle", return_value=object()):
            _workflow(self.session, "launch ship")
        self.assertIn("launching 'ship'", self._shown())

    def test_launch_announces_each_step(self):
        workflows.create("ship", ["one", "two"])
        with mock.patch.object(self.session, "handle", return_value=object()):
            _workflow(self.session, "launch ship")
        self.assertIn("step 1 of 2", self._shown())
        self.assertIn("step 2 of 2", self._shown())

    def test_launch_stops_when_a_step_does_not_complete(self):
        workflows.create("ship", ["one", "two"])
        with mock.patch.object(self.session, "handle", return_value=None) as handled:
            _workflow(self.session, "launch ship")
        self.assertEqual(handled.call_count, 1)
        self.assertIn("stopped", self._shown())

    def test_ctrl_c_stops_the_workflow(self):
        workflows.create("ship", ["one", "two"])
        with mock.patch.object(self.session, "handle", side_effect=KeyboardInterrupt):
            _workflow(self.session, "launch ship")
        self.assertIn("workflow stopped", self._shown())

    def test_launch_without_a_name_is_a_usage_message(self):
        _workflow(self.session, "launch")
        self.assertIn("usage", self._shown())

    def test_launch_an_unknown_workflow_says_so(self):
        _workflow(self.session, "launch nope")
        self.assertIn("no workflow named", self._shown())

    def test_run_is_an_alias_for_launch(self):
        workflows.create("w", ["a"])
        with mock.patch.object(self.session, "handle", return_value=object()) as handled:
            _workflow(self.session, "run w")
        handled.assert_called_once()

    # ---- remove -------------------------------------------------------

    def test_remove_deletes_it(self):
        workflows.create("gone", ["a"])
        _workflow(self.session, "remove gone")
        self.assertIsNone(workflows.get("gone"))

    def test_remove_an_unknown_one_says_so(self):
        _workflow(self.session, "remove nope")
        self.assertIn("no workflow named", self._shown())

    # ---- registration -------------------------------------------------

    def test_workflow_is_in_the_command_table(self):
        from core.console import SLASH_COMMANDS

        self.assertIn("/workflow", [c for c, _ in SLASH_COMMANDS])

    def test_workflow_is_in_the_help_text(self):
        from core.console import HELP_TEXT

        self.assertIn("/workflow", HELP_TEXT)

    def test_dispatch_routes_to_the_workflow_handler(self):
        from core.console import dispatch

        with mock.patch("core.console._workflow") as handled:
            dispatch(self.session, "/workflow show")
        handled.assert_called_once_with(self.session, "show")


if __name__ == "__main__":
    unittest.main()
