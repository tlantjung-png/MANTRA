"""Entry point: serve this harness's tools over the stdio MCP transport.

Usage: ``python -m core.mcp --config <path> [--workspace <dir>]``

The server exposes exactly the tool list the configuration names, gated
by the configured approval mode, so a remote client cannot reach
anything the local path would have refused. stdout carries protocol
messages only; every diagnostic goes to stderr.
"""

from __future__ import annotations

import argparse
import os
import sys

from core.agent.approvals import ApprovalPolicy
from core.agent.exceptions import ConfigError
from core.config import load_config
from core.mcp.server import MCPServer, run_stdio
from core.registry import build_sandbox, build_tools
from core.term import force_utf8_output

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _resolve_arg(path: str) -> str:
    """Resolve an input path against cwd, then the project root."""
    if os.path.exists(path):
        return path
    candidate = os.path.join(PROJECT_ROOT, path)
    return candidate if os.path.exists(candidate) else path


def _note(message: str) -> None:
    # stderr, never stdout: a stray line on stdout desynchronizes the stream.
    print(f"[mcp] {message}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mantra-mcp", description="MCP server for the MANTRA tool set")
    parser.add_argument("--config", required=True, help="Path to config file (resolved against cwd then project root)")
    parser.add_argument("--workspace", default=None, help="Workspace directory (defaults to the current directory)")
    args = parser.parse_args(argv)
    force_utf8_output()

    try:
        config = load_config(_resolve_arg(args.config))
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    workspace = os.path.abspath(args.workspace or os.getcwd())
    sandbox_config = dict(config.get("sandbox") or {})
    if sandbox_config.get("provider", "local") == "local":
        # The host sandbox takes its workspace through "workspace_root";
        # any other key is rejected by the registry's constructor check.
        sandbox_config["workspace_root"] = workspace
    try:
        sandbox = build_sandbox(sandbox_config)
        tools = build_tools(config.get("tools") or [])
    except (ConfigError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    # An external client gets the same gate the local path applies. With no
    # interactive terminal there is no one to answer a prompt, so "default"
    # and "auto" resolve mutations without asking, exactly as the headless
    # runner does; yolo allows everything and plan refuses mutations.
    policy = ApprovalPolicy(mode=config.get("approvals", "yolo"), note=_note)

    server = MCPServer(tools=tools, sandbox=sandbox, approvals=policy, note=_note)
    _note(f"serving {len(tools)} tools over stdio (approvals: {policy.mode})")
    try:
        return run_stdio(server)
    finally:
        try:
            sandbox.cleanup()
        except Exception as exc:  # cleanup must not mask the exit status
            _note(f"sandbox cleanup failed: {exc}")


if __name__ == "__main__":
    raise SystemExit(main())
