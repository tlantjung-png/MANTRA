"""MCP server: expose this harness's tools to an external MCP client.

The server speaks the newline-delimited JSON-RPC stdio transport from
``protocol.py``. It holds a tool mapping and a sandbox, because an MCP
tool call has to run somewhere: the caller supplies the same concrete
tools and sandbox the agent loop uses, so an MCP client sees exactly the
tool set (and the same confinement) the harness itself has.

Approval is not bypassed. When a policy is supplied, every tools/call is
gated through it first, so an external client cannot reach a destructive
action that the local interactive path would have refused.

stdout carries protocol messages only. Diagnostics go to stderr, because
a stray print on stdout would corrupt the stream.
"""

from __future__ import annotations

import sys
from typing import IO, Any, Callable

from core.mcp import protocol
from core.types import Sandbox
from core.types import Tool


class MCPServer:
    """A minimal MCP server over one tool mapping and one sandbox."""

    def __init__(
        self,
        tools: dict[str, Tool] | list[Tool],
        sandbox: Sandbox,
        approvals: Any = None,
        name: str = protocol.SERVER_NAME,
        version: str = protocol.SERVER_VERSION,
        note: Callable[[str], None] | None = None,
    ) -> None:
        if isinstance(tools, dict):
            self.tools: dict[str, Tool] = dict(tools)
        else:
            self.tools = {tool.name: tool for tool in tools}
        self.sandbox = sandbox
        self.approvals = approvals
        self.name = name
        self.version = version
        self._note = note or (lambda message: None)
        self.initialized = False

    # ------------------------------------------------------------------ list

    def tool_specs(self) -> list[dict[str, Any]]:
        """The tools/list payload: name, description, and input schema."""
        specs = []
        for tool in sorted(self.tools.values(), key=lambda t: t.name):
            function = tool.schema()["function"]
            specs.append(
                {
                    "name": function["name"],
                    "description": function.get("description", ""),
                    "inputSchema": function.get("parameters") or {
                        "type": "object",
                        "properties": {},
                    },
                }
            )
        return specs

    # --------------------------------------------------------------- dispatch

    def handle(self, message: dict[str, Any]) -> dict[str, Any] | None:
        """Answer one message; None means it was a notification.

        Never raises for a client-caused fault: a bad request gets a
        JSON-RPC error response so the client can recover, rather than
        killing the connection.
        """
        if message.get("jsonrpc") != "2.0":
            return protocol.error(message.get("id"), protocol.INVALID_REQUEST, "not a JSON-RPC 2.0 message")
        method = message.get("method")
        if not isinstance(method, str):
            return protocol.error(message.get("id"), protocol.INVALID_REQUEST, "method must be a string")
        message_id = message.get("id")
        params = message.get("params")
        if params is not None and not isinstance(params, dict):
            return protocol.error(message_id, protocol.INVALID_PARAMS, "params must be an object")
        params = params or {}

        # A request carries an id and must be answered; a notification does
        # not and must not be.
        if message_id is None:
            self._notification(method, params)
            return None

        if method == "initialize":
            return protocol.result(message_id, self._initialize(params))
        if method == "ping":
            return protocol.result(message_id, {})
        if method == "tools/list":
            return protocol.result(message_id, {"tools": self.tool_specs()})
        if method == "tools/call":
            return self._call(message_id, params)
        if method == "notifications/initialized":
            return protocol.result(message_id, {})
        return protocol.error(message_id, protocol.METHOD_NOT_FOUND, f"unknown method {method!r}")

    def _notification(self, method: str, params: dict[str, Any]) -> None:
        if method == "notifications/initialized":
            # The client finished the handshake; nothing to do but record it.
            self.initialized = True
            return
        self._note(f"ignored notification {method!r}")

    def _initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        version = protocol.negotiate_version(params.get("protocolVersion"))
        self.initialized = True
        return {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": self.name, "version": self.version},
        }

    def _call(self, message_id: Any, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        if not isinstance(name, str) or not name:
            return protocol.error(message_id, protocol.INVALID_PARAMS, "tools/call requires a tool name")
        arguments = params.get("arguments")
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            return protocol.error(message_id, protocol.INVALID_PARAMS, "arguments must be an object")
        tool = self.tools.get(name)
        if tool is None:
            return protocol.error(message_id, protocol.INVALID_PARAMS, f"unknown tool {name!r}")

        if not self._allowed(name, arguments):
            self._note(f"denied tool call {name!r} by approval policy")
            return protocol.result(
                message_id,
                protocol.tool_result(
                    f"refused: the approval policy did not allow {name!r}", is_error=True
                ),
            )
        try:
            observation = tool.execute(self.sandbox, **arguments)
        except Exception as exc:  # tool bugs must not kill the session
            self._note(f"tool {name!r} raised: {exc}")
            return protocol.result(
                message_id,
                protocol.tool_result(f"tool {name!r} failed: {exc}", is_error=True),
            )
        return protocol.result(message_id, protocol.tool_result(str(observation)))

    def _allowed(self, name: str, arguments: dict[str, Any]) -> bool:
        if self.approvals is None:
            return True
        try:
            return bool(self.approvals.check(name, arguments))
        except Exception as exc:
            # Fail closed: an approval failure must not become an allow.
            self._note(f"approval check failed for {name!r}: {exc}")
            return False

    # ------------------------------------------------------------------- loop

    def serve(self, reader: IO[str], writer: IO[str]) -> None:
        """Read messages until EOF, answering each request.

        A malformed line is reported and the stream continues: one bad
        message must not take the server down.
        """
        for raw in reader:
            line = raw.strip()
            if not line:
                continue
            try:
                message = protocol.loads(line)
            except protocol.ProtocolError as exc:
                self._write(writer, protocol.error(None, exc.code, exc.message))
                continue
            response = self.handle(message)
            if response is not None:
                self._write(writer, response)

    def _write(self, writer: IO[str], message: dict[str, Any]) -> None:
        try:
            writer.write(protocol.dumps(message) + "\n")
            writer.flush()
        except (OSError, ValueError):
            # The client went away: there is nowhere left to report this.
            pass


def run_stdio(server: MCPServer) -> int:
    """Serve on the process's own stdio until the client disconnects."""
    server.serve(sys.stdin, sys.stdout)
    return 0
