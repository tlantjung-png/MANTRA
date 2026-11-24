"""Regressions for the tool / external-tool-bridge audit fixes.

Covers Medium-12..17 and Low-18..21. Every test uses temporary
directories or in-memory fakes; none touches the real user home or
project logs.
"""

from __future__ import annotations

import io
import json
import os
import queue
import tempfile
import threading
import types
import unittest
from unittest import mock

from core.types import ExecResult


class _RacySandbox:
    """LocalSandbox that lands a concurrent write on the second read."""

    def __init__(self, real):
        self._real = real
        self.read_count = 0
        self.armed = False

    @property
    def root(self):
        return self._real.root

    def setup(self, task):
        return self._real.setup(task)

    def read_file(self, path):
        self.read_count += 1
        if self.armed and self.read_count == 2:
            self._real.write_file(path, "concurrent version\n")
        return self._real.read_file(path)

    def write_file(self, path, content):
        return self._real.write_file(path, content)


class EditConditionalWriteTest(unittest.TestCase):
    """Medium-12: re-read and compare before the whole-file replacement."""

    def test_concurrent_change_is_refused_and_not_laundered(self):
        from core.sandbox import LocalSandbox
        from core.tools.files import EditFileTool, ReadFileTool
        from core.tools.ledger import EditLedger

        with tempfile.TemporaryDirectory(prefix="mantra-fix-m12-") as ws:
            real = LocalSandbox(ws)
            real.setup({})
            real.write_file("a.txt", "hello world\n")
            sandbox = _RacySandbox(real)
            ledger = EditLedger()

            reader = ReadFileTool()
            reader.ledger = ledger
            self.assertIn("hello world", reader.execute(sandbox, "a.txt"))
            self.assertTrue(ledger.is_current("a.txt", "hello world\n"))

            sandbox.read_count = 0
            sandbox.armed = True
            tool = EditFileTool()
            tool.ledger = ledger
            out = tool.execute(sandbox, "a.txt", "world", "there")

            self.assertIn("changed on disk", out)
            self.assertNotIn("OK:", out)
            with open(os.path.join(ws, "a.txt"), encoding="utf-8") as handle:
                self.assertEqual(handle.read(), "concurrent version\n")
            # The ledger still reflects the bytes the agent actually read.
            self.assertTrue(ledger.is_current("a.txt", "hello world\n"))
            self.assertFalse(ledger.is_current("a.txt", "concurrent version\n"))

    def test_unchanged_file_still_edits(self):
        from core.sandbox import LocalSandbox
        from core.tools.files import EditFileTool, ReadFileTool
        from core.tools.ledger import EditLedger

        with tempfile.TemporaryDirectory(prefix="mantra-fix-m12b-") as ws:
            sandbox = LocalSandbox(ws)
            sandbox.setup({})
            sandbox.write_file("a.txt", "hello world\n")
            ledger = EditLedger()
            reader = ReadFileTool()
            reader.ledger = ledger
            reader.execute(sandbox, "a.txt")
            tool = EditFileTool()
            tool.ledger = ledger
            out = tool.execute(sandbox, "a.txt", "world", "there")
            self.assertTrue(out.startswith("OK:"))
            self.assertEqual(sandbox.read_file("a.txt"), "hello there\n")


class _RecordingRootlessSandbox:
    """No ``root`` attribute: forces the extraction shell fallback."""

    def __init__(self):
        self.calls: list[str] = []

    def exec(self, command, timeout=120.0):
        self.calls.append(command)
        return ExecResult(exit_code=0, stdout="1\n", stderr="")


