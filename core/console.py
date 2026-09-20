"""Interactive console: session, commands, streaming, terminal UI.

The module is a facade: presentation lives in console_render, shared
primitives in console_common, the completer in console_completer, and
the command surfaces in console_skills/console_commands/console_model.
Every public name is re-exported here, so callers (and the test suite's
seam patches) keep working unchanged.
"""

from __future__ import annotations

import argparse
import os
import re
import signal
import subprocess
import sys
import threading
import time
from typing import Any

from core import theme
from core.config import REASONING_EFFORTS, load_config
from core.agent.loop import DEFAULT_SYSTEM_PROMPT, AgentLoop, RunResult
from core.agent.approvals import MODES, ApprovalPolicy
from core.agent.context import ContextManager
from core.agent.events import EventBus
from core.agent.exceptions import AbortError, ConfigError, HarnessError
from core.agent.keys import has_stored, mask, store as store_key, stored_keys  # noqa: F401
from core.agent.models import fetch_models, is_reasoning_model  # noqa: F401 - seam patches target console.fetch_models
import core.agent.skills as skills
import core.agent.workflows as workflows  # noqa: F401
from core.agent.settings import (
    active as get_active,
    add_endpoint,  # noqa: F401
    endpoint_name_for_url,  # noqa: F401
    endpoints as known_endpoints,
    models_for,  # noqa: F401
    remove_endpoint,  # noqa: F401
    set_active,  # noqa: F401
    set_models,  # noqa: F401
    set_skills_prefs,  # noqa: F401
    settings_path,  # noqa: F401
    skills_prefs,
    validate_endpoint,  # noqa: F401
)
from core.agent.knowledge import (
    append_memory,  # noqa: F401
    plan_memory_write,  # noqa: F401
    read_raw_tail,  # noqa: F401
    relevant_memory,
    rewrite_memory,  # noqa: F401
    assemble_system_prompt,
    find_instructions_file,
    render_environment,
)
from core.evaluators import NullEvaluator
from core.logs import JsonlLogger
from core.sandbox import LocalSandbox
from core.tui.overlays import Option  # noqa: F401
from core.tui.composer import Completion  # noqa: F401
from core.mcp import build_mcp_tools
from core.registry import build_llm, build_tools

from core.term import (
    enable_vt,  # noqa: F401
    force_utf8_output,
    safe_write,  # noqa: F401
    selection_in_progress,
    term_size,  # noqa: F401
)  # shared wide-aware impl

# Presentation layer (re-exported for compatibility).
from core.console_render import (  # noqa: F401
    Style,
    StreamingRenderer,
    _ANSI_SANITIZE_RE,
    _flush_table_lines,
    _inline_md,
    _is_table_row,
    _render_md_line,
    _render_table,
    _safe_stdout,
    _sanitize_output,
    _strip_inline_html,
    _syntax_highlight,
    _table_cells,
    _truncate_codepoint,
    render_markdown,
)

# Shared primitives (re-exported for compatibility).
from core.console_common import (  # noqa: F401
    HELP_TEXT,
    KEYLESS_HOSTS,
    MAX_INDEX_ENTRIES,
    MENTION_RE,
    PROJECT_ROOT,
    SLASH_COMMANDS,
    _MENTION_TRIM,
    _SKIP_DIRS,
    _apply_endpoint_override,
    _ask_secret,
    _derive_key_env,
    _derive_name,
    _menu,
    _needs_first_run,
    _read_choice,
    _read_multiline,
    _read_one,
    _read_secret,
    _short,
    _strip_leading_invisible,
    private_write,
    provider_needs_key,
)

# Completer (no seam patching; plain import).
from core.console_completer import ConsoleCompleter  # noqa: F401

# Session state split across focused mixins: mentions, goals/todos,
# usage/compaction, persistence, workspace inspection, endpoints. The
# class below composes them; every method stays reachable as before.
from core.console_boxes import BoxRenderingMixin  # noqa: F401
from core.console_session_mentions import MentionsMixin  # noqa: F401
from core.console_session_goals import GoalsTodosMixin  # noqa: F401
from core.console_session_usage import UsageMixin  # noqa: F401
from core.console_session_workspace import WorkspaceMixin  # noqa: F401
from core.console_session_persist import PersistenceMixin  # noqa: F401
from core.console_session_endpoints import EndpointsMixin  # noqa: F401
# Module-level helpers and constants that moved with their mixin; kept
# importable from here so callers and the test suite never notice.
from core.console_session_mentions import (  # noqa: F401
    MAX_ATTACH_CHARS,
    MAX_GLOB_HITS,
    MAX_LISTING_ENTRIES,
    MAX_TOTAL_ATTACH_CHARS,
)
from core.console_session_usage import _format_elapsed, _transcript  # noqa: F401
from core.console_session_persist import (  # noqa: F401
    _is_safe_session_path,
    _safe_int,
)


def _resolve_data_path(*parts: str) -> str:
    """Locate a bundled data file in a source tree or an installed wheel.

    Installed wheels place data-files under ``sys.prefix`` while the code
    sits in site-packages, so a single PROJECT_ROOT-relative path would
    miss them (installed console could not start with its default config).
    """
    candidates = [
        os.path.join(PROJECT_ROOT, *parts),
        os.path.join(os.path.dirname(PROJECT_ROOT), *parts),
        os.path.join(sys.prefix, *parts),
    ]
    for cand in candidates:
        if os.path.isfile(cand):
            return cand
    return candidates[0]


KNOWN_FAILURES_PATH = _resolve_data_path("knowledge", "known-failures.md")


SYSTEM_PROMPT_CAP = 20_000


