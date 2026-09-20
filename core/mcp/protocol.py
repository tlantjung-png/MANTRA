"""MCP (Model Context Protocol) support: JSON-RPC 2.0 over stdio.

No third-party dependency is added for this. The stdio transport is
newline-delimited JSON, so the framing is one line in and one line out,
and every message shape is built here. ``server.py`` exposes this
harness's tools to an external MCP client; ``client.py`` bridges an
external MCP server's tools into the local tool registry.

Nothing in this module touches the network or the filesystem.
"""

from __future__ import annotations

import json
from typing import Any

# Protocol revision this implementation speaks. A client asking for an
# older supported revision is answered with its own version, which the
# specification allows.
PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_PROTOCOL_VERSIONS = ("2025-06-18", "2024-11-05")

SERVER_NAME = "mantra"
SERVER_VERSION = "0.1.0"

# JSON-RPC 2.0 error codes.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


class ProtocolError(Exception):
    """A malformed or unusable protocol message."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def dumps(message: dict[str, Any]) -> str:
    """Serialize one message for the stdio transport.

    Embedded newlines would split a single message into two lines and
    desynchronize the stream, so they are escaped by the JSON encoder
    itself (``json.dumps`` never emits a raw newline inside a string).
    """
    return json.dumps(message, ensure_ascii=False, separators=(",", ":"))


def loads(line: str) -> dict[str, Any]:
    """Parse one transport line into a message object."""
    try:
        message = json.loads(line)
    except (json.JSONDecodeError, ValueError) as exc:
        raise ProtocolError(PARSE_ERROR, f"invalid JSON: {exc}") from exc
    if not isinstance(message, dict):
        raise ProtocolError(INVALID_REQUEST, "message must be a JSON object")
    return message


def request(message_id: Any, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """A request: expects a response carrying the same id."""
    message: dict[str, Any] = {"jsonrpc": "2.0", "id": message_id, "method": method}
    if params is not None:
        message["params"] = params
    return message


def notification(method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """A notification: never answered, so it must not carry an id."""
    message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        message["params"] = params
    return message


def result(message_id: Any, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """A successful response."""
    return {"jsonrpc": "2.0", "id": message_id, "result": payload if payload is not None else {}}


def error(message_id: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
    """A failed response."""
    payload: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        payload["data"] = data
    return {"jsonrpc": "2.0", "id": message_id, "error": payload}


def text_content(text: str) -> dict[str, Any]:
    """The single content block every text-shaped tool result uses."""
    return {"type": "text", "text": text}


def tool_result(text: str, is_error: bool = False) -> dict[str, Any]:
    """A tools/call result payload.

    Tool failures are results with an error flag, not JSON-RPC errors:
    the protocol reserves protocol errors for protocol-level faults, and
    a failed tool call is information the caller must still receive.
    """
    payload: dict[str, Any] = {"content": [text_content(text)]}
    if is_error:
        payload["isError"] = True
    return payload


def negotiate_version(requested: Any) -> str:
    """The protocol revision to answer with.

    An unknown or missing revision is answered with this implementation's
    own, which tells the client what it is talking to rather than
    pretending to be something it is not.
    """
    if isinstance(requested, str) and requested in SUPPORTED_PROTOCOL_VERSIONS:
        return requested
    return PROTOCOL_VERSION
