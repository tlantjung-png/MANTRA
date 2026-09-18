# Split out of core/console.py; import it from core.console, never from here.

"""The slash-command handlers split out of console.py.

Goal, todo, and workflow live here alongside the smaller command
surfaces (memory, diff, fix, reasoning, approve, cost, compact, clear,
sessions, suggestions). Calls into console go through the module
namespace so test patches of the interactive seams (menus, the
multiline reader, the model picker) stay effective.
"""

from __future__ import annotations

# Calls go through the console namespace so the test suite's
# seam patches (mock.patch("core.console.<name>")) stay effective.
# Patchable seams: _menu, _read_multiline, _choose_model, _model_command.
from core import console as _c

import os
import re
import time

import core.agent.workflows as workflows
from core.agent.approvals import MODES
from core.tui.overlays import Option

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # Type-only: console.py imports this module back, so a runtime import
    # here would be circular. ruff F821 is otherwise raised on every
    # forward reference in signatures.
    from core.console import ConsoleSession

def _goal(session: "ConsoleSession", argument: str) -> None:
    """/goal: the standing objective the whole session is working toward.

    Subcommands are checked before the free-text form, because
    ``/goal note`` is a lot more likely to be meant as a subcommand than
    as a goal whose entire text is the word "note".
    """
    argument = argument.strip()
    if not argument:
        session.show_goal()
        return
    head, _, rest = argument.partition(" ")
    head = head.lower()
    rest = rest.strip()
    if head in ("done", "clear", "drop"):
        session.clear_goal(rest or "cleared by the operator")
    elif head in ("note", "add"):
        if not rest:
            session._print(session.style.dim("  usage: /goal note <text>"))
        else:
            session.add_goal_note(rest)
    elif head in ("show", "check", "status"):
        session.show_goal()
    else:
        # Not a subcommand, so the whole line is the goal.
        session.set_goal(argument)


def _todo(session: "ConsoleSession", argument: str) -> None:
    """/todo: the session checklist the agent works through.

    Bare /todo shows the list. Subcommands: add <text>, done <n|text>
    (number, or text matching an item), rm <n|text>, clear. done and rm
    accept a space- or comma-separated list. A bare /todo done opens a
    picker over the open items; bare /todo rm removes the done ones.
    """
    argument = argument.strip()
    if not argument:
        session.show_todos()
        return
    head, _, rest = argument.partition(" ")
    head = head.lower()
    rest = rest.strip()
    if head in ("add", "new"):
        session.add_todo(rest)
    elif head in ("done", "check", "rm", "remove", "delete", "drop"):
        mark = head in ("done", "check")
        # Bare /todo done opens a picker over the open items; bare
        # /todo rm drops the done ones. Both are friendlier than a
        # usage line and make the command discoverable.
        if not rest:
            if mark:
                picks = [Option(value=str(i + 1), label=item["text"]) for i, item in enumerate(session.todos) if not item["done"]]
                if not picks:
                    session._print(session.style.dim("  nothing open to check - /todo add <what needs doing>"))
                    return
                chosen = _c._menu(session, "Check off", picks)
                if chosen:
                    session.mark_todo_done(chosen)
            else:
                done = [item["text"] for item in session.todos if item["done"]]
                if not done:
                    session._print(session.style.dim("  no done items to remove - /todo lists them"))
                    return
                session.todos = [item for item in session.todos if not item["done"]]
                session._print(session.style.dim(f"  removed {len(done)} done todo(s)"))
            return
        # Resolve each target: a phrase that matches an item whole wins
        # (so "fix the header" is one item, not four tokens), otherwise
        # each space/comma-separated token is its own item number.
        targets = [rest]
        if session._find_todo(rest, open_only=mark) is None:
            targets = [t for t in re.split(r"[,\s]+", rest) if t]
        for target in targets:
            if mark:
                session.mark_todo_done(target)
            else:
                session.rm_todo(target)
    elif head in ("clear", "reset"):
        session.clear_todos()
    elif head in ("show", "list", "status"):
        session.show_todos()
    else:
        session._print(session.style.dim("  usage: /todo add <text> · done <n> · rm <n> · clear"))


def _workflow(session: "ConsoleSession", argument: str) -> None:
    """/workflow create | show | launch | remove."""
    parts = argument.split() if argument else []
    head = parts[0].lower() if parts else ""
    rest = " ".join(parts[1:]).strip()

    if head in ("", "list", "show"):
        _workflow_show(session, rest)
    elif head == "create":
        _workflow_create(session, rest)
    elif head in ("launch", "run", "start"):
        _workflow_launch(session, rest)
    elif head in ("remove", "delete", "rm"):
        _workflow_remove(session, rest)
    else:
        session._print(session.style.dim("  usage: /workflow create|show|launch|remove <name>"))