class ExtractTraversalTest(unittest.TestCase):
    """Medium-13: screen the shell fallback like the direct file view."""

    def test_traversal_and_absolute_paths_never_reach_the_shell(self):
        from core.tools.extract import ExtractDocumentTool

        for path in (
            "../etc/passwd",
            "sub/../../secret.txt",
            "/etc/passwd",
            "C:\\Windows\\win.ini",
            "C:/secret.txt",
        ):
            sandbox = _RecordingRootlessSandbox()
            out = ExtractDocumentTool().execute(sandbox, path)
            self.assertTrue(out.startswith("ERROR"), path)
            self.assertIn("escapes workspace", out)
            self.assertEqual(sandbox.calls, [], path)

    def test_shell_metacharacters_are_refused(self):
        from core.tools.extract import ExtractDocumentTool

        sandbox = _RecordingRootlessSandbox()
        out = ExtractDocumentTool().execute(sandbox, 'note.txt; cat /etc/passwd')
        self.assertIn("unsupported characters", out)
        self.assertEqual(sandbox.calls, [])

    def test_plain_relative_path_still_uses_the_shell_fallback(self):
        from core.tools.extract import ExtractDocumentTool

        sandbox = _RecordingRootlessSandbox()
        out = ExtractDocumentTool().execute(sandbox, "note.txt")
        self.assertEqual(len(sandbox.calls), 2)
        self.assertIn("note.txt", out)


class _FakePopen:
    """A Popen stand-in whose pipes hit EOF immediately."""

    last_instance = None

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.pid = 424242
        self.stdin = io.StringIO()
        self.stdout = io.StringIO("")
        self.stderr = io.StringIO("")
        self.terminated = False
        _FakePopen.last_instance = self

    def poll(self):
        return None

    def wait(self, timeout=None):
        return 0

    def terminate(self):
        self.terminated = True

    def kill(self):
        pass


class MCPLifecycleTest(unittest.TestCase):
    """Medium-14: process-group spawn, tree-kill shutdown, joined readers."""

    def test_start_uses_the_shared_process_group_options(self):
        from core.mcp.client import MCPClient
        from core.procutil import POPEN_GROUP_KWARGS

        with mock.patch("core.mcp.client.subprocess.Popen", _FakePopen):
            client = MCPClient(["fake-server"], name="demo", timeout=2)
            try:
                client.start()
                self.assertIsInstance(client._proc, _FakePopen)
                for key, value in POPEN_GROUP_KWARGS.items():
                    self.assertEqual(client._proc.kwargs.get(key), value)
            finally:
                client.close()

    def test_close_tree_kills_and_joins_reader_threads(self):
        from core.mcp.client import MCPClient

        with mock.patch("core.mcp.client.subprocess.Popen", _FakePopen), mock.patch(
            "core.mcp.client._kill_process_tree"
        ) as tree_kill:
            client = MCPClient(["fake-server"], name="demo", timeout=2)
            client.start()
            readers = list(client._readers)
            proc = client._proc
            client.close()

            tree_kill.assert_called_once_with(proc)
            self.assertTrue(all(not reader.is_alive() for reader in readers))
            self.assertEqual(client._readers, [])
            self.assertIsNone(client._proc)


class MCPRequestRoutingTest(unittest.TestCase):
    """Medium-15: one waiter owns the shared response queue at a time."""

    def test_overlapping_requests_are_serialized(self):
        from core.mcp.client import MCPClient

        client = MCPClient(["fake-server"], timeout=3)
        entered = threading.Event()
        release = threading.Event()
        writes: list[int] = []

        def fake_write(message):
            writes.append(message["id"])
            if message["params"].get("block"):
                entered.set()
                self.assertTrue(release.wait(3))

        client._write = fake_write
        client._await = lambda message_id, method: {"id": message_id}

        first = threading.Thread(target=lambda: client.request("first", {"block": True}))
        first.start()
        self.assertTrue(entered.wait(3))
        second = threading.Thread(target=lambda: client.request("second", {}))
        second.start()
        second.join(timeout=0.4)
        # Without the request lock the second write would already be visible.
        self.assertEqual(writes, [1], writes)
        release.set()
        first.join(timeout=3)
        second.join(timeout=3)
        self.assertEqual(writes, [1, 2], writes)


