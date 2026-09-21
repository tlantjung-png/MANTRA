"""Tests for /mcp: read-only inspection of the configured external tool
servers.

Runtime enable/disable was removed: starting a server rewrites the
operator's config file, so that happens in the file, not from a chat
command mid-session.
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
_tests_dir = os.path.dirname(os.path.abspath(__file__))
if _tests_dir not in sys.path:
    sys.path.insert(0, _tests_dir)

import core.agent.sessions as sessions  # noqa: E402
from core.config import load_config  # noqa: E402
from core.console import Style, _mcp, dispatch  # noqa: E402

from _helpers import messages as _messages  # noqa: E402,F401 - keeps the house import shape


class _IsolatedSessionTest(unittest.TestCase):
    """A fresh session with its own config file per test."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        for var in ("MANTRA_SETTINGS", sessions._OVERRIDE_ENV):
            prior = os.environ.get(var)
            if prior is not None:
                self.addCleanup(os.environ.setdefault, var, prior)
            self.addCleanup(os.environ.pop, var, None)
        os.environ["MANTRA_SETTINGS"] = os.path.join(self.tmp, "settings.json")
        os.environ[sessions._OVERRIDE_ENV] = os.path.join(self.tmp, "sessions")
        self.config_path = os.path.join(self.tmp, "mantra.json")
        with open(self.config_path, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "llm": {"provider": "scripted"},
                    "mcp": {
                        "servers": {
                            "alpha": {"command": ["python", "-c", "print('x')"]},
                            "beta": {"command": ["python", "-c", "print('y')"], "enabled": False},
                        }
                    },
                },
                handle,
            )
        config = load_config(self.config_path)
        # Build with an empty server map: construction would otherwise
        # spawn (and fail on) the test stub commands. The specs are
        # re-attached afterwards, exactly the state after a start where
        # no server came up.
        self.server_specs = config["mcp"]["servers"]
        config["mcp"]["servers"] = {}

        self.session = ConsoleSessionStub.build(self.tmp, config, self.config_path)
        self.session.config["mcp"]["servers"] = self.server_specs

    def tearDown(self):
        for client in getattr(self.session, "mcp_clients", []):
            try:
                client.close()
            except Exception:
                pass


class ConsoleSessionStub:
    """Builds a real ConsoleSession wired to the test's config file."""

    @staticmethod
    def build(workspace: str, config: dict, config_path: str):
        from core.console import ConsoleSession
        from core.scripted import ScriptedLLMClient

        return ConsoleSession(
            config=config,
            workspace=workspace,
            style=Style(enabled=False),
            llm=ScriptedLLMClient([]),
            ask=lambda prompt: "y",
            config_path=config_path,
        )


class StatusTest(_IsolatedSessionTest):

    def test_bare_invocation_lists_configured_servers(self):
        printed: list[str] = []
        with mock.patch.object(self.session, "_print", side_effect=printed.append):
            _mcp(self.session, "")
        joined = "\n".join(str(line) for line in printed)
        self.assertIn("alpha", joined)
        self.assertIn("beta", joined)
        self.assertIn("disabled", joined)

    def test_no_servers_says_so(self):
        self.session.config["mcp"]["servers"] = {}
        printed: list[str] = []
        with mock.patch.object(self.session, "_print", side_effect=printed.append):
            _mcp(self.session, "")
        self.assertTrue(any("no MCP servers configured" in str(line) for line in printed))

    def test_dispatch_routes_slash_mcp(self):
        with mock.patch("core.console._mcp") as handler:
            handled = dispatch(self.session, "/mcp")
        self.assertTrue(handled)
        handler.assert_called_once()

    def test_unknown_subcommand_shows_usage(self):
        printed: list[str] = []
        with mock.patch.object(self.session, "_print", side_effect=printed.append):
            _mcp(self.session, "frobnicate")
        self.assertTrue(any("usage: /mcp" in str(line) for line in printed))

    def test_runtime_enable_and_disable_are_gone(self):
        # Starting a server rewrites the config file; that is the
        # operator's file to edit, not a chat command's to rewrite.
        for sub in ("enable alpha", "disable alpha"):
            printed: list[str] = []
            with mock.patch.object(self.session, "_print", side_effect=printed.append):
                _mcp(self.session, sub)
            self.assertTrue(any("usage: /mcp" in str(line) for line in printed), sub)
            self.assertEqual(self.session.mcp_clients, [])


class ToolsTest(_IsolatedSessionTest):

    def test_tools_for_unconnected_server_says_so(self):
        printed: list[str] = []
        with mock.patch.object(self.session, "_print", side_effect=printed.append):
            _mcp(self.session, "tools alpha")
        self.assertTrue(any("not running" in str(line) for line in printed))


if __name__ == "__main__":
    unittest.main()
