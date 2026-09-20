"""Tests for /mcp: inspect, enable, and disable configured servers."""

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


class _FakeClient:
    """Stand-in for a live stdio connection, so disable can be tested
    without spawning a child process."""

    def __init__(self, name: str):
        self.name = name
        self.closed = False

    def close(self) -> None:
        self.closed = True


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


class EnableTest(_IsolatedSessionTest):

    def test_unknown_server_is_refused(self):
        printed: list[str] = []
        with mock.patch.object(self.session, "_print", side_effect=printed.append):
            _mcp(self.session, "enable gamma")
        self.assertTrue(any("no MCP server named 'gamma'" in str(line) for line in printed))

    def test_failing_start_reports_and_stays_enabled(self):
        self.session.config["mcp"]["servers"]["alpha"]["command"] = [
            "definitely-not-a-real-binary-xyz"
        ]
        printed: list[str] = []
        with mock.patch.object(self.session, "_print", side_effect=printed.append):
            _mcp(self.session, "enable alpha")
        joined = "\n".join(str(line) for line in printed)
        self.assertIn("failed to start", joined)
        self.assertIn("stays enabled", joined)
        self.assertEqual(self.session.mcp_clients, [])

    def test_successful_start_registers_tools_and_persists(self):
        # A real child speaks the protocol: the harness's own MCP server
        # answers initialize and tools/list, so enable exercises the
        # full path against the same surface the console exposes.
        self.session.config["mcp"]["servers"]["alpha"]["command"] = [
            sys.executable, "-m", "core.mcp",
            "--config", self.config_path,
            "--workspace", self.tmp,
        ]
        printed: list[str] = []
        with mock.patch.object(self.session, "_print", side_effect=printed.append):
            _mcp(self.session, "enable alpha")
        joined = "\n".join(str(line) for line in printed)
        self.assertIn("connected", joined)
        self.assertEqual(len(self.session.mcp_clients), 1)
        self.assertTrue(
            any(getattr(tool, "server_name", "") == "alpha" for tool in self.session.tools)
        )
        on_disk = json.load(open(self.config_path, encoding="utf-8"))
        self.assertIs(on_disk["mcp"]["servers"]["alpha"]["enabled"], True)
        # Everything else in the file survives the rewrite untouched.
        self.assertEqual(on_disk["llm"]["provider"], "scripted")
        self.assertIn("beta", on_disk["mcp"]["servers"])

    def test_enable_twice_is_a_no_op(self):
        class _Stub:
            name = "alpha"

            def close(self):
                pass

        self.session.mcp_clients.append(_Stub())
        printed: list[str] = []
        with mock.patch.object(self.session, "_print", side_effect=printed.append):
            _mcp(self.session, "enable alpha")
        joined = "\n".join(str(line) for line in printed)
        self.assertIn("already running", joined)
        self.assertEqual(len(self.session.mcp_clients), 1)


class DisableTest(_IsolatedSessionTest):

    def test_disable_stops_client_removes_tools_and_persists(self):
        client = _FakeClient("alpha")
        self.session.mcp_clients.append(client)
        tool = type("T", (), {"name": "mcp__alpha__x", "server_name": "alpha"})()
        keep = type("K", (), {"name": "read_file", "server_name": ""})()
        self.session.tools.extend([tool, keep])
        printed: list[str] = []
        with mock.patch.object(self.session, "_print", side_effect=printed.append):
            _mcp(self.session, "disable alpha")
        self.assertTrue(client.closed)
        self.assertEqual(self.session.mcp_clients, [])
        self.assertNotIn(tool, self.session.tools)
        self.assertIn(keep, self.session.tools)
        self.assertIs(self.session.config["mcp"]["servers"]["alpha"]["enabled"], False)
        on_disk = json.load(open(self.config_path, encoding="utf-8"))
        self.assertIs(on_disk["mcp"]["servers"]["alpha"]["enabled"], False)

    def test_disable_only_touches_the_named_server(self):
        client = _FakeClient("alpha")
        self.session.mcp_clients.append(client)
        with mock.patch.object(self.session, "_print"):
            _mcp(self.session, "disable beta")
        self.assertFalse(client.closed)
        self.assertEqual(len(self.session.mcp_clients), 1)
        on_disk = json.load(open(self.config_path, encoding="utf-8"))
        # alpha never carried an explicit flag; the rewrite must not add one.
        self.assertIs(on_disk["mcp"]["servers"]["alpha"].get("enabled", True), True)

    def test_disable_of_unknown_name_is_refused(self):
        printed: list[str] = []
        with mock.patch.object(self.session, "_print", side_effect=printed.append):
            _mcp(self.session, "disable gamma")
        self.assertTrue(any("no MCP server named 'gamma'" in str(line) for line in printed))

    def test_disable_without_config_path_stays_in_memory(self):
        self.session.config_path = None
        client = _FakeClient("alpha")
        self.session.mcp_clients.append(client)
        printed: list[str] = []
        with mock.patch.object(self.session, "_print", side_effect=printed.append):
            _mcp(self.session, "disable alpha")
        self.assertTrue(client.closed)
        # The in-memory spec still flips even when nothing was saved.
        self.assertIs(self.session.config["mcp"]["servers"]["alpha"]["enabled"], False)


class ToolsTest(_IsolatedSessionTest):

    def test_tools_for_unconnected_server_says_so(self):
        printed: list[str] = []
        with mock.patch.object(self.session, "_print", side_effect=printed.append):
            _mcp(self.session, "tools alpha")
        self.assertTrue(any("not running" in str(line) for line in printed))


if __name__ == "__main__":
    unittest.main()
