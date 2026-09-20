"""MCP client: bridge an external MCP server's tools into the registry.

Each configured server is spawned as a child process speaking the stdio
transport. Its tools are listed once at startup and wrapped in
``MCPToolAdapter``, which is an ordinary local ``Tool`` with a prefixed
name, so the agent loop, the approval policy, and the registry need no
special case for remote tools.

Two deliberate choices:

* a remote tool is always classified for explicit confirmation, because
  its effect happens outside this process and is invisible to the local
  command screen (see ``register_external_tool``);
* the tool runs in the remote server's own environment, so the adapter
  ignores the local sandbox rather than pretending to confine anything.
"""

from __future__ import annotations

import os
import queue
import re
import subprocess
import threading
import time
from typing import Any

from core.agent.approvals import register_external_tool
from core.mcp import protocol
from core.types import Sandbox
from core.types import Tool

_STDERR_KEEP = 40
_NAME_SAFE = re.compile(r"[^a-z0-9_]+")


class MCPError(Exception):
    """A failure starting, talking to, or shutting down an MCP server."""


class MCPClient:
    """One stdio MCP connection."""

    def __init__(
        self,
        command: list[str],
        name: str = "mcp",
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout: float = 30.0,
    ) -> None:
        if isinstance(command, str):
            # A one-line command is split like a shell would, so a config
            # may write either a string or an argv list.
            import shlex

            command = shlex.split(command, posix=os.name != "nt")
        if not command:
            raise MCPError("an MCP server needs a command to launch")
        self.command = [str(part) for part in command]
        self.name = name
        self.cwd = cwd
        self.timeout = timeout
        self._env = dict(os.environ)
        if env:
            self._env.update({str(k): str(v) for k, v in env.items()})
        self._proc: subprocess.Popen[str] | None = None
        self._lines: queue.Queue[str | None] = queue.Queue()
        self._stderr: list[str] = []
        self._next_id = 0
        self._closed = False

    # ------------------------------------------------------------------ start

    def start(self) -> None:
        """Spawn the server and begin draining its output."""
        if self._proc is not None:
            return
        try:
            self._proc = subprocess.Popen(
                self.command,
                cwd=self.cwd,
                env=self._env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
        except OSError as exc:
            raise MCPError(f"cannot start MCP server {self.name!r}: {exc}") from exc
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

    def _read_stdout(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        try:
            for line in proc.stdout:
                self._lines.put(line)
        except (OSError, ValueError):
            pass
        # EOF sentinel: a waiter blocked on a dead server must not hang.
        self._lines.put(None)

    def _read_stderr(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        try:
            for line in proc.stderr:
                self._stderr.append(line.rstrip("\n"))
                if len(self._stderr) > _STDERR_KEEP:
                    del self._stderr[0]
        except (OSError, ValueError):
            pass

    def stderr_tail(self) -> list[str]:
        """Recent server diagnostics, for error messages."""
        return list(self._stderr)

    # ------------------------------------------------------------------- talk

    def _write(self, message: dict[str, Any]) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None or proc.poll() is not None:
            raise MCPError(f"MCP server {self.name!r} is not running")
        try:
            proc.stdin.write(protocol.dumps(message) + "\n")
            proc.stdin.flush()
        except (OSError, ValueError) as exc:
            raise MCPError(f"cannot write to MCP server {self.name!r}: {exc}") from exc

    def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Send a request and return its result payload."""
        self._next_id += 1
        message_id = self._next_id
        self._write(protocol.request(message_id, method, params))
        return self._await(message_id, method)

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        """Send a notification (no answer expected)."""
        self._write(protocol.notification(method, params))

    def _await(self, message_id: int, method: str) -> dict[str, Any]:
        deadline = time.monotonic() + self.timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise MCPError(
                    f"timeout waiting for {method!r} from MCP server {self.name!r}"
                )
            try:
                line = self._lines.get(timeout=remaining)
            except queue.Empty:
                raise MCPError(
                    f"timeout waiting for {method!r} from MCP server {self.name!r}"
                ) from None
            if line is None:
                tail = "\n".join(self.stderr_tail()[-5:])
                raise MCPError(
                    f"MCP server {self.name!r} closed the connection"
                    + (f"\n{tail}" if tail else "")
                )
            line = line.strip()
            if not line:
                continue
            try:
                message = protocol.loads(line)
            except protocol.ProtocolError:
                # Non-protocol output on stdout is a server bug; skip it
                # rather than failing the whole connection on one line.
                continue
            # Ignore anything that is not the response we are waiting for.
            if message.get("id") != message_id:
                continue
            error = message.get("error")
            if isinstance(error, dict):
                raise MCPError(
                    f"MCP server {self.name!r} returned an error for {method!r}: "
                    f"{error.get('message', 'unknown')}"
                )
            result = message.get("result")
            return result if isinstance(result, dict) else {}

    # ------------------------------------------------------------- handshake

    def initialize(self) -> dict[str, Any]:
        """Perform the initialize handshake, then announce readiness."""
        result = self.request(
            "initialize",
            {
                "protocolVersion": protocol.PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": protocol.SERVER_NAME, "version": protocol.SERVER_VERSION},
            },
        )
        self.notify("notifications/initialized")
        return result

    def list_tools(self) -> list[dict[str, Any]]:
        """Every tool the server advertises."""
        result = self.request("tools/list")
        tools = result.get("tools")
        if not isinstance(tools, list):
            return []
        return [spec for spec in tools if isinstance(spec, dict) and spec.get("name")]

    def call_tool(self, name: str, arguments: dict[str, Any]) -> tuple[str, bool]:
        """Run a remote tool; returns ``(text, is_error)``."""
        result = self.request("tools/call", {"name": name, "arguments": arguments})
        text_parts = []
        for block in result.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "text":
                text_parts.append(str(block.get("text", "")))
        text = "\n".join(part for part in text_parts if part)
        if not text:
            text = "(no content returned)"
        return text, bool(result.get("isError"))

    # ------------------------------------------------------------------ close

    def close(self) -> None:
        """Terminate the child and stop its reader threads."""
        if self._closed:
            return
        self._closed = True
        proc = self._proc
        if proc is None:
            return
        try:
            if proc.stdin is not None:
                proc.stdin.close()
        except (OSError, ValueError):
            pass
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except (OSError, subprocess.TimeoutExpired):
            try:
                proc.kill()
            except OSError:
                pass
        for stream in (proc.stdout, proc.stderr):
            try:
                if stream is not None:
                    stream.close()
            except (OSError, ValueError):
                pass
        self._proc = None


def _tool_name(server: str, remote: str) -> str:
    """A local tool name for a remote tool.

    Function-calling names are restricted to lowercase letters, digits
    and underscores, so the server and remote names are folded into that
    alphabet rather than passed through raw.
    """
    folded = _NAME_SAFE.sub("_", f"mcp__{server}__{remote}".lower()).strip("_")
    return folded or "mcp__tool"


class MCPToolAdapter(Tool):
    """A remote MCP tool presented as an ordinary local tool."""

    def __init__(self, client: MCPClient, spec: dict[str, Any]) -> None:
        remote = str(spec["name"])
        self.client = client
        self.remote_name = remote
        self.server_name = client.name
        self.name = _tool_name(client.name, remote)
        description = spec.get("description") or f"Remote MCP tool {remote} on {client.name}."
        self.description = f"[MCP:{client.name}] {description}"
        schema = spec.get("inputSchema")
        self.parameters = schema if isinstance(schema, dict) else {"type": "object", "properties": {}}
        # A remote effect cannot be screened locally, so it is always
        # confirmed rather than silently allowed.
        register_external_tool(self.name)

    def execute(self, sandbox: Sandbox, **kwargs: Any) -> str:  # noqa: ARG002 - remote execution owns the environment
        try:
            text, is_error = self.client.call_tool(self.remote_name, dict(kwargs))
        except MCPError as exc:
            return f"ERROR: {exc}"
        return f"ERROR: {text}" if is_error else text


def build_mcp_tools(
    config: dict[str, Any] | None,
    on_error: Any = None,
) -> tuple[list[Tool], list[MCPClient]]:
    """Tools and live clients for every configured MCP server.

    ``config`` is the ``mcp`` configuration section: ``{"servers": {name:
    {"command": [...], "cwd": str, "env": {...}, "enabled": bool}}}``. A
    server that fails to start is reported through ``on_error`` and
    skipped, so one broken optional server cannot stop the harness from
    starting.
    """
    servers = (config or {}).get("servers") or {}
    tools: list[Tool] = []
    clients: list[MCPClient] = []
    if not isinstance(servers, dict):
        return tools, clients
    for name, spec in servers.items():
        if not isinstance(spec, dict) or spec.get("enabled", True) is False:
            continue
        client = MCPClient(
            spec.get("command") or [],
            name=str(name),
            cwd=spec.get("cwd"),
            env=spec.get("env"),
            timeout=float(spec.get("timeout") or 30.0),
        )
        try:
            client.start()
            client.initialize()
            specs = client.list_tools()
        except (MCPError, OSError, ValueError) as exc:
            client.close()
            if callable(on_error):
                on_error(str(exc))
            continue
        clients.append(client)
        tools.extend(MCPToolAdapter(client, item) for item in specs)
    return tools, clients
