# Split out of core/console.py; import it from core.console, never from here.

"""The slash-command handlers split out of console.py.

The command surfaces that remain (diff, approve, cost, compact, clear,
sessions, suggestions, mcp). Calls into console go through the module
namespace so test patches of the interactive seams (menus, the
multiline reader, the model picker) stay effective.
"""

from __future__ import annotations

# Calls go through the console namespace so the test suite's
# seam patches (mock.patch("core.console.<name>")) stay effective.
# Patchable seams: _menu, _read_multiline, _choose_model, _model_command.
from core import console as _c

import json
import os
import re

from core.agent.approvals import MODES
from core.mcp.client import MCPError
from core.tui.overlays import Option

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # Type-only: console.py imports this module back, so a runtime import
    # here would be circular. ruff F821 is otherwise raised on every
    # forward reference in signatures.
    from core.console import ConsoleSession

def _no_arg(session: "ConsoleSession", command: str, argument: str, action) -> None:
    """Run a no-argument command, or say so instead of dropping the argument.

    A stray argument used to vanish without a word, so ``/diff --stat``
    looked like it had done something it never did.
    """
    if argument:
        session._print(session.style.dim(f"  usage: {command} (takes no arguments)"))
        return
    action()


def _diff(session: "ConsoleSession") -> None:
    """/diff: show the workspace changeset."""
    # In the TUI the changeset opens in the full-screen review;
    # everywhere else it stays the plain text diff.
    layout = session.layout
    if layout is not None and layout.active:
        diff = session._git("diff", "--no-color", "--unified=3") or ""
        if diff and getattr(layout, "open_review", None):
            layout.open_review(diff)
        else:
            session.show_diff()
    else:
        session.show_diff()


def _approve(session: "ConsoleSession", argument: str) -> None:
    """/approve: show or change the approval mode."""
    if not argument:
        # Modes are a fixed list, so they get a menu too.
        chosen = _c._menu(
            session,
            "approval mode",
            [Option(value=m, hint="current" if m == session.approvals.mode else "")
             for m in MODES],
            allow_filter=False,
        )
        if chosen and chosen in MODES:
            session.approvals.mode = chosen
            session.approvals.reset_session()
            _persist_approvals(session, chosen)
            session._print(f"approval mode is now {chosen}")
            if session.layout is not None and session.layout.active:
                session.layout.draw_chrome()
        else:
            session._print(f"approval mode: {session.approvals.mode}  (choose: {'/'.join(MODES)})")
    elif argument in MODES:
        session.approvals.mode = argument
        session.approvals.reset_session()
        _persist_approvals(session, argument)
        session._print(f"approval mode is now {argument}")
        if session.layout is not None and session.layout.active:
            session.layout.draw_chrome()
    else:
        session._print(f"unknown mode '{argument}'; choose one of {'/'.join(MODES)}")


def _persist_approvals(session: "ConsoleSession", mode: str) -> None:
    """Carry the approval mode into the next session.

    The mode is an operator preference, not a per-run detail: persisting it
    means a restarted session picks up where the operator left off instead
    of silently reverting to the default and prompting again.
    """
    try:
        from core.agent.settings import set_ui_prefs

        set_ui_prefs(approvals=mode)
    except Exception:
        # A failed preference write must not block the mode change that
        # just succeeded in memory.
        session._print("warning: could not write the settings file")


def _cost(session: "ConsoleSession") -> None:
    """/cost: spend so far. One form; scripting reads the session file."""
    session.show_cost()


def _compact(session: "ConsoleSession") -> None:
    """/compact: shrink the conversation context."""
    if not session.compact():
        session._print("nothing to compact")


def _clear(session: "ConsoleSession") -> None:
    """/clear: start a fresh conversation."""
    # Clearing is one action with one name; the checklist is work state,
    # not conversation, so it outlives the clear.
    session.message_count = 0
    session.context.replace_body([])
    session.approvals.reset_session()
    session.reported_changes.clear()
    session.active_skills = []
    session.auto_attached = []
    # Release the saved-session name: autosave reuses it for the life of
    # a session, so keeping it would make the next save overwrite the
    # pre-clear transcript and it could no longer be resumed.
    session.session_name = ""
    if session.layout is not None and session.layout.active:
        session.layout.clear_content()
    session._print("conversation cleared (files kept)")


def _sessions(session: "ConsoleSession", argument: str) -> None:
    """/sessions: browse, list, or resume a saved session."""
    # The session manager: browse (panel in the TUI, text picker
    # elsewhere), list, or resume a named session directly.
    parts = argument.split() if argument else []
    layout = session.layout
    if not parts:
        if layout is not None and layout.active and getattr(layout, "open_sessions", None):
            layout.open_sessions()
        else:
            session.pick_session()
    elif parts[0] in ("list", "show"):
        session.show_sessions()
    else:
        session.resume_session(parts[0])