def _workflow_show(session: "ConsoleSession", name: str) -> None:
    if name:
        found = workflows.get(name)
        if found is None:
            known = workflows.list_workflows()
            if known:
                choice = _c._menu(session, f"Workflow '{workflows.slug(name)}' not found", [Option(value=w["name"], label=w["name"], hint=f"{len(w['steps'])} steps") for w in known])
                if choice:
                    _workflow_show(session, choice)
                    return
            session._print(session.style.ember(f"  no workflow named '{workflows.slug(name)}'"))
            return
        steps = found["steps"]
        session._print(f"  {session.style.bold(found['name'])}")
        for index, step in enumerate(steps, 1):
            session._print(f"    {index}. {step}")
        session._print(session.style.dim(f"  /workflow launch {found['name']}"))
        return

    known = workflows.list_workflows()
    if not known:
        session._print(session.style.dim("  no workflows yet"))
        session._print(session.style.dim("  /workflow create <name>, then type one step per line"))
        return
    choice = _c._menu(session, "Show workflow", [Option(value=w["name"], label=w["name"], hint=f"{len(w['steps'])} steps") for w in known])
    if choice:
        _workflow_show(session, choice)
        return
    session._print(session.style.bold("  workflows"))
    for item in known:
        count = len(item["steps"])
        label = f"{count} step" if count == 1 else f"{count} steps"
        session._print(f"  {item['name']}  {session.style.dim(label)}")
    session._print("")
    session._print(session.style.dim("  /workflow show <name> · /workflow launch <name>"))


def _workflow_create(session: "ConsoleSession", name: str) -> None:
    if not name:
        session._print(session.style.dim("  usage: /workflow create <name>"))
        return
    session._print(
        session.style.dim(f"  steps for '{workflows.slug(name)}', one per line, . to finish:")
    )
    raw = _c._read_multiline(session)
    steps = [line.strip() for line in raw.split("\n") if line.strip()]
    ok, message = workflows.create(name, steps)
    colour = session.style.dim if ok else session.style.ember
    session._print(f"  {colour(message)}")


def _workflow_launch(session: "ConsoleSession", name: str) -> None:
    if not name:
        known = workflows.list_workflows()
        if not known:
            session._print(session.style.dim("  no workflows to launch"))
            session._print(session.style.dim("  usage: /workflow launch <name>"))
            return
        choice = _c._menu(session, "Launch workflow", [Option(value=w["name"], label=w["name"], hint=f"{len(w['steps'])} steps") for w in known])
        if not choice:
            session._print(session.style.dim("  usage: /workflow launch <name>"))
            return
        name = choice
    found = workflows.get(name)
    if found is None:
        known = workflows.list_workflows()
        if known:
            choice = _c._menu(session, f"Workflow '{workflows.slug(name)}' not found", [Option(value=w["name"], label=w["name"], hint=f"{len(w['steps'])} steps") for w in known])
            if choice:
                _workflow_launch(session, choice)
                return
        session._print(session.style.ember(f"  no workflow named '{workflows.slug(name)}'"))
        return
    steps = found["steps"]
    label = f"{len(steps)} step" if len(steps) == 1 else f"{len(steps)} steps"
    session._print(session.style.bold(f"  launching '{found['name']}' ({label})"))
    for index, step in enumerate(steps, 1):
        session._print("")
        session._print(session.style.dim(f"  step {index} of {len(steps)}: {step}"))
        try:
            result = session.handle(step)
        except KeyboardInterrupt:
            session._print(session.style.warn("  workflow stopped"))
            return
        if result is None:
            session._print(session.style.warn("  workflow stopped: the step did not complete"))
            return
    session._print("")
    session._print(session.style.dim(f"  workflow '{found['name']}' finished"))


def _workflow_remove(session: "ConsoleSession", name: str) -> None:
    if not name:
        known = workflows.list_workflows()
        if not known:
            session._print(session.style.dim("  no workflows to remove"))
            return
        choice = _c._menu(session, "Remove workflow", [Option(value=w["name"], label=w["name"], hint=f"{len(w['steps'])} steps") for w in known])
        if not choice:
            session._print(session.style.dim("  usage: /workflow remove <name>"))
            return
        name = choice
    if workflows.delete(name):
        session._print(session.style.dim(f"  removed '{workflows.slug(name)}'"))
    else:
        known = workflows.list_workflows()
        if known:
            choice = _c._menu(session, f"Workflow '{workflows.slug(name)}' not found", [Option(value=w["name"], label=w["name"], hint=f"{len(w['steps'])} steps") for w in known])
            if choice:
                _workflow_remove(session, choice)
                return
        session._print(session.style.ember(f"  no workflow named '{workflows.slug(name)}'"))


def _memory(session: "ConsoleSession") -> None:
    """/memory: show the standing memory file, capped for display."""
    mem = session.memory_path
    session._print(f"memory file: {mem}")
    if not os.path.isfile(mem):
        session._print("(empty)")
    else:
        try:
            # Cap the memory display even though the file itself is capped.
            if os.path.getsize(mem) > 20000:
                with open(mem, encoding="utf-8", errors="replace") as h:
                    data = h.read(8000) + "\n... [truncated]"
            else:
                with open(mem, encoding="utf-8", errors="replace") as h:
                    data = h.read()
            session._print(data or "(empty)")
        except OSError as exc:
            session._print(session.style.ember(f"  cannot read memory: {exc}"))


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


