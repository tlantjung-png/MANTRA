"""Tests for the MCP support: the exposing side, the consuming side, and
the config section that wires the second into the first.

The end-to-end test spawns a real ``python -m core.mcp`` server as a
child process and drives it through ``MCPClient``, so both directions are
exercised over the same stdio protocol a third-party client would use.
No network access is involved.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile
import unittest

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_TESTS_DIR)
for _path in (os.path.join(_PROJECT_ROOT, "."), _PROJECT_ROOT, _TESTS_DIR):
    if _path not in sys.path:
        sys.path.insert(0, _path)


class _EchoTool:
    """A stand-in tool: returns its arguments, or raises on request."""

    name = "echo_tool"
    description = "Echo the text back."
    parameters = {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    }

    def __init__(self):
        self.calls = []

    def schema(self):
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def execute(self, sandbox, **kwargs):
        self.calls.append(kwargs)
        if kwargs.get("text") == "boom":
            raise RuntimeError("tool exploded")
        return f"echo: {kwargs.get('text')}"


class _Sandbox:
    """The server only passes this through, so a bare object is enough."""


class _Deny:
    def check(self, tool, arguments):
        return False


class ProtocolTest(unittest.TestCase):
    def test_messages_round_trip_on_one_line(self):
        from core.mcp import protocol

        line = protocol.dumps(protocol.request(1, "tools/call", {"name": "x"}))
        self.assertNotIn("\n", line)
        self.assertEqual(protocol.loads(line)["method"], "tools/call")

    def test_newlines_inside_a_string_are_escaped(self):
        from core.mcp import protocol

        line = protocol.dumps({"method": "x", "params": {"text": "a\nb"}})
        self.assertEqual(len(line.splitlines()), 1)
        self.assertEqual(protocol.loads(line)["params"]["text"], "a\nb")

    def test_a_non_object_message_is_rejected(self):
        from core.mcp import protocol

        with self.assertRaises(protocol.ProtocolError):
            protocol.loads("[1, 2]")
        with self.assertRaises(protocol.ProtocolError):
            protocol.loads("{not json")

    def test_version_negotiation_prefers_the_client_revision(self):
        from core.mcp import protocol

        self.assertEqual(protocol.negotiate_version("2024-11-05"), "2024-11-05")
        self.assertEqual(protocol.negotiate_version("1999-01-01"), protocol.PROTOCOL_VERSION)
        self.assertEqual(protocol.negotiate_version(None), protocol.PROTOCOL_VERSION)


class ServerTest(unittest.TestCase):
    def _server(self, approvals=None):
        from core.mcp.server import MCPServer

        self.tool = _EchoTool()
        return MCPServer(tools=[self.tool], sandbox=_Sandbox(), approvals=approvals)

    def test_initialize_reports_capabilities_and_server_info(self):
        server = self._server()
        response = server.handle(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}}
        )
        self.assertEqual(response["id"], 1)
        self.assertEqual(response["result"]["protocolVersion"], "2025-06-18")
        self.assertIn("tools", response["result"]["capabilities"])
        self.assertEqual(response["result"]["serverInfo"]["name"], server.name)

    def test_tools_list_describes_each_tool(self):
        server = self._server()
        response = server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        tools = response["result"]["tools"]
        self.assertEqual([t["name"] for t in tools], ["echo_tool"])
        self.assertEqual(tools[0]["inputSchema"]["required"], ["text"])

    def test_tools_call_returns_the_observation(self):
        server = self._server()
        response = server.handle(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "echo_tool", "arguments": {"text": "hi"}},
            }
        )
        self.assertEqual(response["result"]["content"][0]["text"], "echo: hi")
        self.assertNotIn("isError", response["result"])

    def test_a_raising_tool_comes_back_as_an_error_result(self):
        server = self._server()
        response = server.handle(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {"name": "echo_tool", "arguments": {"text": "boom"}},
            }
        )
        self.assertTrue(response["result"]["isError"])
        self.assertIn("failed", response["result"]["content"][0]["text"])

    def test_the_approval_policy_is_enforced(self):
        server = self._server(approvals=_Deny())
        response = server.handle(
            {
                "jsonrpc": "2.0",
                "id": 5,
                "method": "tools/call",
                "params": {"name": "echo_tool", "arguments": {"text": "hi"}},
            }
        )
        self.assertTrue(response["result"]["isError"])
        self.assertEqual(self.tool.calls, [], "a denied tool must not run")

    def test_unknown_tool_and_method_are_protocol_errors(self):
        from core.mcp import protocol

        server = self._server()
        unknown_tool = server.handle(
            {"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {"name": "nope"}}
        )
        self.assertEqual(unknown_tool["error"]["code"], protocol.INVALID_PARAMS)
        unknown_method = server.handle({"jsonrpc": "2.0", "id": 7, "method": "does/not/exist"})
        self.assertEqual(unknown_method["error"]["code"], protocol.METHOD_NOT_FOUND)

    def test_notifications_are_never_answered(self):
        server = self._server()
        self.assertIsNone(
            server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
        )

    def test_serve_survives_a_malformed_line(self):
        from core.mcp import protocol

        server = self._server()
        stream = io.StringIO(
            "not json\n"
            + protocol.dumps(protocol.request(1, "tools/list"))
            + "\n"
            + protocol.dumps(protocol.request(2, "ping"))
            + "\n"
        )
        out = io.StringIO()
        server.serve(stream, out)
        replies = [json.loads(line) for line in out.getvalue().splitlines()]
        self.assertEqual(replies[0]["error"]["code"], protocol.PARSE_ERROR)
        self.assertIn("tools", replies[1]["result"])


class ConfigValidationTest(unittest.TestCase):
    def _config(self, mcp, tmp):
        from core.config import load_config

        path = os.path.join(tmp, "config.json")
        document = {
            "tools": ["read_file"],
            "evaluator": {"type": "none"},
            "logging": {"type": "jsonl", "path": os.path.join(tmp, "run.jsonl")},
            "mcp": mcp,
        }
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(document, handle)
        return load_config(path)

    def test_a_well_formed_section_is_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = self._config(
                {"servers": {"demo": {"command": ["python", "-m", "demo"], "env": {"A": "1"}}}},
                tmp,
            )
        self.assertIn("demo", config["mcp"]["servers"])

    def test_unknown_keys_are_rejected(self):
        from core.agent.exceptions import ConfigError

        cases = (
            {"servers": {"demo": {"command": "x", "bogus": 1}}},
            {"servers": {"demo": {"not_command": "x"}}},
            {"bogus": {}},
        )
        for mcp in cases:
            with tempfile.TemporaryDirectory() as tmp:
                with self.assertRaises(ConfigError, msg=repr(mcp)):
                    self._config(mcp, tmp)

    def test_a_missing_or_empty_command_is_rejected(self):
        from core.agent.exceptions import ConfigError

        for command in ([], [""], 5, None):
            with tempfile.TemporaryDirectory() as tmp:
                with self.assertRaises(ConfigError, msg=repr(command)):
                    self._config({"servers": {"demo": {"command": command}}}, tmp)


class _ServerFixture:
    """Builds a workspace plus a config the MCP server can run from."""

    def __init__(self, test, tools=("read_file",), approvals="yolo"):
        self.root = tempfile.mkdtemp(prefix="mantra-mcp-")
        test.addCleanup(shutil.rmtree, self.root, True)
        self.workspace = os.path.join(self.root, "ws")
        os.makedirs(self.workspace, exist_ok=True)
        with open(os.path.join(self.workspace, "note.txt"), "w", encoding="utf-8", newline="\n") as handle:
            handle.write("hello from the workspace\n")
        self.config_path = os.path.join(self.root, "config.json")
        document = {
            "tools": list(tools),
            "sandbox": {"provider": "local"},
            "evaluator": {"type": "none"},
            "logging": {"type": "jsonl", "path": os.path.join(self.root, "run.jsonl")},
            "approvals": approvals,
        }
        with open(self.config_path, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(document, handle)

    def command(self):
        return [
            sys.executable,
            "-m",
            "core.mcp",
            "--config",
            self.config_path,
            "--workspace",
            self.workspace,
        ]


class ClientServerRoundTripTest(unittest.TestCase):
    """Both directions at once: a real server process driven by the client."""

    def _client(self, **kwargs):
        from core.mcp import MCPClient

        fixture = _ServerFixture(self, **kwargs)
        client = MCPClient(fixture.command(), name="demo", cwd=_PROJECT_ROOT, timeout=60.0)
        self.addCleanup(client.close)
        client.start()
        client.initialize()
        return client, fixture

    def test_list_and_call_a_real_server_tool(self):
        client, _fixture = self._client()
        tools = client.list_tools()
        self.assertIn("read_file", [tool["name"] for tool in tools])

        text, is_error = client.call_tool("read_file", {"path": "note.txt"})
        self.assertFalse(is_error, text)
        self.assertIn("hello from the workspace", text)

    def test_the_default_approval_mode_is_honoured_over_mcp(self):
        # approvals=plan refuses every mutating tool, so the write is
        # refused at the server rather than performed.
        client, _fixture = self._client(tools=("write_file",), approvals="plan")
        text, is_error = client.call_tool("write_file", {"path": "new.txt", "content": "x"})
        self.assertTrue(is_error, text)
        self.assertFalse(os.path.exists(os.path.join(_fixture.workspace, "new.txt")))

    def test_adapters_register_as_external_tools(self):
        from core.agent.approvals import classify
        from core.mcp.client import MCPToolAdapter

        client, _fixture = self._client()
        adapter = MCPToolAdapter(client, client.list_tools()[0])

        self.assertTrue(adapter.name.startswith("mcp__demo__"), adapter.name)
        self.assertEqual(adapter.name, adapter.name.lower())
        self.assertNotIn("__-", adapter.name)
        # A remote effect is never classified as silently safe.
        self.assertEqual(classify(adapter.name, {"path": "note.txt"})[0], "confirm")

        observation = adapter.execute(None, path="note.txt")
        self.assertIn("hello from the workspace", observation)

    def test_build_mcp_tools_reports_a_server_that_cannot_start(self):
        from core.mcp.client import build_mcp_tools

        failures: list[str] = []
        tools, clients = build_mcp_tools(
            {"servers": {"broken": {"command": ["definitely-not-a-real-binary-xyz"]}}},
            on_error=failures.append,
        )
        self.assertEqual(tools, [])
        self.assertEqual(clients, [])
        self.assertTrue(failures, "a failed server must be reported")

    def test_build_mcp_tools_skips_a_disabled_server(self):
        from core.mcp.client import build_mcp_tools

        tools, clients = build_mcp_tools(
            {"servers": {"off": {"command": ["definitely-not-a-real-binary-xyz"], "enabled": False}}}
        )
        self.assertEqual((tools, clients), ([], []))


if __name__ == "__main__":
    unittest.main()
