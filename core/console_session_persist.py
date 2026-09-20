# Split out of core/console.py; import it from core.console, never from here.

"""Session persistence on the session: explicit save/load, the silent
autosave that keeps a session resumable, resuming, and the /sessions
listing and picker. Also the session-path containment check and the
int-coercing helper the saved totals rely on.

Calls the interactive menu through the console module namespace so the
test suite's seam patches (mock.patch.object(console, "_menu")) stay
effective.
"""

from __future__ import annotations

import json
import os
import tempfile
import time

import core.agent.sessions as sessions
from core.console_common import PROJECT_ROOT, private_write
from core.console_render import _sanitize_output, render_markdown
from core.tools.ledger import _CASE_INSENSITIVE as _CASE_INSENSITIVE_FS
from core.tui.overlays import Option

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # Type-only: console.py imports this module back, so a runtime import
    # here would be circular. ruff F821 is otherwise raised on every
    # forward reference in signatures.
    from core.console import ConsoleSession


def _safe_int(value: object) -> int:
    """Total from a session file as an int; corrupt values read as zero.

    Session files are hand-editable JSON, so one non-numeric total must
    cost that counter, not the whole restore command.
    """
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def _is_safe_session_path(path: str, workspace: str) -> bool:
    """Check if a session file path is inside allowed directories."""
    try:
        real = os.path.realpath(os.path.abspath(path))
        # Allow workspace and its .mantra, sessions dir, temp dir, and home/.mantra
        allowed = [
            os.path.realpath(workspace),
            os.path.realpath(os.path.join(workspace, ".mantra")),
            os.path.realpath(sessions.sessions_dir()),
            os.path.realpath(tempfile.gettempdir()),
            os.path.realpath(os.path.expanduser("~/.mantra")),
        ]
        # Also allow current project workspace default
        try:
            allowed.append(os.path.realpath(os.path.join(PROJECT_ROOT, "workspace")))
        except OSError:
            pass  # PROJECT_ROOT removed under us; the other allowdirs still apply
        # Case-fold only where the filesystem is case-insensitive; on a
        # case-sensitive volume a differently-cased sibling must not slip
        # through the containment check (realpath does not normalize case).
        # The check is a probe, not a platform name: a case-insensitive
        # filesystem on another platform folds too.
        if _CASE_INSENSITIVE_FS:
            real_cmp = real.lower()
            for base in allowed:
                base = base.rstrip(os.sep)
                base_cmp = base.lower()
                if real_cmp == base_cmp or real_cmp.startswith(base_cmp + os.sep.lower()):
                    return True
        else:
            for base in allowed:
                base = base.rstrip(os.sep)
                if real == base or real.startswith(base + os.sep):
                    return True
        return False
    except OSError:
        return False


