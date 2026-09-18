"""Tests for /suggestions on|off - the runtime toggle for the post-task
suggestion row.

The command must flip the live TUI flag, persist the choice in the
session config, dismiss a visible row when turning off, and report state
on a bare invocation. All duck-typed: works with and without a ui.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "."))
_tests_dir = os.path.dirname(os.path.abspath(__file__))
if _tests_dir not in sys.path:
    sys.path.insert(0, _tests_dir)

import core.agent.sessions as sessions
from core.console import ConsoleSession, Style, _set_suggestions, dispatch

from _helpers import messages as _messages
from tui_harness import FakeBackend as _TuiFakeBackend


class _FakeBackend(_TuiFakeBackend):
    """The TUI suite's fake backend, reused for app construction."""


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


class _FakeUi:
    """Duck-typed stand-in for the TuiApp suggestion surface."""

    def __init__(self):
        self.suggestions_enabled = True
        self.dismissed = False

    def _dismiss_suggestions(self):
        self.dismissed = True


class SuggestionsToggleTest(_IsolatedSessionTest):

    def test_off_flips_the_live_flag_and_persists(self):
        ui = _FakeUi()
        self.session.ui = ui
        _set_suggestions(self.session, "off")
        self.assertFalse(ui.suggestions_enabled, "live flag not flipped")
        self.assertTrue(ui.dismissed, "visible row not dismissed on off")
        self.assertIs(self.session.config["suggestions"], False, "choice not persisted")

    def test_on_flips_back_on(self):
        self.session.ui = _FakeUi()
        _set_suggestions(self.session, "off")
        _set_suggestions(self.session, "on")
        self.assertTrue(self.session.ui.suggestions_enabled)
        self.assertIs(self.session.config["suggestions"], True)

    def test_bare_invocation_reports_state(self):
        printed: list[str] = []
        self.session._print = lambda text="": printed.append(text)
        self.session.ui = _FakeUi()
        _set_suggestions(self.session, "")
        self.assertTrue(any("on" in p and "usage" in p for p in printed), printed)

    def test_bare_invocation_without_ui_reads_config(self):
        printed: list[str] = []
        self.session._print = lambda text="": printed.append(text)
        self.session.ui = None
        _set_suggestions(self.session, "")
        self.assertTrue(any("on" in p for p in printed), printed)
        _set_suggestions(self.session, "off")
        printed.clear()
        _set_suggestions(self.session, "")
        self.assertTrue(any("off" in p for p in printed), printed)

    def test_off_without_ui_still_persists(self):
        self.session.ui = None
        _set_suggestions(self.session, "off")
        self.assertIs(self.session.config["suggestions"], False)

    def test_dispatch_routes_the_command(self):
        # The full dispatcher must claim "/suggestions off" (return True)
        # and leave an unknown near-miss to the agent path.
        self.session.ui = _FakeUi()
        self.assertTrue(dispatch(self.session, "/suggestions off"))
        self.assertFalse(self.session.ui.suggestions_enabled)
        self.assertTrue(dispatch(self.session, "/suggestions on"))
        self.assertTrue(self.session.ui.suggestions_enabled)

    def test_turn_end_respects_the_runtime_flag(self):
        # Wiring check: the app consults suggestions_enabled (set by the
        # command) at turn end, so off suppresses the row entirely.
        ui = _FakeUi()
        self.session.ui = ui
        _set_suggestions(self.session, "off")
        self.assertFalse(ui.suggestions_enabled)
        # Simulate the app's gate (mirrors _maybe_show_suggestions).
        row = ["run the tests"] if ui.suggestions_enabled else []
        self.assertEqual(row, [])

    def test_toggling_shows_a_two_second_toast(self):
        # The flip rides the live TUI as a 2-second toast; the plain
        # REPL (no ui) falls back to a printed line.
        class _ToastUi(_FakeUi):
            def __init__(self):
                super().__init__()
                self.toasts: list[tuple[str, float]] = []

            def toast_message(self, text, seconds=1.6):
                self.toasts.append((text, seconds))

        ui = _ToastUi()
        self.session.ui = ui
        _set_suggestions(self.session, "off")
        self.assertEqual(ui.toasts, [("suggestions off", 2.0)])
        _set_suggestions(self.session, "on")
        self.assertEqual(ui.toasts[-1], ("suggestions on", 2.0))

    def test_choice_is_persisted_to_the_settings_file(self):
        from core.agent.settings import settings_path, ui_prefs

        _set_suggestions(self.session, "off")
        file = settings_path()
        self.assertTrue(file.is_file(), "settings file not written")
        self.assertIs(ui_prefs()["suggestions"], False, "choice not persisted on disk")
        # And flipping back rewrites it.
        _set_suggestions(self.session, "on")
        self.assertIs(ui_prefs()["suggestions"], True)

    def test_persisted_choice_survives_a_restart(self):
        from core.tui.app import TuiApp

        self.session.ui = None
        _set_suggestions(self.session, "off")
        # A brand-new app ("restart") with a session whose config has no
        # override must read the persisted preference from disk.
        from core.config import merge_defaults

        fresh_config = merge_defaults({})
        fresh_config.pop("suggestions", None)
        fresh = ConsoleSession(
            config=fresh_config,
            workspace=self.tmp,
            style=Style(enabled=False),
            llm=self.session.llm,
            ask=lambda prompt: "y",
        )
        app = TuiApp(fresh, backend=_FakeBackend())
        app._init_surface()
        self.assertFalse(app.suggestions_enabled, "persisted off lost across restart")

    def test_session_config_overrides_the_settings_file(self):
        from core.tui.app import TuiApp

        from core.config import merge_defaults

        # Disk says off; an explicit run-config "suggestions": true wins.
        _set_suggestions(self.session, "off")
        cfg = merge_defaults({})
        cfg["suggestions"] = True
        override = ConsoleSession(
            config=cfg,
            workspace=self.tmp,
            style=Style(enabled=False),
            llm=self.session.llm,
            ask=lambda prompt: "y",
        )
        app = TuiApp(override, backend=_FakeBackend())
        app._init_surface()
        self.assertTrue(app.suggestions_enabled, "session config override ignored")


if __name__ == "__main__":
    unittest.main()