def _fix(session: "ConsoleSession", argument: str) -> None:
    """/fix: send the last failure back to the agent for a diagnosis."""
    # Send the most recent failed command/tool result to the agent
    # for a diagnosis and a suggested (never auto-run) fix.
    prompt = session._fix_prompt(argument.strip())
    if prompt is None:
        session._print(session.style.dim("  no recent failure to fix"))
    else:
        session.handle(prompt)


def _reasoning(session: "ConsoleSession", argument: str) -> None:
    """/reasoning and /effort: aliases that land on the model menu."""
    # Reasoning is a property of the model, so this is now the model
    # menu. Kept as an alias so muscle memory still lands somewhere.
    if argument:
        session.set_reasoning(argument)
    elif not _c._choose_model(session):
        session.show_reasoning()


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
            session._print(f"approval mode is now {chosen}")
            if session.layout is not None and session.layout.active:
                session.layout.draw_chrome()
        else:
            session._print(f"approval mode: {session.approvals.mode}  (choose: {'/'.join(MODES)})")
    elif argument in MODES:
        session.approvals.mode = argument
        session.approvals.reset_session()
        session._print(f"approval mode is now {argument}")
        if session.layout is not None and session.layout.active:
            session.layout.draw_chrome()
    else:
        session._print(f"unknown mode '{argument}'; choose one of {'/'.join(MODES)}")


def _cost(session: "ConsoleSession", argument: str) -> None:
    """/cost: spend so far, in plain, compact, or JSON form."""
    arg = argument.strip().lower()
    session.show_cost(
        compact=arg in ("compact", "brief", "c", "--compact"),
        as_json=arg in ("json", "j", "--json"),
    )


def _compact(session: "ConsoleSession") -> None:
    """/compact: shrink the conversation context."""
    if not session.compact():
        session._print("nothing to compact")


def _clear(session: "ConsoleSession") -> None:
    """/clear (and the hidden /reset alias): start a fresh conversation."""
    # /reset is a hidden alias: clearing the conversation is one
    # action, and advertising two names for it is how they drift.
    session.message_count = 0
    session.context.replace_body([])
    session.approvals.reset_session()
    session.reported_changes.clear()
    session.goal = ""
    session.goal_notes = []
    session.todos = []
    session.active_skills = []
    session.auto_attached = []
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


def _export(session: "ConsoleSession", argument: str) -> None:
    """/export [path]: write the conversation to a Markdown or JSON file.

    Format follows the extension: .json keeps roles and raw content
    blocks; anything else (default: exported-<stamp>.md in the
    workspace) is a readable Markdown transcript. Paths go through the
    same allow-list as session saves, so the export cannot land outside
    workspace, .mantra, the sessions dir, temp, or home/.mantra.
    """
    import json as jsonlib

    argument = argument.strip().strip('"\'')
    path = argument or os.path.join(
        session.workspace, f"exported-{time.strftime('%Y%m%d-%H%M%S')}.md"
    )
    if not _c._is_safe_session_path(path, session.workspace):
        session._print(session.style.warn(f"  refusing to export outside allowed dirs: {path}"))
        return
    messages = [m for m in session.context.messages if m.get("role") != "system"]
    if not messages:
        session._print(session.style.dim("  nothing to export yet"))
        return
    try:
        if path.lower().endswith(".json"):
            payload = {
                "exported_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "workspace": session.workspace,
                "model": session.model_name(),
                "messages": messages,
            }
            with open(path, "w", encoding="utf-8", newline="\n") as handle:
                jsonlib.dump(payload, handle, ensure_ascii=False, indent=2)
            os.chmod(path, 0o600)
            session._print(session.style.dim(f"  exported {len(messages)} messages to {path}"))
            return
        lines = [
            f"# MANTRA conversation export",
            "",
            f"- Exported: {time.strftime('%Y-%m-%d %H:%M:%S')}",
            f"- Workspace: `{session.workspace}`",
            f"- Model: {session.model_name()}",
            f"- Turns: {len(messages)}",
            "",
            "---",
            "",
        ]
        for m in messages:
            who = "You" if m.get("role") == "user" else "Agent"
            content = m.get("content", "")
            if not isinstance(content, str):
                # Multimodal block lists: keep the text parts, mark the rest.
                parts = []
                for block in content:
                    text = block.get("text") if isinstance(block, dict) else None
                    if text:
                        parts.append(str(text))
                    else:
                        parts.append(f"[{block.get('type', 'attachment') if isinstance(block, dict) else 'attachment'}]")
                content = "\n".join(parts)
            lines.append(f"## {who}")
            lines.append("")
            lines.append(str(content))
            lines.append("")
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("\n".join(lines))
        os.chmod(path, 0o600)
        session._print(session.style.dim(f"  exported {len(messages)} messages to {path}"))
    except (OSError, TypeError, ValueError) as exc:
        session._print(session.style.ember(f"  export failed: {exc}"))


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