class PersistenceMixin:
    """Save/load/resume/autosave and the /sessions surface."""

    def save_session(self: "ConsoleSession", path: str) -> bool:
        """Persist the session to *path*, validated inside the allowed directories. Returns True on success."""
        # Validate path is inside allowed directories to prevent arbitrary write
        if not _is_safe_session_path(path, self.workspace):
            self._print(self.style.warn(f"  refusing to save outside allowed dirs: {path}"))
            self._print(self.style.dim(f"  allowed: workspace, {sessions.sessions_dir()}, temp"))
            return False
        # Enforce size cap on file path length and payload
        if len(path) > 500:
            self._print(self.style.ember("  save failed: path too long"))
            return False
        payload = {
            "version": 1,
            "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "workspace": self.workspace,
            "model": self.config.get("llm", {}).get("model", "?"),
            "totals": self.totals,
            "messages": self.context.messages,
            "show_tool_output": self._show_tool_output,
            "pending_pages": self._pending_pages_snapshot(),
        }
        # Cap file size via payload size check. A failure here (e.g. the
        # payload contains something json can't serialize) means the write
        # below will fail the same way, so surface it now instead of
        # silently skipping the check and hitting an uncaught error later.
        try:
            data = json.dumps(payload, ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            self._print(self.style.ember(f"  save failed: {exc}"))
            return False
        if len(data) > 10_000_000:
            self._print(self.style.ember("  save failed: session too large"))
            return False
        try:
            # Parent dirs only need to exist; the session file itself
            # gets 0o600 below, which is where the privacy boundary sits.
            parent = os.path.dirname(os.path.abspath(path))
            if parent:
                os.makedirs(parent, exist_ok=True)
            # Created owner-only: the session holds the whole conversation.
            with private_write(path) as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
        except (OSError, TypeError, ValueError) as exc:
            self._print(self.style.ember(f"  save failed: {exc}"))
            return False
        self._print(self.style.dim(f"  saved {len(self.context.messages)} messages to {path}"))
        return True

    def load_session(self: "ConsoleSession", path: str) -> bool:
        """Restore a session from *path* (size-capped, allow-listed). Returns True on success."""
        if not _is_safe_session_path(path, self.workspace):
            self._print(self.style.warn(f"  refusing to load outside allowed dirs: {path}"))
            return False
        try:
            # Size check before load
            try:
                if os.path.getsize(path) > 10_000_000:
                    self._print(self.style.ember("  load failed: file too large"))
                    return False
            except OSError:
                pass
            with open(path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            self._print(self.style.ember(f"  load failed: {exc}"))
            return False
        if not isinstance(payload, dict):
            # A top-level list/string would crash the payload.get below.
            self._print(self.style.ember("  load failed: session file must contain a JSON object"))
            return False
        messages = payload.get("messages")
        if not isinstance(messages, list) or not messages:
            self._print(self.style.ember("  load failed: no messages in file"))
            return False
        if len(messages) > 500:
            # Cap restored sessions so a huge transcript cannot blow the budget.
            self._print(self.style.warn(f"  warning: large session {len(messages)} messages, truncating"))
            messages = messages[-500:]
        # A hand-edited session file can carry non-dict elements; the
        # budget pass calls message.get() on each entry, so filter to
        # dicts first (the replay loop applies the same guard).
        self.context.messages = [m for m in messages if isinstance(m, dict)]
        self.context.enforce_budget()
        totals = payload.get("totals")
        if isinstance(totals, dict):
            self.totals.update({k: _safe_int(v) for k, v in totals.items() if k in self.totals})
        output_pref = payload.get("show_tool_output")
        if isinstance(output_pref, bool):
            self._show_tool_output = output_pref
        remaining_pages = self._restore_pending_pages(payload)
        self._print(
            self.style.dim(f"  restored {len(messages)} messages (~{self.context.tokens} tokens)")
        )
        if remaining_pages:
            self._print(
                self.style.dim(
                    "  (capped outputs from the saved run remain — press Enter with an empty prompt to page through them)"
                )
            )
        return True

    # ---- resumable sessions ---------------------------------------------

    def autosave(self: "ConsoleSession") -> None:
        """Keep the session resumable without being asked.

        Silent by design: the only time the operator learns the file
        exists is when /sessions lists it. Nothing is written until there
        is a real conversation.
        """
        if len(self.context.messages) < 2:
            return
        if not self.session_name:
            self.session_name = sessions.derive_name(self.workspace, self.model_name())
        saved = sessions.save(
            self.session_name,
            {
                "workspace": self.workspace,
                "model": self.model_name(),
                "summary": sessions._summarise(self.context.messages),
                "totals": self.totals,
                "goal": self.goal,
                "goal_notes": self.goal_notes,
                "todos": self.todos,
                "messages": self.context.messages,
                "show_tool_output": self._show_tool_output,
                "pending_pages": self._pending_pages_snapshot(),
            },
        )
        if saved is None and not self._autosave_warned:
            # A lock-contention or write failure leaves no session file;
            # say so once rather than silently believing the save landed.
            self._autosave_warned = True
            self._print(self.style.dim("  (autosave failed - session not written to disk)"))

    def model_name(self: "ConsoleSession") -> str:
        return str(self.config.get("llm", {}).get("model", "") or "")

    def resume_session(self: "ConsoleSession", name: str) -> bool:
        """Restore a saved session by name."""
        data = sessions.load(name)
        if data is None:
            known = sessions.list_sessions()
            if not known:
                self._print(self.style.dim("  no saved sessions yet"))
                return False
            self._print(self.style.ember(f"  no session named '{name}'"))
            self._print(self.style.dim("  /sessions lists them"))
            return False
        # Workspace guard: a session saved in one workspace is not
        # resumed in another.
        saved_ws = data.get("workspace") or ""
        if saved_ws and not self._is_same_workspace(saved_ws):
            self._print(self.style.warn(f"  session '{name}' belongs to workspace {saved_ws}"))
            self._print(self.style.dim(f"  current workspace is {self.workspace} — switch workspace or use /sessions list to see this workspace's sessions"))
            return False
        messages = data.get("messages")
        if not isinstance(messages, list) or not messages:
            self._print(self.style.ember(f"  session '{name}' has no conversation"))
            return False
        # Filter to dict entries before the budget pass: a hand-edited
        # session file can carry non-dict elements that would crash the
        # message.get() calls inside enforce_budget().
        self.context.messages = [m for m in messages if isinstance(m, dict)]
        self.context.enforce_budget()
        totals = data.get("totals")
        if isinstance(totals, dict):
            self.totals.update({k: _safe_int(v) for k, v in totals.items() if k in self.totals})
        self.message_count = sum(
            1 for m in messages if isinstance(m, dict) and m.get("role") == "user"
        )
        # The goal travels with the conversation: resuming a session to
        # finish something and finding the objective gone defeats the
        # point of resuming it.
        self.goal = str(data.get("goal") or "")
        notes = data.get("goal_notes")
        self.goal_notes = [str(n) for n in notes] if isinstance(notes, list) else []
        # The todo checklist travels with the conversation too: resuming
        # a session to finish a multi-part task and finding its list
        # wiped would scatter the remaining work.
        saved_todos = data.get("todos")
        self.todos = []
        if isinstance(saved_todos, list):
            for entry in saved_todos:
                if isinstance(entry, dict) and "text" in entry:
                    self.todos.append({"text": str(entry["text"]), "done": bool(entry.get("done"))})
        # Adopt the name, so the next autosave continues this session
        # rather than starting a second file beside it.
        self.session_name = name
        # The operator's display preference travels with the session, so
        # resuming one where the tool-output boxes were hidden stays that
        # way (Ctrl+O flips it back live).
        output_pref = data.get("show_tool_output")
        if isinstance(output_pref, bool):
            self._show_tool_output = output_pref
        # An interrupted paging session resumes where it left off: any
        # capped tool output not yet paged through comes back with the
        # conversation, ready for the empty-Enter pager.
        remaining_pages = self._restore_pending_pages(data)
        # Hide splash so full history is visible immediately — splash otherwise
        # covers viewport until next resize/handle hides it.
        if self.layout is not None and getattr(self.layout, "_splash_visible", False):
            try:
                self.layout.hide_splash()
            except Exception:
                pass  # duck-typed bridge: splash removal is cosmetic
            self._splash_visible = False
        if not self._show_tool_output:
            self._print(self.style.dim("  (tool output boxes are hidden in this session — ctrl+o to show)"))
        self._print(
            self.style.dim(
                f"  resumed '{name}' - {len(messages)} messages "
                f"(~{self.context.tokens} tokens)"
            )
        )
        if remaining_pages:
            self._print(
                self.style.dim(
                    "  (capped outputs from the interrupted run remain — press Enter with an empty prompt to page through them)"
                )
            )
        # Replay conversation history in the viewport.
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            role = msg.get("role", "")
            content = msg.get("content", "")
            # Handle multimodal/list content
            if isinstance(content, list):
                try:
                    content = " ".join(part.get("text", "") for part in content if isinstance(part, dict) and part.get("type") == "text") or str(content)
                except (TypeError, AttributeError):
                    content = str(content)  # unexpected part shape: stringify
            if role == "user":
                text = content if isinstance(content, str) else str(content or "")
                if text.strip():
                    self._print(f"{self.style.ash('you')} {_sanitize_output(text)}")
                else:
                    self._print(f"{self.style.ash('you')} (empty)")
            elif role == "assistant":
                # Assistant may have null content + tool_calls — render body as markdown.
                # Model-controlled text is sanitized exactly like the live
                # reply paths: saved output must not drive the terminal.
                if isinstance(content, str) and content.strip():
                    self._print(f"{self.style.brand('ENCHANTER')}")
                    self._print(render_markdown(_sanitize_output(content), self.style))
                elif msg.get("tool_calls"):
                    calls = ", ".join((c.get("function") or {}).get("name", "?") for c in msg.get("tool_calls") or [])
                    self._print(f"{self.style.brand('ENCHANTER')} [called {calls}]")
                    if isinstance(content, str) and content.strip():
                        self._print(render_markdown(_sanitize_output(content), self.style))
                elif isinstance(content, str):
                    self._print(f"{self.style.brand('ENCHANTER')} {_sanitize_output(content)}")
            elif role == "tool":
                text = content if isinstance(content, str) else str(content or "")
                if text.strip():
                    # Keep the replay compact: 300 chars per tool result.
                    self._print(f"{self.style.dim('tool')} {self.style.dim(_sanitize_output(text)[:300])}")
                else:
                    self._print(f"{self.style.dim('tool')} (no output)")
        summary = data.get("summary") or ""
        if summary:
            self._print(self.style.dim(f"  started with: {summary}"))
        # Flush viewport so history appears immediately — without this the
        # throttled writes in the terminal application stay buffered until flush.
        if self.layout is not None and getattr(self.layout, "active", False):
            try:
                self.layout.flush()
            except Exception:
                pass  # duck-typed bridge: flush is cosmetic, never fatal
        return True

    def _is_same_workspace(self: "ConsoleSession", saved_ws: str) -> bool:
        try:
            cur = os.path.realpath(os.path.abspath(self.workspace or ""))
            saved = os.path.realpath(os.path.abspath(saved_ws or ""))
            if os.name == "nt":
                return cur.lower() == saved.lower()
            return cur == saved
        except (OSError, ValueError):
            # Unresolvable paths (deleted cwd, bad chars): compare raw.
            return (saved_ws or "") == (self.workspace or "")

    def show_sessions(self: "ConsoleSession") -> None:
        """List what can be resumed — filtered to current workspace."""
        all_known = sessions.list_sessions()
        known = [item for item in all_known if self._is_same_workspace(item.get("workspace") or "")]
        if not known:
            if all_known:
                self._print(self.style.dim(f"  no saved sessions for this workspace ({self.workspace})"))
                self._print(self.style.dim(f"  {len(all_known)} session(s) exist for other workspaces — switch workspace to see them"))
            else:
                self._print(self.style.dim("  no saved sessions yet - they are saved as you go"))
            return
        self._print(self.style.bold(f"  saved sessions for {self.workspace}"))
        for item in known:
            when = item["saved_at"] or "unknown time"
            turns = item["turns"]
            label = f"{turns} turn" if turns == 1 else f"{turns} turns"
            head = f"  {item['name']}"
            if item["name"] == self.session_name:
                head += self.style.dim(" (current)")
            self._print(head)
            extra = ""
            if item.get("show_tool_output") is False:
                extra = " · output boxes hidden"
            self._print(self.style.dim(f"      {when} · {label} · {item['model'] or '?'}{extra}"))
            if item["summary"]:
                self._print(self.style.dim(f"      {item['summary']}"))
        self._print("")
        self._print(self.style.dim("  /sessions <name> to pick one up"))

    def pick_session(self: "ConsoleSession") -> bool:
        """Resume from a menu — filtered to current workspace. False when nothing was chosen."""
        all_known = sessions.list_sessions()
        known = [item for item in all_known if self._is_same_workspace(item.get("workspace") or "")]
        if not known:
            if all_known:
                self._print(self.style.dim(f"  no saved sessions for this workspace ({self.workspace})"))
            else:
                self._print(self.style.dim("  no saved sessions yet - they are saved as you go"))
            return False
        options = []
        for item in known:
            summary = item["summary"] or "no summary"
            detail = f"{item['saved_at']} · {item['turns']} turns · {summary}"
            if item.get("show_tool_output") is False:
                detail += " · output boxes hidden"
            options.append(Option(item["name"], item["name"], detail))
        # Menu seam: resolved through the console namespace at call time,
        # so mock.patch.object(console, "_menu") keeps steering this call.
        from core import console as _c

        chosen = _c._menu(
            self,
            "Resume a session",
            options,
            hint="up/down move · Enter resume · Esc cancel",
        )
        if not chosen:
            return False
        return self.resume_session(chosen)
