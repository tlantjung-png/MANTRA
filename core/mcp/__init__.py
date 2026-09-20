"""MCP (Model Context Protocol) support, in both directions.

* ``core.mcp.server`` exposes this harness's tools to an external MCP
  client over the stdio transport (``python -m core.mcp``).
* ``core.mcp.client`` bridges an external MCP server's tools into the
  local tool registry as ordinary tools.

Nothing here adds a third-party dependency: the transport and the
message shapes are implemented in ``protocol.py``.
"""

from core.mcp.client import MCPClient, MCPError, MCPToolAdapter, build_mcp_tools
from core.mcp.protocol import PROTOCOL_VERSION
from core.mcp.server import MCPServer, run_stdio

__all__ = [
    "MCPClient",
    "MCPError",
    "MCPServer",
    "MCPToolAdapter",
    "PROTOCOL_VERSION",
    "build_mcp_tools",
    "run_stdio",
]
