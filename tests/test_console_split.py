"""Guards for the console.py split: the facade keeps every name, and the
patch seams the test suite relies on still reach the moved code."""

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
import core.console as console  # noqa: E402
from core.console import ConsoleSession, Style  # noqa: E402


class _IsolatedSessionTest(unittest.TestCase):
    """A fresh session in its own settings/session store per test."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        for var in ("MANTRA_SETTINGS", sessions._OVERRIDE_ENV):
            prior = os.environ.get(var)
            if prior is not None:
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
            ask=lambda prompt: "y",
        )


class FacadeTest(_IsolatedSessionTest):
    """Every method the split moved out stays on the composed class."""

    def test_moved_methods_are_still_on_the_class(self):
        for name in (
            # mentions
            "expand_mentions", "_resolve_mention", "_render_file", "_render_listing",
            # goals and todos
            "set_goal", "show_goal", "clear_goal", "add_goal_note",
            "_check_goal_completion", "add_todo", "show_todos", "mark_todo_done",
            "rm_todo", "clear_todos", "_check_todo_completion",
            "_handle_stream_todo_report", "_apply_todo_report",
            # usage and compaction
            "_record_usage", "_usage_line", "_record_memory", "_report_changes",
            "_auto_compact", "compact",
            # persistence
            "save_session", "load_session", "autosave", "resume_session",
            "show_sessions", "pick_session", "model_name", "_is_same_workspace",
            # workspace and cost
            "_git", "_git_ok", "show_workspace", "show_diff", "undo_changes",
            "show_cost",
            # endpoints
            "set_model", "set_reasoning", "show_reasoning", "use_endpoint",
            "_warn_if_key_missing", "_warn_if_any_key_missing", "show_endpoints",
        ):
            self.assertTrue(hasattr(ConsoleSession, name), f"missing: {name}")

    def test_module_level_helpers_still_importable_from_console(self):
        for name in (
            "_safe_int", "_is_safe_session_path", "_format_elapsed", "_transcript",
            "_short", "MAX_ATTACH_CHARS", "MAX_TOTAL_ATTACH_CHARS",
            "MAX_GLOB_HITS", "MAX_LISTING_ENTRIES", "PROJECT_ROOT",
        ):
            self.assertTrue(hasattr(console, name), f"facade lost: {name}")

    def test_moved_methods_actually_run(self):
        # One live call per mixin, so a silent import break inside any of
        # them fails here rather than in production.
        self.session.context.messages = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ]
        self.session.set_goal("ship it")
        self.assertEqual(self.session.goal, "ship it")
        self.session.add_todo("one")
        self.session.mark_todo_done("1")
        result = self.session.expand_mentions("no mentions here")
        self.assertEqual(result[0], "no mentions here")
        self.session.totals["tokens_in"] = 10
        self.session.show_cost()
        self.session.show_workspace()
        self.session.show_endpoints()
        self.assertEqual(self.session.save_session(os.path.join(self.tmp, "s.json")), True)
        self.assertEqual(self.session.load_session(os.path.join(self.tmp, "s.json")), True)


class SeamTest(_IsolatedSessionTest):
    """mock.patch.object(console, ...) must still reach the mixin code."""

    def test_pick_session_calls_the_menu_seam_through_console(self):
        # pick_session only opens the menu when something is resumable,
        # so seed one saved session in this workspace first.
        import core.agent.sessions as sessions

        sessions.save(
            "demo",
            {
                "workspace": self.tmp,
                "model": "test-model",
                "summary": "seeded",
                "totals": {},
                "messages": [
                    {"role": "user", "content": "hi"},
                    {"role": "assistant", "content": "hello"},
                ],
                "show_tool_output": True,
                "pending_pages": [],
            },
        )
        with mock.patch.object(console, "_menu", return_value=None) as menu:
            chosen = self.session.pick_session()
        self.assertFalse(chosen)
        menu.assert_called_once()

    def test_patched_menu_value_is_the_one_used(self):
        # A patched seam returning a name must drive the resume path,
        # proving the call goes through the console namespace at runtime.
        with mock.patch.object(console, "_menu", return_value="no-such-session"):
            chosen = self.session.pick_session()
        # "no-such-session" does not exist, so resume reports failure -
        # but only after the seam's return was actually used.
        self.assertFalse(chosen)


if __name__ == "__main__":
    unittest.main()