class MCPBoundedTransportTest(unittest.TestCase):
    """Medium-16: line lengths and the response queue are bounded."""

    def test_loads_rejects_an_overlong_line(self):
        from core.mcp import protocol

        original = protocol.MAX_LINE_CHARS
        try:
            protocol.MAX_LINE_CHARS = 50
            with self.assertRaises(protocol.ProtocolError) as ctx:
                protocol.loads("x" * 200)
            self.assertIn("too long", str(ctx.exception))
            self.assertEqual(protocol.loads('{"jsonrpc":"2.0"}')["jsonrpc"], "2.0")
        finally:
            protocol.MAX_LINE_CHARS = original

    def test_server_reports_an_overlong_line_and_continues(self):
        from core.mcp import protocol
        from core.mcp.server import MCPServer

        original = protocol.MAX_LINE_CHARS
        try:
            protocol.MAX_LINE_CHARS = 50
            reader = io.StringIO(
                "x" * 200 + "\n" + protocol.dumps(protocol.request(1, "ping")) + "\n"
            )
            out = io.StringIO()
            MCPServer(tools=[], sandbox=object()).serve(reader, out)
            replies = [json.loads(line) for line in out.getvalue().splitlines()]
            self.assertTrue(
                any(reply.get("error", {}).get("message") == "line too long" for reply in replies)
            )
            self.assertTrue(any(reply.get("id") == 1 for reply in replies))
        finally:
            protocol.MAX_LINE_CHARS = original

    def test_client_queue_overflow_is_bounded_and_reported(self):
        from core.mcp import protocol
        from core.mcp.client import MCPClient, MCPError

        client = MCPClient(["fake-server"], timeout=2)
        client._lines = queue.Queue(maxsize=2)
        client._overflow.clear()
        proc = _FakePopen()
        proc.stdout = io.StringIO("noise\n" * 10)
        client._proc = proc
        client._read_stdout()

        self.assertLessEqual(client._lines.qsize(), 2)
        self.assertTrue(client._overflow.is_set())
        self.assertLessEqual(len(proc.stdout.getvalue()), protocol.MAX_LINE_CHARS * 10)
        with self.assertRaises(MCPError) as ctx:
            client._await(1, "ping")
        self.assertIn("overflow", str(ctx.exception))


class MCPBadAdvertisementTest(unittest.TestCase):
    """Medium-17: one broken tool spec must not abort harness startup."""

    def test_bad_advertisement_is_skipped_and_reported(self):
        from core.mcp import client as client_mod

        class _FakeClient:
            name = "demo"

            def __init__(self, *args, **kwargs):
                pass

            def start(self):
                pass

            def initialize(self):
                return {}

            def list_tools(self):
                return [
                    {
                        "name": "good",
                        "description": "ok",
                        "inputSchema": {"type": "object", "properties": {}},
                    },
                    {"description": "no name key - malformed"},
                ]

            def close(self):
                pass

        errors: list[str] = []
        with mock.patch.object(client_mod, "MCPClient", _FakeClient), mock.patch.object(
            client_mod, "register_external_tool"
        ):
            tools, clients = client_mod.build_mcp_tools(
                {"servers": {"demo": {"command": ["fake"]}}}, on_error=errors.append
            )

        self.assertEqual(len(clients), 1)
        self.assertEqual(len(tools), 1)
        self.assertTrue(tools[0].name.startswith("mcp__demo__good"))
        self.assertTrue(errors)
        self.assertIn("bad tool", errors[0])


class GlobAndHeaderTest(unittest.TestCase):
    """Low-18: reuse drive-prefix screening; cap the response header block."""

    def test_glob_branch_refuses_drive_prefixed_patterns(self):
        from core.sandbox import LocalSandbox
        from core.tools.files import ReadFileTool

        with tempfile.TemporaryDirectory(prefix="mantra-fix-m18-") as ws:
            sandbox = LocalSandbox(ws)
            sandbox.setup({})
            tool = ReadFileTool()
            for pattern in ("C:/Windows/*.txt", "C:\\Windows\\*.txt"):
                out = tool.execute(sandbox, pattern)
                self.assertTrue(out.startswith("ERROR"), pattern)
                self.assertIn("traversal blocked", out)

            sandbox.write_file("a.txt", "hello\n")
            self.assertTrue(tool.execute(sandbox, "*.txt").startswith("READ 1/1"))

    def test_oversized_header_block_is_refused_before_the_body(self):
        from core.sandbox import LocalSandbox
        from core.tools.web import WebFetchTool

        class _BigHeaders:
            def raw_items(self):
                return [("X-Huge", "h" * 100_000)]

            def get(self, key, default=""):
                return ""

            def get_content_charset(self):
                return None

        class _Response:
            headers = _BigHeaders()

            def read(self, size=-1):
                raise AssertionError("the body must not be read")

            def geturl(self):
                return "https://example.com/x"

            def getcode(self):
                return 200

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        with mock.patch("core.tools.web.urlopen", return_value=_Response()):
            out = WebFetchTool().execute(LocalSandbox(), "https://example.com/x")
        self.assertIn("headers too large", out)