def _set_suggestions(session: "ConsoleSession", argument: str) -> None:
    """Flip the post-task suggestion row at runtime (/suggestions on|off)."""
    from core.agent.settings import set_ui_prefs

    arg = argument.strip().lower()
    ui = getattr(session, "ui", None)
    if arg in ("on", "off"):
        enabled = arg == "on"
        # Persist to the settings file on disk (survives restarts), the
        # session config (this process), and the live TUI flag.
        if not set_ui_prefs(suggestions=enabled):
            session._print("warning: could not write the settings file")
        session.config["suggestions"] = enabled
        if ui is not None and hasattr(ui, "suggestions_enabled"):
            ui.suggestions_enabled = enabled
            if not enabled:
                ui._dismiss_suggestions()
        state = "on" if enabled else "off"
        # The toast rides the TUI when there is one; the plain REPL
        # gets the same message as a printed line.
        if ui is not None and hasattr(ui, "toast_message"):
            ui.toast_message(f"suggestions {state}", seconds=2.0)
        else:
            session._print(f"suggestions {state}")
        return
    # Bare /suggestions: report the current state.
    current = None
    if ui is not None and hasattr(ui, "suggestions_enabled"):
        current = ui.suggestions_enabled
    else:
        current = bool(session.config.get("suggestions", True))
    session._print(f"suggestions are {'on' if current else 'off'} - usage: /suggestions on|off")


# ------------------------------------------------------------------- mcp


def _mcp(session: "ConsoleSession", argument: str) -> None:
    """/mcp: inspect the configured external tool servers.

    Read-only on purpose: starting or stopping a server edits the
    operator's config file, so that happens in the file (and at the next
    start) rather than from a chat command mid-session.
    """
    parts = argument.split()
    if not parts or parts[0] in ("list", "status"):
        _mcp_status(session)
    elif parts[0] == "tools":
        _mcp_tools(session, parts[1] if len(parts) > 1 else "")
    else:
        session._print(
            session.style.dim(
                "  usage: /mcp [list], /mcp tools [server]"
            )
        )


def _mcp_servers(session: "ConsoleSession") -> dict:
    """The configured server map, defensively normalised to a dict."""
    servers = (session.config.get("mcp") or {}).get("servers") or {}
    return servers if isinstance(servers, dict) else {}


def _mcp_client(session: "ConsoleSession", name: str):
    """The live client for *name*, or None when it is not running."""
    for client in getattr(session, "mcp_clients", []) or []:
        if getattr(client, "name", "") == name:
            return client
    return None


def _mcp_status(session: "ConsoleSession") -> None:
    servers = _mcp_servers(session)
    if not servers:
        session._print(
            session.style.dim(
                "  no MCP servers configured - add them under mcp.servers "
                "in the config file"
            )
        )
        return
    session._print(session.style.bold("  mcp servers"))
    for name in sorted(servers):
        spec = servers[name]
        if not isinstance(spec, dict):
            session._print(f"  {name}  {session.style.ember('(malformed entry)')}")
            continue
        command = spec.get("command") or []
        command_text = (
            " ".join(str(part) for part in command)
            if isinstance(command, list)
            else str(command)
        )
        client = _mcp_client(session, name)
        if spec.get("enabled", True) is False and client is None:
            state = session.style.dim("disabled")
        elif client is not None:
            count = sum(
                1 for tool in session.tools
                if getattr(tool, "server_name", "") == name
            )
            state = session.style.dim(f"connected, {count} tool(s)")
        else:
            state = session.style.dim("enabled, not running")
        session._print(f"  {name}  {state}")
        session._print(session.style.dim(f"      {command_text}"))
    session._print(
        session.style.dim(
            "  /mcp tools <server> lists a connected server's tools; "
            "servers start per the config file's enabled flags"
        )
    )


def _mcp_tools(session: "ConsoleSession", name: str) -> None:
    if not name:
        session._print(session.style.dim("  usage: /mcp tools <server>"))
        return
    client = _mcp_client(session, name)
    if client is None:
        session._print(
            session.style.warn(
                f"  {name} is not running - enable it under mcp.servers."
                f"{name} in the config file and restart"
            )
        )
        return
    try:
        specs = client.list_tools()
    except MCPError as exc:
        session._print(session.style.ember(f"  {name} did not answer: {exc}"))
        return
    if not specs:
        session._print(session.style.dim(f"  {name} advertises no tools"))
        return
    session._print(session.style.bold(f"  {name} tools"))
    for spec in specs:
        remote = str(spec.get("name", "?"))
        description = str(spec.get("description") or "").splitlines()[0][:80]
        session._print(f"  {remote}")
        if description:
            session._print(session.style.dim(f"      {description}"))