# ------------------------------------------------------------------- session
class ConsoleSession(
    MentionsMixin,
    GoalsTodosMixin,
    UsageMixin,
    WorkspaceMixin,
    BoxRenderingMixin,
    PersistenceMixin,
    EndpointsMixin,
):
    """One REPL session over one persistent local workspace."""

    def __init__(
        self,
        config: dict,
        workspace: str,
        style: Style,
        llm: Any = None,
        ask: Any = None,
        config_path: str | None = None,
    ) -> None:
        self.config = config
        self.style = style
        self.workspace = workspace
        # Where the config was loaded from, so commands that change the
        # configuration (/mcp enable|disable) can persist it. None for
        # programmatically built sessions: those then stay in-memory only.
        self.config_path = config_path
        self.sandbox = LocalSandbox(workspace)
        self.sandbox.setup({})
        self._isolate_git(workspace)
        self.memory_path = os.path.join(workspace, ".mantra", "memory.md")
        self.instructions_path = find_instructions_file(workspace)

        ctx_cfg = config.get("context") or {}
        self.context = ContextManager(
            max_messages=int(ctx_cfg.get("max_messages", 200)),
            max_chars=int(ctx_cfg.get("max_chars", 240_000)),
        )
        env = render_environment(workspace)
        # Preload workspace map so model is not blind before user asks
        try:
            entries = os.listdir(workspace)[:50]
            files = []
            for e in sorted(entries)[:35]:
                p = os.path.join(workspace, e)
                files.append(f"{e}/" if os.path.isdir(p) else e)
            env += f"\n- workspace files: {', '.join(files) or '(empty)'}"
            for hint in ("README.md", "package.json", "pyproject.toml", "AGENTS.md", "main.py", "index.html"):
                if hint in entries:
                    env += f"\n- has {hint}"
            # Preload the README head so the model can describe the
            # project without a tool call.
            for readme in ("README.md", "readme.md"):
                rp = os.path.join(workspace, readme)
                if os.path.isfile(rp):
                    try:
                        with open(rp, "r", encoding="utf-8", errors="replace") as f:
                            head = f.read(2500).strip().replace("\r", "")
                        if head:
                            # Send the first 400 characters of the README.
                            snippet = head[:400].replace("\n", " ")
                            env += f"\n- README: {snippet}"
                        break
                    except OSError:
                        pass  # unreadable candidate readme; try the next
        except Exception:
            pass  # environment probing is advisory; the prompt still assembles
        self.system_prompt = assemble_system_prompt(
            config.get("system_prompt") or DEFAULT_SYSTEM_PROMPT,
            known_failures_path=KNOWN_FAILURES_PATH,
            memory_path=self.memory_path,
            instructions_path=self.instructions_path,
            environment=env,
        )
        self.tools = build_tools(config["tools"])
        # External MCP tools join the same list, so the loop, the approval
        # policy, and the schema sent to the model need no special case. A
        # server that fails to start is reported and skipped rather than
        # blocking the console.
        self.mcp_clients: list[Any] = []
        mcp_tools, self.mcp_clients = build_mcp_tools(
            config.get("mcp"), on_error=lambda message: self._note(f"MCP: {message}")
        )
        self.tools.extend(mcp_tools)
        self.llm = llm if llm is not None else build_llm(config["llm"])
        self.approvals = ApprovalPolicy(
            mode=config.get("approvals", "yolo"),
            ask=ask or self._ask,
            note=self._note,
        )
        self.totals = {"tokens_in": 0, "tokens_out": 0, "turns": 0, "tool_errors": 0, "cache_hit": 0,
                   "observation_saved": 0, "digest_turns": 0, "digest_chars": 0, "digest_failures": 0}
        self.last_reply = ""  # the finished turn's assistant reply (suggestion engine input)
        self.reported_changes: set[str] = set()
        # Model ids discovered from the endpoint, so `/model <tab>` can
        # complete from what is actually served rather than a guess.
        self.known_models: list[str] = []
        # Per-turn cache metrics for trend analysis.
        self.turn_history: list[dict] = []  # [{turn, tokens_in, tokens_out, cache_hit, cache_rate}]

        log_path = config["logging"].get("path", "logs/mantra-console.jsonl")
        if not os.path.isabs(log_path):
            log_path = os.path.join(PROJECT_ROOT, log_path)
        self.logger = JsonlLogger(log_path)
        self.bus = EventBus()
        self.bus.subscribe(self._on_event)

        self.message_count = 0
        # Monotonic turn id, never reset by /clear, so run-log and event
        # identifiers stay unique across the whole process.
        self._task_counter = 0
        self.verbose = bool(config.get("verbose", False))
        self.max_steps = int(config.get("max_steps", 30))
        # Set the first time autosave writes, so a session that never got
        # anywhere leaves no file behind. Adopted by /sessions so picking a
        # session up continues it instead of forking it.
        self.session_name = ""
        self._autosave_warned = False
        # The standing objective, if the operator set one. Injected into
        # every turn's system prompt, so an agent working across many
        # turns keeps aiming at the same thing instead of drifting to
        # whatever the last message asked for.
        self.goal = ""
        # Free-form notes the operator attached to the goal with
        # /goal note <text>: constraints found along the way, decisions
        # made. Shown with the goal so they are not re-litigated.
        self.goal_notes: list[str] = []
        # The session todo checklist (/todo). Discrete items the operator
        # wants done, each kept open or done. Injected into every turn's
        # system prompt like the goal, so an agent working over many
        # turns can see what remains rather than losing the thread of a
        # multi-part request.
        self.todos: list[dict] = []  # [{"text": str, "done": bool}]
        # Reports the agent emitted on the turn in flight, already applied
        # inline as its reply streamed. The end-of-turn pass skips them so
        # nothing is added, checked, or announced twice.
        self._turn_todo_reports: list[tuple[str, str]] = []  # [(kind, normalised_text)]
        # Skills attached with /skills use <name>. Their procedures ride
        # along in the system prompt so the agent follows them rather
        # than improvising, which is the whole point of a skill existing.
        self.active_skills: list[str] = []
        # Skills the router attached on its own for the turn in flight.
        # Detached the moment that turn ends, because routing reads one
        # request - leaving its guess attached would mean every later
        # turn inherits a procedure nobody asked for, and would stop the
        # router ever looking again.
        self.auto_attached: list[str] = []
        self.last_error: str | None = None  # most recent failure, for /fix
        # True while a bundle is running its steps. A bundle step is a
        # turn like any other, but it is one the router must keep its
        # hands off: the step already knows which skill it wants.
        self.in_bundle = False

        # The terminal application (set by main for interactive use);
        # every screen-facing decision delegates to it when present.
        self.ui: Any = None
        self._streamed_this_run = False
        self._stream_header_done = False
        self._turn_started: float = 0.0
        # Live token counter: approximate tokens received during streaming.
        self._stream_tokens: int = 0
        self._last_counter_update: float = 0.0
        # Timestamp of when the last prompt was sent (Enter pressed).
        self._prompt_sent_at: float = 0.0
        # Streaming markdown renderer for inline formatting during token streaming.
        self._stream_renderer = StreamingRenderer(self.style)
        self.layout: Any = None
        self._splash_visible = True
        self._abort = threading.Event()
        self._prev_sigint = None
        # Keys the turn-scoped scroll reader buffered while a task streamed
        # (non-scroll input). The prompt editor drains these before touching
        # the terminal, so typing during a turn is not lost.
        self._scroll_preload: list[str] = []
        # Rendered fragments deferred while the terminal host was performing
        # a native mouse selection (Windows). Flushed once the drag ends so
        # streaming repaints never disturb the selection.
        self._deferred_stream: list[str] = []
        # Pre-edit file snapshots keyed by workspace-relative path, taken
        # at tool_call time so tool_result can render a real before/after
        # diff of what the agent changed. None means the file did not
        # exist yet (a fresh write). Cleared at the end of every turn.
        self._edit_snapshots: dict[str, str | None] = {}
        self._last_edit_path: str | None = None
        # Last run_command text / shell task id, so the observation
        # boxes that follow each step can title themselves.
        self._last_command: str | None = None
        self._last_shell_task: str | None = None
        # Ctrl+O during a run toggles whether tool-output boxes print.
        self._show_tool_output = True
        # Tool output too big for one screenful is queued here (title,
        # remaining lines) by the box renderers; an empty Enter at the
        # prompt pages through it a screenful at a time. Holds reads,
        # commands, diffs and background-task logs alike.
        self._pending_pages: list[tuple[str, list[str]]] = []
        # Parallel to _pending_pages: True when the queued lines are
        # already fully styled (diff panes) instead of raw text that the
        # pager must run through _row().
        self._pending_styled: list[bool] = []
        # Box renderers run on the turn worker thread while the TUI main
        # thread may call page_next(); the queue must not be mutated
        # without this lock.
        self._pager_lock = threading.Lock()

    def prompt_text(self, body: str = "") -> str:
        """Prompt label — fixed at bottom when layout active."""
        if not body:
            if self.style.enabled:
                body = self.style.hair("\u2502 ") + self.style.bone("MANTRA > ")
            else:
                body = "\u2502 MANTRA > "
        if self.layout is not None and self.layout.active:
            return self.layout.prompt_text(body)
        return body

    def refresh_title(self) -> None:
        """Deprecated no-op: the terminal application owns the title."""
        return

    def _isolate_git(self, workspace: str) -> None:
        """Give the workspace its own git repo if it does not have one."""
        if os.path.isdir(os.path.join(workspace, ".git")):
            return
        try:
            subprocess.run(
                ["git", "init"], cwd=workspace,
                capture_output=True, timeout=30, check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass

    # ---- output helpers (spinner-aware) ---------------------------------

    def _print(self, text: str = "") -> None:
        if self.layout is not None and self.layout.active:
            self.layout.write(text + "\n")
            return
        try:
            sys.stdout.write(text + "\n")
        except UnicodeEncodeError:
            sys.stdout.write((text + "\n").encode(sys.stdout.encoding or "utf-8", errors="replace").decode(sys.stdout.encoding or "utf-8", errors="replace"))
        sys.stdout.flush()

    def _set_busy(self, on: bool) -> None:
        """Tell the terminal application a turn is running (spinner state)."""
        if self.ui is not None:
            self.ui.set_busy(on, label="Chanting")

    # Tool-output boxes are budgeted in *viewport rows*, not raw lines: one
    # long line can wrap to several screen rows, and a huge read or log dump
    # must not flood the viewport mid-run. Roughly a screenful is shown and
    # the rest is noted - never dropped silently. Reads that overflow are
    # queued and can be paged through with an empty Enter at the prompt.
    _TOOL_OUTPUT_BUDGET_ROWS = 36
    _READ_PAGE_ROWS = 24
    _OUTPUT_COLS_FALLBACK = 80

    def toggle_tool_output(self) -> None:
        """Flip whether run/read/git boxes print while the agent works."""
        self._show_tool_output = not self._show_tool_output
        state = "on" if self._show_tool_output else "off"
        self._print(self.style.dim(f"  (tool output boxes {state} — ctrl+o to toggle)"))

    def _capture_last_error(self, tool: str, observation: str) -> None:
        """Keep the most recent failed tool/command result for /fix."""
        text = observation.strip()
        if text.startswith("ERROR"):
            self.last_error = f"[{tool}] {text[:2000]}"
            return
        # run_command / shell_output observations begin with an exit_code
        # line; a nonzero code is a failure worth fixing (grep's "no
        # matches" and similar notes are explicitly not errors).
        m = re.match(r"exit_code:\s*(\d+)([^\n]*)", text)
        if m:
            code = int(m.group(1))
            if code != 0 and "not an error" not in m.group(2):
                self.last_error = f"[{tool}] {text[:2000]}"

    def _fix_prompt(self, hint: str = "") -> str | None:
        """The agent prompt for the most recent failure, or None."""
        if not self.last_error:
            return None
        prompt = (
            "A tool or command failed in this workspace. Here is the failure:\n"
            f"---\n{self.last_error}\n---\n"
            "Diagnose the root cause and suggest a fix. Do NOT run any "
            "command yourself - propose the exact command for the operator "
            "to approve and run."
        )
        if hint:
            prompt += f"\nAdditional hint from the operator: {hint}"
        return prompt

    def _attention(self) -> None:
        """A soft terminal bell so a failed turn or a denial is noticed."""
        try:
            sys.stdout.write("\x07")
            sys.stdout.flush()
        except OSError:
            pass

    def _on_tool_observation(self, tool: str, observation: str, step: int) -> None:
        """Show what a tool returned while the agent works.

        Event payloads and the run log deliberately avoid carrying file
        contents or command output, so the agent loop hands display-worthy
        observations straight to the console: the operator sees the command
        output, diffs and file contents the agent sees, not just the tool
        name. Ctrl+O (typed mid-run) hides or restores these boxes.
        """
        # Remember the most recent failure for /fix, before the display
        # filter: read_file errors matter as much as command failures.
        if isinstance(observation, str) and observation.strip():
            self._capture_last_error(tool, observation)
        # Turn-scoped evidence for the TUI's post-task suggestion engine:
        # it names the tools that ran and the artifacts they touched, so
        # the next-step rows follow the work that actually happened.
        ui = getattr(self, "ui", None)
        if ui is not None and isinstance(observation, str) and observation.strip():
            collector = getattr(ui, "_turn_tool_text", None)
            if isinstance(collector, list):
                collector.append(f"{tool}: {observation.strip()[:600]}")
        if not self._show_tool_output:
            return
        if not isinstance(observation, str) or not observation.strip():
            return
        try:
            if tool in ("run_command", "git_diff", "git_reset"):
                kind = {"run_command": "run_command", "git_diff": "git diff", "git_reset": "git reset"}[tool]
                shown = self._render_command_result(observation, kind=kind)
            elif tool == "shell_output":
                shown = self._render_shell_output(observation)
            else:
                # read_file observations are deliberately not boxed: the
                # STEP line names the file, and dumping whole contents
                # floods the viewport.
                return
        except Exception:
            # Rendering an observation box is cosmetic: any failure means
            # the raw observation is shown by the caller instead.
            return
        if shown:
            self._print(shown)

    def _render_command_result(self, observation: str, kind: str = "run_command") -> str:
        """Boxed transcript view of a run_command / git_diff observation."""
        raw = _sanitize_output(observation).replace("\r", "").splitlines()
        lines = [ln for ln in raw if ln.strip() not in ("<<<UNTRUSTED_TASK_OUTPUT", ">>>")]
        if not lines:
            return ""
        # Exit line and any notes ride above the first output section.
        split = len(lines)
        for i, ln in enumerate(lines):
            if ln.startswith(("stdout:", "stderr:", "log:")):
                split = i
                break
        header = lines[:split]
        body = lines[split:]
        if split == len(lines):
            # No stdout:/stderr: sections: the whole observation is
            # content (e.g. a raw git diff). Color it like a diff.
            header = []
            body = lines
        # The command text is model-controlled, so the title must not echo
        # raw escape sequences back into the terminal.
        command = _sanitize_output(
            (getattr(self, "_last_command", None) or "").replace("\r", "").replace("\n", " ")
        )
        if kind == "run_command" and command:
            title = f"$ {command}"
        else:
            title = kind
        # git diff / git reset observations that are real unified diffs
        # render as old/new panes; anything else keeps the generic box.
        if kind in ("git diff", "git reset") and body:
            pane = self._diff_pane_rows("\n".join(body), max_lines=4000)
            if pane is not None:
                return self._render_diff_pages(title, pane, lead=header or None)
        return self._render_paged_box(title, body, lead=header or None)

    def _render_shell_output(self, observation: str) -> str:
        """Boxed view of a background-task log read (shell_output)."""
        raw = _sanitize_output(observation).replace("\r", "").splitlines()
        lines = [ln for ln in raw if ln.strip() not in ("<<<UNTRUSTED_TASK_OUTPUT", ">>>")]
        if not lines:
            return ""
        task = _sanitize_output(getattr(self, "_last_shell_task", None) or "")
        title = f"shell_output {task}" if task else "shell_output"
        return self._render_paged_box(title, lines)

    def _pending_pages_snapshot(self) -> list[list]:
        """JSON-safe copy of the pager queue (title + remaining lines).

        Persisted so an interrupted paging session can resume where it
        left off. Content is raw observation text or pre-styled diff
        rows - either way it is the same data already carried in the
        session's tool messages, so no new content is written to disk.
        """
        with self._pager_lock:
            return [[title, list(lines)] for title, lines in self._pending_pages]

    def _restore_pending_pages(self, data: dict) -> int:
        """Rebuild the pager queue from a session payload.

        Returns the number of boxes restored so the caller can surface a
        hint. Garbage entries are dropped defensively; old session files
        without the field simply leave the queue empty. Pre-styled rows
        (diff panes, detected by their ANSI escapes) restore as styled so
        the pager prints them verbatim instead of re-colouring them.
        """
        raw = data.get("pending_pages")
        restored: list[tuple[str, list[str]]] = []
        styled_flags: list[bool] = []
        if isinstance(raw, list):
            for entry in raw[:100]:
                if not isinstance(entry, (list, tuple)) or len(entry) != 2:
                    continue
                title, lines = entry
                if not isinstance(title, str) or not isinstance(lines, list):
                    continue
                content = [ln for ln in lines if isinstance(ln, str)]
                if content:
                    restored.append((title, content))
                    styled_flags.append(any(ln.startswith("\x1b") for ln in content))
        with self._pager_lock:
            self._pending_pages = restored
            self._pending_styled = styled_flags
        return len(restored)

    def page_next(self) -> bool:
        """Print the next screenful of a queued capped box.

        Called by the REPL when Enter is pressed with an empty prompt. The
        queue holds any tool output - reads, commands, diffs, background
        logs - that overflowed a screenful during the run. Returns True
        while more pages remain.
        """
        with self._pager_lock:
            return self._page_next_locked()

    def _page_next_locked(self) -> bool:
        if not self._pending_pages:
            return False
        title, content = self._pending_pages[0]
        styled = bool(self._pending_styled[0]) if self._pending_styled else False
        if not content:
            self._pending_pages.pop(0)
            if self._pending_styled:
                self._pending_styled.pop(0)
            if not self._pending_pages:
                self._print(self.style.dim(f"  (end of {title})"))
            return bool(self._pending_pages)
        chunk, _ = self._head_lines(content, self._READ_PAGE_ROWS)
        del content[: len(chunk)]
        # Diff panes are queued fully styled; raw boxes run through _row.
        rows = list(chunk) if styled else [self._row(ln) for ln in chunk]
        if content:
            rows.append(
                self.style.dim(
                    f"│ … {len(content)} more lines — Enter (empty prompt) for the next page"
                )
            )
        self._print(self._box(f"{title} (continued)", rows))
        if not content:
            # Last page of this box consumed: drop it and surface the end.
            self._pending_pages.pop(0)
            if self._pending_styled:
                self._pending_styled.pop(0)
            if not self._pending_pages:
                self._print(self.style.dim(f"  (end of {title})"))
        return bool(self._pending_pages)

    def _note(self, text: str) -> None:
        self._print(f"  {self.style.dim(text)}")

    def _on_event(self, name: str, payload: dict) -> None:
        """Route one event-bus notification (tool_call, tool_result, denial, error) to the transcript."""
        if name == "tool_call":
            step = payload.get("step")
            tool = payload.get("tool")
            args = payload.get("args") or {}
            # Show file path for file-operation tools so the operator can
            # follow along without waiting for the full reply.
            detail = ""
            if tool in ("write_file", "edit_file", "read_file", "list_dir"):
                path = args.get("path") or args.get("directory") or ""
                if path:
                    detail = f" {self.style.dim('>')} {self.style.dim(_sanitize_output(path.upper()))}"
                    if tool in ("write_file", "edit_file"):
                        # Snapshot the file before the edit lands so the
                        # tool_result can paint a real before/after diff.
                        self._last_edit_path = path
                        self._edit_snapshots[path] = self._file_text(path)
            elif tool == "run_command":
                cmd = args.get("command") or ""
                self._last_command = cmd
                if cmd:
                    detail = f" {self.style.dim('>')} {self.style.dim(_sanitize_output(cmd[:60].upper()))}{'...' if len(cmd) > 60 else ''}"
            elif tool == "shell_output":
                task_id = args.get("task_id") or ""
                if task_id:
                    self._last_shell_task = str(task_id)
                    detail = f" {self.style.dim('>')} {self.style.dim(_sanitize_output(str(task_id).upper()))}"
            # Show the tool step line while the spinner keeps running.
            msg = f"  {self.style.dim(f'* STEP {step}')} {self.style._wrap(theme.BONE_BOLD, tool.upper())}{detail}"
            if self.layout is not None and self.layout.active:
                try:
                    self.layout.write(msg + "\n")
                    try:
                        self.layout.draw_prompt("")
                    except Exception:
                        pass  # duck-typed bridge: prompt redraw is cosmetic
                except Exception:
                    # The bridge rejected the whole write; fall back to
                    # plain output so the step line is never lost.
                    self._print(msg)
            else:
                self._print(msg)
        elif name == "tool_denied":
            self._print(f"  {self.style._wrap(theme.EMBER, 'DENIED')} {self.style.dim(payload.get('tool','').upper())}")
        elif name == "run_error":
            self._print(f"  {self.style._wrap(theme.EMBER, '!! ' + str(payload.get('error')))}")
        elif name == "tool_result":
            tool = payload.get("tool")
            result = payload.get("result")
            ok = payload.get("ok")
            seconds = payload.get("seconds")
            # Show what changed on disk for file-edit tools: a coloured
            # before/after diff for edits, a syntax preview for newly
            # written files — the operator sees the file being edited as
            # the agent works instead of a bare OK line.
            if tool in ("edit_file", "write_file"):
                if ok and isinstance(result, str) and result.strip():
                    shown = self._render_file_change(tool, result)
                    if shown:
                        self._print(shown)
                elif self.verbose:
                    detail = self.style.dim("OK" if ok else "FAILED")
                    self._print(f"    {detail} {seconds}s")
            elif self.verbose:
                detail = self.style.dim("OK" if ok else "FAILED")
                self._print(f"    {detail} {seconds}s")

    def _on_delta(self, piece: str) -> None:
        """Streamed content fragment from the LLM client."""
        if self._abort.is_set():
            raise AbortError("interrupted by operator")
        # Approximate token count: ~4 chars per token.
        self._stream_tokens += max(1, len(piece) // 4)
        rendered = self._stream_renderer.render_piece(piece)
        if self.layout is not None and self.layout.active:
            # The terminal host owns the mouse during streaming: while a
            # native click-drag selection is in progress (Windows, app
            # -owned viewport), every repaint would disturb the drag, so
            # defer the fragment and flush everything once the selection
            # completes. The native-scrollback layout only ever repaints
            # its bottom box, so selection is never at risk there.
            if getattr(self.layout, "is_inline", False) is not True and selection_in_progress():
                self._deferred_stream.append(rendered)
                return
            self._flush_deferred_stream()
            self._stream_write(rendered)
            # Live token counter; the throttled fragment is flushed at
            # the handle() tail.
            self._update_live_counter()
        else:
            if not self._stream_header_done:
                sys.stdout.write(f"{self.style.brand('ENCHANTER')} ")
                self._stream_header_done = True
            _safe_stdout(rendered)
            sys.stdout.flush()
        self._streamed_this_run = True

    def _stream_write(self, text: str) -> None:
        """Route one rendered fragment into the compact viewport."""
        if not self._stream_header_done:
            self.layout.write(f"{self.style.brand('ENCHANTER')} ")
            self._stream_header_done = True
        self.layout.write(text)

    def _flush_deferred_stream(self) -> None:
        """Flush fragments buffered while a native selection was active."""
        if not self._deferred_stream:
            return
        buffered = self._deferred_stream
        self._deferred_stream = []
        for text in buffered:
            self._stream_write(text)
        self._streamed_this_run = True

    def _update_live_counter(self) -> None:
        """Update the live token counter in the bottom prompt row."""
        if self.layout is None or not self.layout.active:
            return
        # Throttle: only update every 100ms to avoid flicker.
        now = time.monotonic()
        if now - self._last_counter_update < 0.1:
            return
        self._last_counter_update = now
        # Short form (1.2k); the label stays bone while the live counter
        # sits faint to its right, followed by the stream rate.
        tok_str = _short(self._stream_tokens)
        elapsed = max(0.1, now - (self._turn_started or now))
        rate_str = _short(round(self._stream_tokens / elapsed))
        if self.style.enabled:
            counter = self.style._wrap(theme.FAINT, f" {tok_str} tok · {rate_str} tok/s")
            body = self.style.hair("\u2502 ") + self.style.bone("MANTRA >") + counter
        else:
            body = "\u2502 MANTRA >" + f" {tok_str} tok · {rate_str} tok/s"
        self.layout.draw_prompt(body=body)

    # ---- approvals -------------------------------------------------------

    def _ask(self, prompt: str) -> str:
        """Terminal prompt for the approval policy. Returns y / n / a."""
        self._print("")
        self._print(f"  {self.style.bold('allow?')} {prompt}")
        self._print(self.style.dim("  [y]es   [n]o   [a]lways for this session"))
        try:
            answer = input("  allow> ").strip().lower()
        except (KeyboardInterrupt, EOFError):
            return "n"
        if answer in ("a", "always"):
            return "a"
        if answer in ("y", "yes"):
            return "y"
        return "n"

    # ---- @ mentions -------------------------------------------------------

    # ---- interrupt handling ----------------------------------------------

    def _install_sigint(self) -> None:
        """Install the double-Ctrl+C handler.

        signal.signal only works on the main thread, so handle() must
        run on the main thread outside the TUI for this handler to take
        effect; inside the TUI the turn runs on a worker thread and
        TuiApp owns Ctrl+C itself.
        """
        def handler(signum, frame):
            if self._abort.is_set():
                raise KeyboardInterrupt
            self._abort.set()
            self._print(self.style.dim("  (stopping after this step - ctrl+c again to quit)"))

        self._prev_sigint = signal.getsignal(signal.SIGINT)
        try:
            signal.signal(signal.SIGINT, handler)
        except (ValueError, OSError):
            # Off the main thread (TUI worker): signal.signal refuses.
            # Fall back to the terminal application's own Ctrl+C
            # handling; no session-level handler applies here.
            self._prev_sigint = None

    def _restore_sigint(self) -> None:
        if self._prev_sigint is not None:
            try:
                signal.signal(signal.SIGINT, self._prev_sigint)
            except (ValueError, OSError):
                pass

    # ---- message handling ------------------------------------------------

    def _effective_system_prompt(self, request_text: str = "") -> str:
        """The base prompt plus whatever the session is aiming at.

        Rebuilt per turn rather than frozen at startup, because the goal
        is set and cleared while the session is running. Appended rather
        than spliced into the base so the standing instructions stay
        intact when the goal changes. Re-applies total cap after additions.
        """
        prompt = self.system_prompt
        for name in self.active_skills:
            skill = skills.get(name)
            if skill is None or not skill.body.strip():
                continue
            prompt += (
                "\n\n## Skill in force: " + skill.name
                + "\nFollow this procedure where it applies to the current task.\n\n"
                + skill.body.strip()
            )
            if skill.resources:
                prompt += "\n\nBundled with this skill: " + ", ".join(skill.resources)
        # Progressive disclosure: the newest memory entries already ride
        # in the base prompt; add only the older entries this request
        # actually touches (keyword-ranked, capped).
        rel = relevant_memory(self.memory_path, request_text)
        if rel:
            prompt += "\n\n## Memory relevant to this request\n" + rel
        if not self.goal and not self.todos:
            # Re-apply cap even when only skills were added
            if len(prompt) > SYSTEM_PROMPT_CAP:
                prompt = _truncate_codepoint(prompt, SYSTEM_PROMPT_CAP) + "\n... [truncated — system prompt exceeded cap]"
            return prompt
        lines: list[str] = []
        if self.goal:
            lines += [
                "",
                "## Standing goal",
                "The operator set this goal for the session. It outlives any",
                "single message: work toward it on every turn, and treat the",
                "current request as a step within it rather than a replacement.",
                "",
                f"Goal: {self.goal}",
            ]
            if self.goal_notes:
                lines.append("")
                lines.append("Notes recorded while working toward it:")
                for note in self.goal_notes:
                    lines.append(f"- {note}")
            lines.append("")
            lines.append(
                "When the goal is fully met, say so plainly in your final "
                "message and start it with GOAL COMPLETE so the operator can "
                "clear it without checking by hand. Do not claim it is "
                "complete until it actually is."
            )
        if self.todos:
            lines += self._todos_prompt_lines()
        injected_block = "\n" + "\n".join(lines)
        base_len = len(prompt)
        prompt = prompt + injected_block
        if len(prompt) > SYSTEM_PROMPT_CAP:
            # Truncate on a code-point boundary so no multibyte char is
            # split mid-sequence.
            prompt = _truncate_codepoint(prompt, SYSTEM_PROMPT_CAP) + "\n... [truncated — prompt exceeded cap after goal/todo injection]"
            # The injected goal/todo block is the tail, so a cap hit can
            # drop it entirely; say so rather than losing it silently.
            if base_len >= SYSTEM_PROMPT_CAP:
                self._note("goal/todo block dropped - system prompt over the total cap")
        return prompt

    def _todos_prompt_lines(self) -> list[str]:
        """The session checklist as prompt lines - every item, with its state.

        Completed items stay listed (struck through) rather than being
        pruned, because the checklist is the shared record: an agent that
        has seen an item all session should not suddenly find it gone.
        """
        open_count = sum(1 for t in self.todos if not t["done"])
        state = "all done" if not open_count else f"{open_count} open"
        lines = ["", "## Session todo list", f"The operator keeps a checklist of {state} item{'s' if open_count != 1 else ''} for this session:"]
        for item in self.todos:
            mark = " " if not item["done"] else "x"
            lines.append(f"- [{mark}] {item['text']}")
        lines += [
            "",
            "Work through the open items; they outlive any single request.",
            "Keep the list current as you work: when a task turns up real",
            "follow-up work, add it yourself by putting a line in your",
            "final message reading TODO ADD: <the follow-up, stated as a",
            "concrete task>. Only add work that genuinely remains - not",
            "steps you are about to do anyway, and not busywork.",
            "When you finish an item, report it on its own line as",
            "TODO DONE: <the item's exact text> so it is checked off.",
            "Never claim an item is done until it actually is, and never",
            "remove or edit items - the operator owns the list.",
        ]
        return lines

    def auto_route(self, text: str) -> str | None:
        """Attach the skill this request is asking for, without being asked.

        Returns the name of a bundle that fits the request, or None. The
        skill is attached for this turn only; the bundle is handed back
        rather than launched, because a bundle is several turns and the
        caller has to decide whether to spend them.

        Declines when a skill is already attached: that means the
        operator chose one, or a bundle step chose one, and either way a
        deliberate choice outranks a guess.
        """
        # config.json sets the baseline; the preferences the operator
        # switched with /skills auto are stored beside their endpoints and
        # win over it, because a choice made out loud outranks a file.
        prefs = dict(self.config.get("skills") or {})
        prefs.update(skills_prefs())
        if not prefs.get("auto", True) or self.in_bundle or self.active_skills:
            return None
        if not skills.list_skills():
            # Nothing to route to, so do not pay for a scan of a directory
            # the operator has not populated on every single turn.
            return None
        found, bundle = skills.recommend(text)
        if found is not None:
            key = found.name.lower()
            self.active_skills.append(key)
            self.auto_attached.append(key)
            summary = " ".join(str(found.description).split())
            if len(summary) > 56:
                summary = summary[:53].rstrip() + "..."
            self._note(f"skill auto-attached: {found.name} — {summary}")
            _warn_untrusted_skill(self, found)
        if bundle is None:
            return None
        if prefs.get("auto_bundle", False):
            # The bundle attaches its own skill per step, so the single
            # skill routed a moment ago would only crowd it out.
            self._detach_auto()
            return bundle
        # Left as a hint rather than launched: a bundle is several turns
        # of work and starting that on a guess is not a favour.
        self._note(f"bundle '{bundle}' covers this end to end - /skills launch {bundle}")
        return None

    def _detach_auto(self) -> None:
        """Drop whatever the router attached, once the turn is over."""
        if not self.auto_attached:
            return
        self.active_skills = [s for s in self.active_skills if s not in self.auto_attached]
        self.auto_attached = []

    def handle(self, text: str) -> RunResult | None:
        """Run one turn: expand mentions, route a skill, execute the agent
        loop, and report the result (usage, memory, changes, todo/goal
        checks). Must run on the main thread outside the TUI; inside the
        TUI it runs on the app's worker thread instead."""
        self.last_error = None  # a fresh turn starts clean for /fix
        self.last_reply = ""  # the finished turn's assistant reply (suggestion engine input)
        # Turn-scoped read-before-edit: a fresh turn must not inherit
        # what the previous turn had already read or edited.
        for tool in self.tools:
            ledger = getattr(tool, "ledger", None)
            if ledger is not None:
                ledger.forget_all()
        # Startup card disappears on first real work. Splash rows count as
        # no content: they live in the same viewport buffer, and counting
        # them made the very first turn look like "resumed content" — the
        # card then never left the buffer, resurfacing above the prompt
        # mid-stream and re-appearing misplaced after a resize.
        has_resumed_content = (
            self.layout is not None
            and self.layout.active
            and len(self.layout.lines) > 0
            and not getattr(self.layout, "_splash_visible", False)
        )
        if getattr(self, "_splash_visible", False) and self.layout is not None and not has_resumed_content:
            try:
                if getattr(self.layout, "_splash_visible", False):
                    self.layout.hide_splash()
            except Exception:
                pass  # duck-typed bridge: splash removal is cosmetic
            self._splash_visible = False
        self.message_count += 1
        self._task_counter += 1
        self._abort.clear()
        # The untouched request: the router must score what the operator
        # said, not the message with attached file dumps appended, and a
        # launched bundle must still see the original wording.
        request_text = text
        text, attached = self.expand_mentions(text)
        if attached:
            shown = attached[:8]
            extra = "" if len(attached) <= 8 else f" (+{len(attached) - 8} more)"
            self._note("attached: " + ", ".join(shown) + extra)
        # Routed before the turn runs, so the chosen procedure is in the
        # system prompt the agent actually reads rather than in the next
        # one, which may never come.
        bundle = self.auto_route(request_text)
        if bundle:
            return _skills_launch(self, bundle, initial_text=text)
        task = {"task_id": f"console-{self._task_counter}", "problem_statement": text}

        self._auto_compact()

        loop = AgentLoop(
            llm=self.llm,
            sandbox=self.sandbox,
            tools=self.tools,
            evaluator=NullEvaluator(),
            logger=self.logger,
            events=self.bus,
            system_prompt=self._effective_system_prompt(request_text),
            max_steps=self.max_steps,            on_delta=self._on_delta,
            context=self.context,
            abort=self._abort,
            approver=self.approvals,
            on_tool_result=self._on_tool_observation,
            observation_max_chars=self._observation_max_chars(),
            digest=bool((self.config.get("context") or {}).get("digest", True)),
            digest_max_chars=int((self.config.get("context") or {}).get("digest_max_chars", 4000) or 0),
        )
        self._streamed_this_run = False
        self._stream_header_done = False
        self._stream_tokens = 0
        self._last_counter_update = 0.0
        self._stream_renderer.reset()
        # Reports the agent emits this turn are applied and announced as
        # its reply streams; the list is cleared so the end-of-turn pass
        # knows what it still has to handle.
        self._turn_todo_reports = []
        self._stream_renderer.report_hook = self._handle_stream_todo_report

        self._turn_started = time.monotonic()
        # The terminal application runs this turn on its worker thread
        # and owns all input and screen updates; the session only marks
        # the busy state so the status row can show it.
        self._set_busy(True)
        result = None
        self._install_sigint()
        try:
            result = loop.run(task)
        except (KeyboardInterrupt, AbortError):
            self._abort.set()
            self._print(self.style.ember("  interrupted"))
        except HarnessError as exc:
            self._print(self.style.ember(f"  !! {exc}"))
        else:
            # Flush any remaining buffered text from the streaming renderer.
            # Fragments deferred while a native selection was active must
            # land before the renderer tail or they would be lost.
            try:
                self._flush_deferred_stream()
            except Exception:
                pass  # deferred fragments are best effort; the turn already ended
            if self._streamed_this_run:
                tail = self._stream_renderer.flush()
                if tail:
                    if self.layout is not None and self.layout.active:
                        self.layout.write(tail + "\n")
                        # Ensure throttled viewport actually renders the final tail
                        try:
                            self.layout.flush()
                        except Exception:
                            pass  # duck-typed bridge: flush is cosmetic, never fatal
                    else:
                        _safe_stdout(tail)
                        sys.stdout.flush()
                # Streamed in full, so the reply is already on screen - do
                # not print it again. A second copy would sit below the
                # first, and because the streaming path emits raw text
                # while render_markdown would strip its marks, the two
                # would disagree with each other line for line.
                if self.layout is not None and self.layout.active:
                    self.layout.write("\n")
                    try:
                        self.layout.flush()
                    except Exception:
                        pass  # duck-typed bridge: flush is cosmetic, never fatal
                else:
                    sys.stdout.write("\n")
                    sys.stdout.flush()
            elif result is not None and result.final_message:
                # Same sanitization contract as the streaming path: model
                # text must never drive the terminal, whether it arrives
                # one fragment at a time or all at once.
                body = render_markdown(_sanitize_output(result.final_message), self.style)
                self._print(f"{self.style.brand('ENCHANTER')} {body}")
            if result is not None:
                self._finish_turn_report(task, result)
                # The TUI's suggestion engine reads the assistant's final
                # reply (plus the prompt it answers) to pick follow-up
                # chips; stash it turn-scoped, cleared at next turn start.
                self.last_reply = result.final_message or ""
        finally:
            self._restore_sigint()
            self._set_busy(False)
            # Last, so the skill the router chose is in force for the whole
            # turn - including the divider row - and not a moment less.
            self._detach_auto()
            # Respect a deliberate scroll: if the operator detached to
            # read earlier output, the final reply and footer are still
            # in the transcript - never yank the viewport back to the
            # tail, or scrolling after a task looks broken.
            if self.layout is not None and self.layout.active:
                try:
                    if self.layout.following:
                        self.layout.scroll_to_bottom()
                except Exception:
                    pass  # duck-typed bridge: auto-scroll is cosmetic
            self._edit_snapshots.clear()
            self._last_edit_path = None
            self._last_command = None
            self._last_shell_task = None
            # Tell the operator capped output can be paged through with an
            # empty Enter (the queue survives until the next turn). The
            # hint only makes sense on the TUI path: the plain REPL has no
            # empty-Enter paging, so an unconditional hint would lie and
            # the queue would just grow until the next autosave.
            if self._pending_pages and self.layout is not None and self.layout.active:
                self._print(self.style.dim("  (capped output above — press Enter with an empty prompt to page through it)"))
            self._end_turn(result)
            # Non-streamed turns print their last lines inside the same
            # throttle window, so force one render here — without it the
            # usage footer can sit buffered and invisible until the next
            # turn happens to repaint.
            if self.layout is not None and self.layout.active:
                try:
                    self.layout.flush()
                except Exception:
                    pass  # duck-typed bridge: flush is cosmetic, never fatal
        return result

    def _finish_turn_report(self, task: dict, result: "RunResult") -> None:
        """Record usage and memory, report changes and goal/todo checks, save.

        Runs only after the reply is fully on screen, so a session saved
        mid-turn can never be missing the assistant's last answer.
        """
        self._record_usage(result)
        self._record_memory(task, result)
        self._report_changes()
        self._check_goal_completion(result)
        self._check_todo_completion(result)
        # Attention: a failed turn or a denied approval is the
        # one thing that must not pass silently.
        if result.stopped_reason == "error" or int(result.metrics.get("denied", 0)) > 0:
            self._attention()
        self.autosave()

    def _end_turn(self, result: "RunResult | None") -> None:
        """Print the usage footer for the turn that just ended."""
        # Clear the live token counter so the next prompt is clean.
        self._stream_tokens = 0
        if result is not None:
            self._print(f"  {self.style.dim(self._usage_line(result))}")

    # ---- inspection commands ---------------------------------------------

    # ---- git/workspace/cost, endpoints, banner follow ---------------------

    def banner(self) -> None:
        s = self.style
        art = [
            "╔══════════════════════════════╗",
            "║  M A N T R A                 ║",
            "║  coding agent harness        ║",
            "╚══════════════════════════════╝",
        ]
        for line in art:
            self._print(s.brand(line))
        llm_cfg = self.config.get("llm", {})
        self._print(f"model      {s.bold(llm_cfg.get('model', '?'))}")
        self._print(f"endpoint   {s.dim(llm_cfg.get('base_url', '?'))}")
        self._print(f"workspace  {self.sandbox.root}")
        branch = self._git("rev-parse", "--abbrev-ref", "HEAD").strip()
        if branch:
            dirty = self._git("status", "--porcelain")
            state = "clean" if not dirty else f"{len(dirty.strip().splitlines())} uncommitted"
            self._print(f"git        {branch} ({state})")
        self._print(f"approvals  {self.approvals.mode}  {s.dim('/'.join(MODES))}")
        self._print(f"tools      {len(self.tools)} loaded, step limit {self.max_steps}")
        if self.instructions_path:
            self._print(s.dim(f"instructions loaded from {os.path.basename(self.instructions_path)}"))
        try:
            if os.path.isfile(KNOWN_FAILURES_PATH):
                # Skip the "## KF-N" template line: only numbered entries count.
                with open(KNOWN_FAILURES_PATH, encoding="utf-8", errors="replace") as kf:
                    count = sum(
                        1 for ln in kf
                        if ln.startswith("## KF-") and ln[6:7].isdigit()
                    )
                self._print(s.dim(f"known-failure registry: {count} classes"))
        except OSError:
            # A race or permission error on a piped run must not crash the banner.
            pass
        self._print(s.dim("type /help for commands"))


_SAFE_HOME = os.path.expanduser("~")


def _infer_workspace() -> str:
    """Current directory becomes the workspace, like a real agent CLI.

    Guards: skip MANTRA itself (uses its own sandbox) and refuse to turn
    filesystem roots or the user home into a workspace - git-isolating
    ``C:\\Users\\<name>`` would be destructive nonsense.
    """
    cwd = os.getcwd()
    if cwd == PROJECT_ROOT:
        return os.path.join(PROJECT_ROOT, "workspace")
    drive = os.path.splitdrive(cwd)[0]
    # Drive-root form follows the platform separator; on POSIX splitdrive
    # returns "", which must not become a protected path or every cwd
    # would be rejected.
    drive_root = drive + os.sep if drive else None
    protected = {
        os.path.dirname(_SAFE_HOME.rstrip("\\/")) or _SAFE_HOME,
        _SAFE_HOME,
    }
    if drive_root:
        protected.add(drive_root)  # drive root, e.g. C:\
    if os.name != "nt":
        protected.add(os.sep)  # filesystem root on POSIX: never git-isolate it
    normalized = cwd.rstrip("\\/")
    if any(normalized.lower() == p.lower().rstrip("\\/") for p in protected):
        return os.path.join(PROJECT_ROOT, "workspace")
    return cwd


def dispatch(session: ConsoleSession, line: str) -> bool:
    """Run a slash command. Returns True when the line was a command."""
    # Be forgiving: strip invisible leading chars and normalize full-width forms
    stripped = _strip_leading_invisible(line)
    # Normalize full-width characters that look like slash or at-sign
    if stripped.startswith("\uff0f"):
        stripped = "/" + stripped[1:]
    if stripped.startswith("／"):
        stripped = "/" + stripped[1:]
    if not stripped.startswith("/"):
        return False
    parts = stripped.split(None, 1)
    # Strip trailing punctuation from the command token only - a period
    # at the end of "/goal fix the bug." belongs to the argument.
    command = parts[0].lower().rstrip(".,;:!?)'\"`").strip("'\"`")
    # Also handle quoted commands like "/help" or '/help'
    command = command.rstrip(".,;:!?")
    # Normalize any internal full-width slash
    command = command.replace("\uff0f", "/").replace("／", "/")
    argument = parts[1].strip() if len(parts) > 1 else ""
    # Also strip surrounding quotes from argument for robustness
    argument = argument.strip("'\"`")

    if command in ("/exit", "/quit"):
        # Persist the pager state at exit time: content paged through
        # since the last autosave is dropped, what remains survives an
        # interrupted resume. Silent when there is nothing to save yet.
        try:
            session.autosave()
        except Exception:
            pass  # a failed autosave must not prevent the exit itself
        raise SystemExit(0)
    if command in ("/help", "/"):
        session._print(HELP_TEXT)
    elif command == "/workspace":
        session.show_workspace()
    elif command == "/memory":
        _memory(session)
    elif command == "/diff":
        _diff(session)
    elif command == "/fix":
        _fix(session, argument)
    elif command == "/undo":
        session.undo_changes()
    elif command == "/model":
        # One command for providers and models; /connect is gone so a
        # single name is advertised and nothing else drifts in.
        _model_command(session, argument.split())
    elif command in ("/reasoning", "/effort"):
        _reasoning(session, argument)
    elif command == "/approve":
        _approve(session, argument)
    elif command == "/cost":
        _cost(session, argument)
    elif command == "/compact":
        _compact(session)
    elif command in ("/clear", "/reset"):
        _clear(session)
    elif command == "/sessions":
        _sessions(session, argument)
    elif command == "/export":
        _export(session, argument)
    elif command == "/goal":
        _goal(session, argument)
    elif command == "/todo":
        _todo(session, argument)
    elif command == "/workflow":
        _workflow(session, argument)
    elif command in ("/skills", "/skill"):
        _skills(session, argument)
    elif command == "/mcp":
        _mcp(session, argument)
    elif command == "/verbose":
        session.verbose = not session.verbose
        session._print(f"verbose {'on' if session.verbose else 'off'}")
    elif command == "/suggestions":
        _set_suggestions(session, argument)
    else:
        # A line like "/tmp/x" or "/usr/local/bin" is a path, not a
        # command; hand it to the agent instead of swallowing it. A
        # single-token "/typo" keeps the unknown-command error.
        if "/" in command[1:] or "/" in argument:
            return False
        session._print(f"unknown command '{command}' - /help for the list")
    return True


def main(argv: list[str] | None = None) -> int:
    """Console entry point: parse flags, build the session, and run the once / plain-REPL / TUI path. Returns the process exit code."""
    parser = argparse.ArgumentParser(prog="mantra-console", description="MANTRA interactive console")
    parser.add_argument("--config", default=_resolve_data_path("examples", "config.json"))
    parser.add_argument("--workspace", default=None, help="Persistent working folder (default: current directory)")
    parser.add_argument("--once", default=None, metavar="MSG", help="Handle one message non-interactively, then exit")
    parser.add_argument("--model", default=None, help="Override the configured model")
    parser.add_argument("--reasoning", default=None, metavar="LEVEL",
                        help=f"Thinking effort: {', '.join(REASONING_EFFORTS)}, or off")
    parser.add_argument("--endpoint", default=None, metavar="URL",
                        help="Override the endpoint base URL for this run")
    parser.add_argument("--approve", default=None, choices=list(MODES), help="Override the approval mode")
    parser.add_argument("--plain", action="store_true", help="Disable ANSI styling")
    parser.add_argument("--compact", action="store_true", help="(kept for compatibility; the full-screen console is the only surface)")
    args = parser.parse_args(argv)

    # Windows consoles start on cp1252; switching both the console codepage
    # and the Python streams to UTF-8 is what lets arrows and box-drawing
    # characters render instead of raising or degrading to "?".
    force_utf8_output()

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        # Same contract as the headless runner: a bad config file is a
        # two-line diagnosis, never a traceback.
        print(f"error: {exc}", file=sys.stderr)
        return 2
    # Apply the user's saved active pick so the startup check and header
    # reflect the chosen endpoint instead of the example default.
    # Failures here are non-fatal: defaults and explicit flags still apply.
    try:
        act = get_active()
        ep_name = (act.get("endpoint") or "").strip()
        if ep_name and not args.endpoint:
            ep = known_endpoints().get(ep_name.lower())
            if ep and ep.get("base_url"):
                llm_cfg = config.setdefault("llm", {})
                llm_cfg["base_url"] = ep["base_url"]
                if ep.get("api_key_env"):
                    llm_cfg["api_key_env"] = ep["api_key_env"]
                if not args.model and act.get("model"):
                    llm_cfg["model"] = act["model"]
                if not args.reasoning and "reasoning_effort" in act:
                    llm_cfg["reasoning_effort"] = act["reasoning_effort"]
    except Exception:
        pass  # saved-pick restore is advisory; explicit flags still apply
    if args.endpoint:
        url = args.endpoint.rstrip("/")
        if not url.startswith(("http://", "https://")):
            parser.error("--endpoint must start with http:// or https://")
        _apply_endpoint_override(config, url)
    if args.model:
        config.setdefault("llm", {})["model"] = args.model
    if args.reasoning:
        level = args.reasoning.strip().lower()
        config.setdefault("llm", {})["reasoning_effort"] = (
            None if level in ("off", "none") else level
        )
        if level not in ("off", "none") and level not in REASONING_EFFORTS:
            parser.error(
                f"unknown reasoning level '{args.reasoning}'; "
                f"choose from {', '.join(REASONING_EFFORTS)} or off"
            )
    if args.approve:
        # An explicit flag outranks every stored preference.
        config["approvals"] = args.approve
    else:
        # Otherwise the operator's last /approve choice wins over the
        # config file: it is a preference, not a per-run detail, so a
        # restarted session keeps the mode the operator actually picked
        # instead of reverting to whatever the file says.
        try:
            from core.agent.settings import ui_prefs

            saved = str(ui_prefs().get("approvals") or "").strip()
            if saved in MODES:
                config["approvals"] = saved
        except Exception:
            pass  # unreadable preferences: the config default stands

    # Color follows the stream: a real terminal gets ANSI, a pipe or
    # redirect gets clean text unless the operator forces color.
    # NO_COLOR (the cross-tool convention) always wins.
    if args.plain or os.environ.get("NO_COLOR") is not None:
        color_on = False
    else:
        mode = os.environ.get("MANTRA_COLOR", "auto").lower()
        forced = os.environ.get("MANTRA_FORCE_COLOR", "").lower() in ("1", "true", "yes")
        color_on = mode == "always" or (mode != "never" and (sys.stdout.isatty() or forced))
    style = Style(enabled=color_on)
    workspace = args.workspace or _infer_workspace()
    session = ConsoleSession(config, workspace, style, config_path=args.config)

    interactive = sys.stdin.isatty() and sys.stdout.isatty()

    if args.once is not None:
        session._warn_if_key_missing()
        session._warn_if_any_key_missing()
        try:
            result = session.handle(args.once)
        finally:
            session.logger.close()
        # handle() returns None when the run was interrupted or failed
        # before producing a result: that is a failure, not a success.
        if result is None or result.stopped_reason in ("error", "aborted"):
            return 1
        return 0

    if not interactive:
        # Piped runs keep the plain read-eval-print loop.
        session.banner()
        _repl_plain(session)
        return 0
    return _run_terminal(session)


def _repl_plain(session: ConsoleSession, reader: Any = None) -> None:
    """The plain read-eval-print loop for piped (non-tty) runs.

    ``reader`` supplies one line per call when a caller drives the loop
    with a script instead of the console.
    """
    while True:
        try:
            line = (reader("MANTRA > ") if reader is not None else input("MANTRA > ")).strip()
        except (KeyboardInterrupt, EOFError):
            print("bye")
            _close_mcp(session)
            session.logger.close()
            return
        if not line:
            continue
        try:
            if not dispatch(session, line):
                session.handle(line)
        except SystemExit:
            _close_mcp(session)
            session.logger.close()
            return


def _close_mcp(session: ConsoleSession) -> None:
    """Shut down every MCP server this session started.

    The children are real processes, so they are terminated rather than
    left for the operating system to reap at exit.
    """
    for client in getattr(session, "mcp_clients", []) or []:
        try:
            client.close()
        except Exception:
            pass  # a failed shutdown must not mask the exit path
    session.mcp_clients = []


def _run_terminal(session: ConsoleSession) -> int:
    """Interactive entry: the terminal application owns everything."""
    from core.tui.app import TuiApp

    app = TuiApp(session)
    app.composer.completer = ConsoleCompleter(session)
    session._warn_if_key_missing()
    session._warn_if_any_key_missing()
    needs_setup = _needs_first_run(session)
    try:
        if needs_setup:
            # Nothing usable is configured; walk the operator through it
            # inside the running application so menus and prompts work.
            session._print(session.style.dim("no endpoint configured yet - let's connect one."))
            app.run_detached(lambda: _connect(session, []))
        app.start()
    finally:
        try:
            session.autosave()
        except Exception:
            pass  # a failed autosave must not mask the operation it follows
        _close_mcp(session)
        session.logger.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())


# The command modules call back into this module (menus, readers, the
# catalogue fetch) through its namespace, so the test suite's seam
# patches keep working. Import them last: they read attributes at call
# time, not import time.
from core.console_skills import (  # noqa: E402,F401
    _skills,
    _skills_auto,
    _skills_bundles,
    _skills_dashboard,
    _skills_find,
    _skills_help,
    _skills_launch,
    _skills_list,
    _skills_one_shot,
    _skills_show,
    _skills_use,
    _skills_use_all,
    _warn_untrusted_skill,
)
from core.console_commands import (  # noqa: E402,F401
    _approve,
    _clear,
    _compact,
    _cost,
    _diff,
    _export,
    _fix,
    _goal,
    _memory,
    _mcp,
    _reasoning,
    _sessions,
    _set_suggestions,
    _todo,
    _workflow,
    _workflow_create,
    _workflow_launch,
    _workflow_remove,
    _workflow_show,
)
from core.console_model import (  # noqa: E402,F401
    ADD_ENDPOINT,
    NEW_ENDPOINT,
    PICK_MODEL,
    RE_ENTER_KEY,
    REMOVE_ENDPOINT_ENTRY,
    REPLACE_KEY_ENTRY,
    SHOW_ALL_MODELS,
    SHOW_FIRST_MODELS,
    SWITCH_ENDPOINT,
    SWITCH_ENDPOINT_ENTRY,
    TYPE_A_MODEL,
    _FIRST_MODEL_WINDOW,
    _LARGE_MODEL_CATALOGUE,
    _apply_model,
    _choose_model,
    _connect,
    _connect_choose_endpoint,
    _connect_new,
    _connect_remove,
    _effort_options,
    _endpoint_options,
    _is_auth_failure,
    _model_command,
    _model_help,
    _model_master,
    _pick_effort,
    _replace_key,
    _rescue_catalogue,
    _store_key,
    _try_fetch,
    _type_a_model,
)