class _VerboseTool:
    name = "verbose"
    description = "verbose"
    parameters = {"type": "object", "properties": {}}

    def schema(self):
        return {
            "type": "function",
            "function": {"name": self.name, "description": self.description, "parameters": self.parameters},
        }

    def execute(self, sandbox, **kwargs):
        return "x" * 500_000


class MCPOutputCapTest(unittest.TestCase):
    """Low-19: cap an observation before serializing it into one line."""

    def test_oversized_observation_is_truncated_with_a_note(self):
        from core.mcp import protocol
        from core.mcp.server import MCPServer, _MAX_OBSERVATION_CHARS

        server = MCPServer(tools=[_VerboseTool()], sandbox=object())
        response = server.handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "verbose", "arguments": {}},
            }
        )
        text = response["result"]["content"][0]["text"]
        body, marker = text.split("\n... [truncated:", 1)
        self.assertEqual(len(body), _MAX_OBSERVATION_CHARS)
        self.assertIn("observation exceeded", marker)
        self.assertEqual(len(protocol.dumps(response).splitlines()), 1)


class PumpJoinTest(unittest.TestCase):
    """Low-20: force the pipe closed when the output pump outlives its join."""

    def test_timeout_closes_the_pipe_and_records_a_warning(self):
        import core.tools.commands as commands

        class _StuckPump:
            def join(self, timeout=None):
                pass

            def is_alive(self):
                return True

        class _Stdout:
            def __init__(self):
                self.closed = False

            def close(self):
                self.closed = True

        task_id = "tsk_low20_test"
        commands._TASKS[task_id] = {}
        stdout = _Stdout()
        try:
            commands._join_output_pump(_StuckPump(), types.SimpleNamespace(stdout=stdout), task_id)
            self.assertTrue(stdout.closed)
            self.assertIn("cleanup_warning", commands._TASKS[task_id])
        finally:
            commands._TASKS.pop(task_id, None)

    def test_pump_that_finishes_leaves_the_pipe_alone(self):
        import core.tools.commands as commands

        class _DonePump:
            def join(self, timeout=None):
                pass

            def is_alive(self):
                return False

        class _Stdout:
            def __init__(self):
                self.closed = False

            def close(self):
                self.closed = True

        task_id = "tsk_low20b_test"
        commands._TASKS[task_id] = {}
        stdout = _Stdout()
        try:
            commands._join_output_pump(_DonePump(), types.SimpleNamespace(stdout=stdout), task_id)
            self.assertFalse(stdout.closed)
            self.assertNotIn("cleanup_warning", commands._TASKS[task_id])
        finally:
            commands._TASKS.pop(task_id, None)


class WindowsQuoteGuardTest(unittest.TestCase):
    """Low-21: quoted payloads are not mistaken for command-position tokens."""

    @unittest.skipUnless(os.name == "nt", "Windows-only guard")
    def test_quoted_payloads_do_not_trigger_a_false_refusal(self):
        from core.tools.commands import _windows_unix_guard

        self.assertIsNone(_windows_unix_guard('echo "a; grep b"'))
        self.assertIsNone(_windows_unix_guard('echo "a | cat b"'))
        self.assertIsNone(_windows_unix_guard('python -c "import os; print(1)"'))

    @unittest.skipUnless(os.name == "nt", "Windows-only guard")
    def test_bare_unix_utilities_are_still_flagged(self):
        from core.tools.commands import _windows_unix_guard

        self.assertIn("Select-String", _windows_unix_guard("grep -n hi f.txt"))
        self.assertIn("Get-Content", _windows_unix_guard("cat greeting.txt | grep hi"))


if __name__ == "__main__":
    unittest.main()
