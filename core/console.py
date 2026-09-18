"""Interactive console: session, commands, streaming, terminal UI.

The module is a facade: presentation lives in console_render, shared
primitives in console_common, the completer in console_completer, and
the command surfaces in console_skills/console_commands/console_model.
Every public name is re-exported here, so callers (and the test suite's
seam patches) keep working unchanged.
"""

from __future__ import annotations

import argparse
import difflib
import glob
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
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
from core.agent.keys import has_stored, mask, store as store_key, stored_keys
from core.agent.models import fetch_models, is_reasoning_model
import core.agent.sessions as sessions
import core.agent.skills as skills
import core.agent.workflows as workflows
from core.agent.settings import (
    active as get_active,
    add_endpoint,
    endpoint_name_for_url,
    endpoints as known_endpoints,
    models_for,
    remove_endpoint,
    set_active,
    set_models,
    set_skills_prefs,
    settings_path,
    skills_prefs,
    validate_endpoint,
)
from core.agent.knowledge import (
    append_memory,
    plan_memory_write,
    read_raw_tail,
    relevant_memory,
    rewrite_memory,
    assemble_system_prompt,
    find_instructions_file,
    render_environment,
)
from core.evaluators import NullEvaluator
from core.logs import JsonlLogger
from core.sandbox import LocalSandbox
from core.tui.overlays import Option
from core.tui.composer import Completion
from core.registry import build_llm, build_tools

from core.term import (
    enable_vt,
    force_utf8_output,
    safe_write,
    selection_in_progress,
    term_size,
    visible_len,
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
    _strip_leading_invisible,
    provider_needs_key,
)

# Completer (no seam patching; plain import).
from core.console_completer import ConsoleCompleter  # noqa: F401


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


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


MAX_ATTACH_CHARS = 20_000
MAX_TOTAL_ATTACH_CHARS = 60_000
MAX_GLOB_HITS = 20
MAX_LISTING_ENTRIES = 100
SYSTEM_PROMPT_CAP = 20_000


def _short(count: int) -> str:
    """1234 -> 1.2k. Token counts only ever need two significant figures."""
    if count < 1000:
        return str(count)
    if count < 10_000:
        return f"{count / 1000:.1f}k"
    return f"{round(count / 1000)}k"


def _safe_int(value: Any) -> int:
    """Total from a session file as an int; corrupt values read as zero.

    Session files are hand-editable JSON, so one non-numeric total must
    cost that counter, not the whole restore command.
    """
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _format_elapsed(seconds: float) -> str:
    """Format elapsed time: '1.6s', 'done in 1m23s', 'done in 1h05m'."""
    if seconds < 60:
        return f"{seconds:.1f}s"
    if seconds < 3600:
        m = int(seconds) // 60
        s = int(seconds) % 60
        return f"done in {m}m{s:02d}s"
    h = int(seconds) // 3600
    m = (int(seconds) % 3600) // 60
    return f"done in {h}h{m:02d}m"


def _transcript(messages: list[dict[str, Any]]) -> str:
    """Flatten history to plain text for the summariser."""
    lines = []
    for message in messages:
        role = message.get("role", "?")
        content = message.get("content") or ""
        if role == "system":
            continue
        if role == "assistant" and message.get("tool_calls"):
            names = ", ".join(
                (call.get("function") or {}).get("name", "?")
                for call in message["tool_calls"]
            )
            lines.append(f"assistant: [called {names}]")
            if content:
                lines.append(f"assistant: {content}")
            continue
        if role == "tool":
            body = content if len(content) <= 300 else content[:300] + " ..."
            lines.append(f"result of {message.get('name', 'tool')}: {body}")
            continue
        lines.append(f"{role}: {content}")
    return "\n".join(lines)


# ------------------------------------------------------------------- session
class ConsoleSession:
    """One REPL session over one persistent local workspace."""

    def __init__(
        self,
        config: dict,
        workspace: str,
        style: Style,
        llm: Any = None,
        ask: Any = None,
    ) -> None:
        self.config = config
        self.style = style
        self.workspace = workspace
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
        self.llm = llm if llm is not None else build_llm(config["llm"])
        self.approvals = ApprovalPolicy(
            mode=config.get("approvals", "default"),
            ask=ask or self._ask,
            note=self._note,
        )
        self.totals = {"tokens_in": 0, "tokens_out": 0, "turns": 0, "tool_errors": 0, "cache_hit": 0}
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

    def _format_diff(self, diff_text: str, max_lines: int = 60, title: str = "") -> str:
        """Colour a unified diff inside a small box, optionally titled."""
        if not diff_text:
            return ""
        lines = diff_text.splitlines()
        total = len(lines)
        if len(lines) > max_lines:
            lines = lines[:max_lines]
            lines.append(self.style.dim(f"... ({total - max_lines} more lines)"))
        if title:
            top = self.style._wrap(theme.HAIR, "┌ ") + self.style._wrap(theme.BONE, title)
        else:
            top = self.style._wrap(theme.HAIR, "┌" + "─" * 38)
        out = [top]
        for line in lines:
            if line.startswith("+"):
                out.append(self.style._wrap(theme.HAIR, "│ ") + self.style._wrap(theme.DIFF_ADD, line))
            elif line.startswith("-"):
                out.append(self.style._wrap(theme.HAIR, "│ ") + self.style._wrap(theme.DIFF_REMOVE, line))
            else:
                out.append(self.style._wrap(theme.HAIR, "│ ") + self.style._wrap(theme.FAINT, line))
        out.append(self.style._wrap(theme.HAIR, "└" + "─" * 38))
        return "\n".join(out)

    def _file_text(self, rel: str) -> str | None:
        """Full text of a workspace-relative file, or None when unreadable."""
        if not rel:
            return None
        # The path can arrive from model-supplied tool arguments, so the
        # join is confined like every other workspace read: a "..", an
        # absolute path, or a symlink must not widen the snapshot read
        # beyond the workspace.
        try:
            root = os.path.realpath(self.workspace)
            full = os.path.realpath(os.path.join(root, rel))
            if not (full == root or full.startswith(root + os.sep)):
                return None
        except OSError:
            return None
        try:
            if not os.path.isfile(full):
                return None
            if os.path.getsize(full) > 1_000_000:
                return None
            with open(full, "r", encoding="utf-8", errors="replace") as fh:
                return fh.read()
        except OSError:
            return None

    def _render_file_change(self, tool: str, result: str) -> str:
        """Readable result of edit_file / write_file.

        edit_file gets a coloured before/after diff against the snapshot
        taken when the tool_call arrived; write_file shows a syntax
        preview of the new file. Falls back to the tool's own message
        when the file is missing or too large to snapshot, so the
        operator always sees the file that was just edited, capped and
        wrapped instead of dumped raw.
        """
        path = self._last_edit_path or ""
        # The path is model-supplied tool-argument text, so the display
        # form must not carry ANSI escapes into box titles or diff labels.
        display = _sanitize_output(path)
        new = self._file_text(path) if path else None
        if new is None:
            if isinstance(result, str) and result.strip():
                return self._format_diff(result.strip(), max_lines=20)
            return ""
        old = self._edit_snapshots.get(path)
        if tool == "write_file" and old is None:
            # A brand-new file: syntax preview, capped so a long file
            # stays readable.
            lines = new.splitlines()
            total = len(lines)
            if total > 120:
                lines = lines[:120]
            out = [self.style._wrap(theme.HAIR, "┌ ") + self.style._wrap(theme.BONE, f"wrote {display} ({total} lines)")]
            for line in lines:
                out.append(_syntax_highlight(line, self.style))
            if total > 120:
                out.append(self.style.dim(f"  ... {total - 120} more lines"))
            out.append(self.style._wrap(theme.HAIR, "└" + "─" * 38))
            return "\n".join(out)
        body = "\n".join(
            difflib.unified_diff(
                (old or "").splitlines(),
                new.splitlines(),
                fromfile=display + " (before)",
                tofile=display + " (after)",
                lineterm="",
                n=2,
            )
        )
        if body.strip():
            pane = self._diff_pane_rows(body, max_lines=220)
            if pane is not None:
                return self._box(f"edit {display}", pane)
            return self._format_diff(body, max_lines=220, title=f"edit {display}")
        if isinstance(result, str) and result.strip():
            return self._format_diff(result.strip(), max_lines=20)
        return ""

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

    def _output_cols(self) -> int:
        """Current viewport width, or a sane default when not on a TUI."""
        try:
            layout = getattr(self, "layout", None)
            cols = getattr(layout, "_cols", 0)
            if layout is not None and getattr(layout, "active", False) and cols >= 30:
                return int(cols)
        except Exception:
            pass  # duck-typed bridge in an unexpected shape; use the fallback
        return self._OUTPUT_COLS_FALLBACK

    def _row_cost(self, line: str) -> int:
        """Approx wrapped screen rows one line will occupy inside a box."""
        cols = max(1, self._output_cols())
        width = visible_len(line) + 2  # '│ ' gutter
        return max(1, (width + cols - 1) // cols)

    def _rows_of(self, lines: list[str]) -> int:
        return sum(self._row_cost(ln) for ln in lines)

    def _head_lines(self, lines: list[str], budget: int) -> tuple[list[str], int]:
        """Take from the start of a stream until the row budget is spent."""
        shown: list[str] = []
        used = 0
        for ln in lines:
            cost = self._row_cost(ln)
            if used + cost > budget and shown:
                break
            shown.append(ln)
            used += cost
        return shown, len(lines) - len(shown)

    def _box(self, title: str, body: list[str]) -> str:
        """Assemble a titled box with '│ ' gutter rows."""
        out = [self.style._wrap(theme.HAIR, "┌ ") + self.style._wrap(theme.BONE, title)]
        out.extend(body)
        out.append(self.style._wrap(theme.HAIR, "└" + "─" * 38))
        return "\n".join(out)

    def _row(self, ln: str) -> str:
        """One styled '│ ' gutter row inside a tool-output box."""
        if ln.startswith("exit_code:"):
            code = -1
            try:
                code = int(ln.split(":", 1)[1].split()[0])
            except (ValueError, IndexError):
                pass  # malformed exit_code line: keep the error colour
            color = theme.SAGE if code == 0 else theme.EMBER
            return self.style._wrap(color, "│ " + ln)
        if ln.startswith(("stdout:", "stderr:", "log:", "Note:")):
            return self.style._wrap(theme.FAINT, "│ " + ln)
        if ln.startswith("+") and not ln.startswith("+++"):
            return self.style._wrap(theme.DIFF_ADD, "│ " + ln)
        if ln.startswith("-") and not ln.startswith("---"):
            return self.style._wrap(theme.DIFF_REMOVE, "│ " + ln)
        if ln.startswith(("@@", "index ", "diff --git", "--- ", "+++ ")):
            return self.style._wrap(theme.FAINT, "│ " + ln)
        return "│ " + ln

    # ---- before/after diff panes ---------------------------------------
    #
    # Structured unified diffs (agent edits, /diff, git-diff boxes) render
    # as two stacked panes per hunk instead of a +/- line stream: first
    # the before-state with removed lines in a soft dusty red, then the
    # after-state with added lines in a soft sage green - text colour
    # only, no backgrounds. A context line exists on both sides, so it is
    # shown only once - in the pane whose change sits nearest - which
    # keeps each pane anchored without doubling the code.

    def _pane_row(self, body: str, code: str | None) -> str:
        """One '│ ' gutter row; *code* colours the changed text softly."""
        gutter = self.style._wrap(theme.HAIR, "│ ")
        if not code:
            return gutter + body
        return gutter + self.style._wrap(code, body)

    def _pane_chip(self, label: str) -> str:
        """A small 'old' / 'new' marker row that opens a pane."""
        return self.style._wrap(theme.HAIR, "│ ") + self.style._wrap(theme.ASH, label)

    def _diff_pane_rows(self, diff_text: str, max_lines: int = 60) -> list[str] | None:
        """Styled old/new pane rows for a unified diff.

        Returns None when *diff_text* is not a parseable unified diff (no
        hunks, or foreign content before the first hunk), so callers can
        fall back to the plain diff renderer.
        """
        groups: list[tuple[str | None, str | None, list[list[str]]]] = []
        cur: tuple[str | None, str | None, list[list[str]]] | None = None
        hunk: list[str] | None = None
        seen_hunk = False
        for ln in diff_text.splitlines():
            if ln.startswith("--- "):
                cur = [ln[4:].strip(), None, []]
                groups.append(cur)
                hunk = None
            elif ln.startswith("+++ "):
                if cur is None:
                    cur = [None, None, []]
                    groups.append(cur)
                cur[1] = ln[4:].strip()
            elif ln.startswith("@@"):
                if cur is None:
                    cur = [None, None, []]
                    groups.append(cur)
                hunk = []
                cur[2].append(hunk)
                seen_hunk = True
            elif hunk is not None and ln[:1] in (" ", "-", "+"):
                hunk.append(ln)
            elif not seen_hunk and ln and not ln.startswith(
                ("diff ", "index ", "new file", "deleted file", "old mode", "new mode", "similarity ", "rename ", "Binary ")
            ):
                # Foreign content before any hunk (command output, notes):
                # this is not a diff we should pane-ify.
                return None
        if not any(hs for _, _, hs in groups):
            return None

        def _file_base(label: str | None) -> str | None:
            """'a/src/x.py' -> 'src/x.py'; 'x.py (after)' -> 'x.py'."""
            if not label:
                return None
            base = label.replace("\\", "/")
            if base.startswith(("a/", "b/")):
                base = base[2:]
            for suffix in (" (before)", " (after)"):
                if base.endswith(suffix):
                    base = base[: -len(suffix)]
            return base or None

        rows: list[str] = []
        for old_lbl, new_lbl, hunks in groups:
            # A per-file chip before a group's first hunk. Agent-edit diffs
            # already name the file in their box title (labels end in
            # "(before)"), so only git-style output gets the chip.
            file_base = _file_base(old_lbl or new_lbl)
            git_style = not ((old_lbl or "").endswith(" (before)") and (new_lbl or "").endswith(" (after)"))
            if file_base and git_style:
                rows.append(self._pane_chip(file_base))
            for hunk_lines in hunks:
                seq: list[tuple[str, str]] = []
                for ln in hunk_lines:
                    seq.append((ln[0], ln[1:]))
                minus_idx = [i for i, (kind, _) in enumerate(seq) if kind == "-"]
                plus_idx = [i for i, (kind, _) in enumerate(seq) if kind == "+"]
                old: list[tuple[str, bool]] = []
                new: list[tuple[str, bool]] = []
                for i, (kind, body) in enumerate(seq):
                    if kind == "-":
                        old.append((body, True))
                    elif kind == "+":
                        new.append((body, True))
                    else:
                        # Context is identical on both sides: show it once,
                        # in whichever pane holds the change nearest it.
                        d_old = min((abs(i - j) for j in minus_idx), default=10**9)
                        d_new = min((abs(i - j) for j in plus_idx), default=10**9)
                        if d_old <= d_new:
                            old.append((body, False))
                        else:
                            new.append((body, False))
                if old:
                    rows.append(self._pane_chip("old"))
                    rows.extend(self._pane_row(b, theme.DIFF_REMOVE if changed else None) for b, changed in old)
                if new:
                    rows.append(self._pane_chip("new"))
                    rows.extend(self._pane_row(b, theme.DIFF_ADD if changed else None) for b, changed in new)
        total = len(rows)
        if total > max_lines:
            rows = rows[:max_lines]
            rows.append(
                self.style._wrap(theme.HAIR, "│ ")
                + self.style._wrap(theme.FAINT, f"… {total - max_lines} more diff lines")
            )
        return rows

    def _render_diff_pages(self, title: str, rows: list[str], lead: list[str] | None = None) -> str:
        """Box for pre-styled diff-pane rows, paged like other tool boxes."""
        lead_rows = [self._row(ln) for ln in (lead or [])]
        if self._rows_of(lead_rows) + self._rows_of(rows) <= self._TOOL_OUTPUT_BUDGET_ROWS:
            return self._box(title, lead_rows + rows)
        preview, _ = self._head_lines(rows, self._READ_PAGE_ROWS)
        remaining = rows[len(preview):]
        if remaining:
            with self._pager_lock:
                self._pending_pages.append((title, remaining))
                self._pending_styled.append(True)
        shown = lead_rows + preview
        shown.append(
            self.style.dim(
                f"│ … {len(remaining)} more rows — press Enter (empty prompt) to page through the diff"
            )
        )
        return self._box(title, shown)

    def _render_paged_box(self, title: str, content: list[str], lead: list[str] | None = None) -> str:
        """Box for any tool output that may exceed a screenful.

        Fits the budget → shown whole. Overflows → a compact first page
        and the remainder queued (title + lines) for the empty-Enter
        pager, so nothing is lost and the viewport is never flooded
        mid-run - reads, commands, diffs and background logs alike.
        """
        lead_rows = lead or []
        if self._rows_of(lead_rows) + self._rows_of(content) <= self._TOOL_OUTPUT_BUDGET_ROWS:
            rows = [self._row(ln) for ln in lead_rows] + [self._row(ln) for ln in content]
            return self._box(title, rows)
        preview, _ = self._head_lines(content, self._READ_PAGE_ROWS)
        remaining = content[len(preview):]
        if remaining:
            with self._pager_lock:
                self._pending_pages.append((title, remaining))
                self._pending_styled.append(False)
        rows = [self._row(ln) for ln in lead_rows] + [self._row(ln) for ln in preview]
        rows.append(
            self.style.dim(
                f"│ … {len(remaining)} more lines — press Enter (empty prompt) to page through the output"
            )
        )
        return self._box(title, rows)

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

    def expand_mentions(self, text: str) -> tuple[str, list[str]]:
        """Turn ``@path`` tokens into real context the model can see.

        Keeps the operator's wording intact and appends an "Attached
        context" block, which is what every mainstream agent CLI does and
        what the model already understands. Unknown references are left
        alone and reported rather than silently dropped.
        """
        # Normalize full-width variants before matching so ＠ and ／ work
        text_norm = text.replace("＠", "@").replace("／", "/")
        tokens = MENTION_RE.findall(text_norm)
        if not tokens:
            # Also try finding full-width mentions directly if normal found none
            tokens = MENTION_RE.findall(text)
            if not tokens:
                return text, []

        # Normalize: a root given with forward slashes compares unequal to
        # normpath output on Windows, which made every mention "no match".
        # Realpath (not just abspath) so the containment checks below and
        # in _resolve_mention measure against the same canonical root.
        root = os.path.realpath(os.path.abspath(self.sandbox.root))
        blocks: list[str] = []
        attached: list[str] = []
        total = 0
        seen: set[str] = set()
        budget_note_shown = False

        for token in tokens:
            # Strip surrounding quotes and trailing punctuation that is
            # sentence punctuation, not part of the path.
            raw = token.strip().strip("'\"`")
            trimmed = raw.rstrip(_MENTION_TRIM)
            # Also handle "@\"src/app.py\"" style where quotes were part of token
            trimmed = trimmed.strip("'\"`")
            if not trimmed:
                continue
            if total >= MAX_TOTAL_ATTACH_CHARS:
                # Say it once, then stop scanning: one note per skipped
                # mention just spams the transcript.
                if not budget_note_shown:
                    budget_note_shown = True
                    self._note("attachment budget reached - remaining mentions skipped")
                break
            dedup_key = trimmed.lower() if os.name == "nt" else trimmed
            if dedup_key in seen:
                continue
            seen.add(dedup_key)
            # Try several variations to be forgiving
            candidates_to_try = [trimmed]
            if trimmed != token:
                candidates_to_try.append(token.strip("'\"`").rstrip(_MENTION_TRIM))
            # Also try without leading ./ if present
            if trimmed.startswith("./"):
                candidates_to_try.append(trimmed[2:])
            if trimmed.startswith(".\\"):
                candidates_to_try.append(trimmed[2:])
            paths: list[str] = []
            for cand in candidates_to_try:
                paths = self._resolve_mention(cand, root)
                if paths:
                    break
            if not paths:
                # Show the trimmed form in the note so the operator sees
                # what was actually tried, not the raw token with punctuation.
                self._note(f"no match for @{trimmed} (tried {candidates_to_try[0]!r})")
                continue
            for rel in paths:
                if total >= MAX_TOTAL_ATTACH_CHARS:
                    # Say it once, then stop scanning: one note per
                    # skipped mention just spams the transcript.
                    if not budget_note_shown:
                        budget_note_shown = True
                        self._note("attachment budget reached - remaining mentions skipped")
                    break
                full = os.path.join(root, rel)
                # Re-validate at read time: the path could have been
                # swapped for a symlink since the mention was resolved.
                # Resolve again so the containment check sits as close
                # to the open as possible, then render the canonical
                # path itself. Residual TOCTOU window: the symlink could
                # still be swapped between this check and the open below.
                # No dirfd-based read exists on Windows; accepted because
                # the model already controls the workspace contents.
                real = os.path.realpath(full)
                if real != root and not real.startswith(root + os.sep):
                    continue
                block = (
                    self._render_listing(rel, real)
                    if os.path.isdir(real)
                    else self._render_file(rel, real)
                )
                if not block:
                    continue
                blocks.append(block)
                attached.append(rel)
                total += len(block)

        if not blocks:
            return text, []
        return text + "\n\nAttached context:\n\n" + "\n\n".join(blocks), attached

    def _resolve_mention(self, token: str, root: str) -> list[str]:
        """Resolve one mention to workspace-relative paths. Escapes refused."""
        root = os.path.realpath(os.path.abspath(root))
        # Robust trimming: quotes and trailing punctuation, and leading ./
        token = token.strip().strip("'\"`").rstrip(_MENTION_TRIM).strip("'\"`")
        if not token:
            return []
        # Normalize separators to the host's convention before probing.
        candidate = token.replace("/", os.sep).replace("\\", os.sep)
        if "*" in token:
            # Normalize pattern for glob: use forward slashes for root_dir glob
            # which expects POSIX-style patterns on all platforms.
            cand_posix = token.replace("\\", "/").lstrip("/")
            hits = sorted(glob.glob(cand_posix, root_dir=root, recursive=True))
            valid: list[str] = []
            for hit in hits:
                full_hit = os.path.realpath(os.path.join(root, hit))
                if not (full_hit == root or full_hit.startswith(root + os.sep)):
                    continue
                if os.path.isfile(os.path.join(root, hit)):
                    valid.append(hit)
                if len(valid) >= MAX_GLOB_HITS:
                    break
            return valid
        full = os.path.realpath(os.path.join(root, candidate))
        # Never read outside the workspace, however the path was written.
        if full != root and not full.startswith(root + os.sep):
            return []
        return [os.path.relpath(full, root)] if os.path.exists(full) else []

    @staticmethod
    def _render_file(rel: str, full: str) -> str:
        try:
            with open(full, "r", encoding="utf-8", errors="replace") as handle:
                content = handle.read(MAX_ATTACH_CHARS + 1)
        except OSError:
            return ""
        truncated = len(content) > MAX_ATTACH_CHARS
        body = content[:MAX_ATTACH_CHARS].rstrip()
        if truncated:
            body += "\n* [truncated]"
        return f"* @{rel.upper()} *\n{body}"

    @staticmethod
    def _render_listing(rel: str, full: str) -> str:
        try:
            entries = sorted(os.listdir(full))[:MAX_LISTING_ENTRIES]
        except OSError:
            return ""
        lines = [f"* @{rel.upper()} ({len(entries)} entries) *"]
        for entry in entries:
            kind = "DIR " if os.path.isdir(os.path.join(full, entry)) else "FILE"
            lines.append(f"{kind} {entry}")
        return "\n".join(lines)

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

    def set_goal(self, text: str) -> None:
        self.goal = text.strip()
        if not self.goal:
            return
        self._print(self.style.dim(f"  goal set: {self.goal}"))
        self._print(self.style.dim("  /goal to check it · /goal done to clear it"))

    def show_goal(self) -> None:
        if not self.goal:
            self._print(self.style.dim("  no goal set - /goal <what you want done>"))
            return
        self._print(f"  {self.style.bold('goal')} {self.goal}")
        for note in self.goal_notes:
            self._print(self.style.dim(f"    · {note}"))
        if not self.goal_notes:
            self._print(self.style.dim("    (no notes - /goal note <text> to add one)"))

    def clear_goal(self, reason: str = "") -> None:
        if not self.goal:
            self._print(self.style.dim("  no goal set"))
            return
        finished = self.goal
        self.goal = ""
        self.goal_notes = []
        self._print(self.style.dim(f"  goal cleared: {finished}"))
        if reason:
            self._print(self.style.dim(f"  {reason}"))

    def add_goal_note(self, text: str) -> None:
        if not self.goal:
            self._print(self.style.dim("  set a goal first: /goal <what you want done>"))
            return
        self.goal_notes.append(text.strip())
        self._print(self.style.dim(f"  noted ({len(self.goal_notes)} on this goal)"))

    def _check_goal_completion(self, result: "RunResult | None") -> None:
        """Notice an agent that declared the goal met.

        The agent cannot clear the goal itself - only report - so a wrong
        claim costs nothing but a line the operator can ignore.
        """
        if not self.goal or result is None or not result.final_message:
            return
        if "GOAL COMPLETE" not in result.final_message.upper():
            return
        self._print(
            self.style.dim("  the agent reports the goal is met - /goal done to clear it")
        )

    # ---- todos ----------------------------------------------------------

    def _normalise_todo(self, text: str) -> str:
        return re.sub(r"\s+", " ", text.strip()).casefold()

    def _find_todo(self, query: str, open_only: bool = True) -> int | None:
        """Resolve a numbered item (1-based) or a text match to an index.

        Text must match an item's whole text (after normalising
        whitespace and case), never a fragment: a phrase that merely
        sits inside a longer item would check the wrong thing off. To
        pick among similar items or reach a done one, use the number
        shown by /todo. With ``open_only`` (the default) done items are
        never auto-selected - the operator explicitly re-numbers an item
        to reopen it. Removal passes ``open_only=False`` because a done
        item still needs to be findable to drop.
        """
        query = query.strip()
        if query.isdigit():
            index = int(query) - 1
            return index if 0 <= index < len(self.todos) else None
        target = self._normalise_todo(query)
        for index, item in enumerate(self.todos):
            if (not open_only or not item["done"]) and self._normalise_todo(item["text"]) == target:
                return index
        return None

    def add_todo(self, text: str) -> None:
        text = text.strip()
        if not text:
            self._print(self.style.dim("  usage: /todo add <what needs doing>"))
            return
        self.todos.append({"text": text, "done": False})
        self._print(f"  {self.style.brand(str(len(self.todos)) + '.')} {text}")
        self._print(self.style.dim("  /todo to see the list · /todo done <n> when it's done"))

    def show_todos(self) -> None:
        if not self.todos:
            self._print(self.style.dim("  no todos - /todo add <what needs doing>"))
            return
        for index, item in enumerate(self.todos, 1):
            marker = self.style.ash("[ ]") if not item["done"] else self.style.hair("[x]")
            body = item["text"] if not item["done"] else self.style.strike(item["text"])
            self._print(f"  {index:>2} {marker} {body}")
        open_count = sum(1 for t in self.todos if not t["done"])
        if open_count:
            self._print("")
            self._print(self.style.dim(f"  {open_count} open · /todo done <n> to check one off · /todo rm <n> to drop one"))

    def mark_todo_done(self, query: str) -> bool:
        """Mark an item done by 1-based number or text match. Returns True when found."""
        index = self._find_todo(query, open_only=True)
        if index is None:
            # Distinguish "not in the list at all" from "already done".
            existing = self._find_todo(query, open_only=False)
            if existing is not None:
                self._print(self.style.dim(f"  already done: {self.todos[existing]['text']}"))
                return False
            self._print(self.style.dim("  no open todo matches - /todo lists them"))
            return False
        item = self.todos[index]
        if item["done"]:
            self._print(self.style.dim(f"  already done: {item['text']}"))
            return False
        item["done"] = True
        self._print(self.style.dim(f"  done: {item['text']}"))
        remaining = sum(1 for t in self.todos if not t["done"])
        if not remaining:
            self._print(self.style.dim("  all todos done - /todo clear to drop the list"))
        return True

    def rm_todo(self, query: str) -> bool:
        """Drop an item by 1-based number or text match. Returns True when found."""
        index = self._find_todo(query, open_only=False)
        if index is None:
            self._print(self.style.dim("  no todo matches - /todo lists them"))
            return False
        removed = self.todos.pop(index)["text"]
        self._print(self.style.dim(f"  removed: {removed}"))
        return True

    def clear_todos(self) -> None:
        if not self.todos:
            self._print(self.style.dim("  no todos to clear"))
            return
        count = len(self.todos)
        self.todos = []
        self._print(self.style.dim(f"  cleared {count} todos"))

    def _todo_status_snippet(self) -> str:
        """Styled open-item count for the border row while a turn runs.

        Deprecated: no border row exists since the frame shims were
        removed; kept because the test suite still exercises it.
        Empty string when nothing is open, so the spinner row only gains
        the ``[ ] N open`` readout when the checklist actually has work
        left - and it drains live as the agent checks items off.
        """
        open_count = sum(1 for t in self.todos if not t["done"])
        if not open_count:
            return ""
        plural = "" if open_count == 1 else "s"
        return self.style.dim(f"[ ] {open_count} open item{plural}")

    def _check_todo_completion(self, result: "RunResult | None") -> None:
        """Apply the agent's TODO reports from its final message.

        Two reports, each on its own line:

        - ``TODO DONE: <text>`` checks an open item off. Matching is
          exact after normalising whitespace, so the agent echoing an
          item's text is the only thing that checks it off; a paraphrase
          does nothing and the operator can mark it with /todo done <n>.
        - ``TODO ADD: <text>`` appends a new item the agent discovered
          along the way (follow-up work a task turned up). Deduplicated
          against the list verbatim so repeated reports do not stack.

        The agent can add and complete items, but never remove or edit
        them - the operator owns the list.

        Reports already applied inline by the streaming hook are skipped
        here: their state change happened as the reply streamed and their
        note is already on screen, so the end-of-turn pass only handles
        what the stream never saw (non-streamed replies, reports whose
        line fell outside the stream path).
        """
        if result is None or not result.final_message:
            return
        # Two passes, adds first: an item added and completed in the same
        # message must check off regardless of which line came first.
        reports = []
        for line in result.final_message.splitlines():
            head, _, rest = line.partition(":")
            head = head.strip().upper()
            reported = self._normalise_todo(rest)
            if not reported:
                continue
            if head in ("TODO DONE", "TODO ADD"):
                reports.append((head, rest.strip(), reported))
        # Adds first, then completions; reports already applied inline
        # by the streaming hook are skipped inside _apply_todo_report.
        for head, text, reported in reports:
            if head != "TODO ADD":
                continue
            if self._apply_todo_report(head, text, reported):
                self._print(self.style.dim(f"  todo added ({len(self.todos)}): {text}"))
        for head, text, reported in reports:
            if head != "TODO DONE":
                continue
            if self._apply_todo_report(head, text, reported):
                self._print(self.style.dim(f"  checked off: {text}"))

    def _handle_stream_todo_report(self, line: str) -> str:
        """Stream hook: apply a TODO report the moment its line arrives.

        The raw ``TODO ADD: …`` protocol text never reaches the screen.
        Instead the change is applied to the live list and a quiet note is
        returned in its place, so the operator watches the checklist grow
        and drain inside the streamed reply rather than reading a marker
        line or waiting for the turn to end.
        """
        head, _, text = line.partition(":")
        reported = self._normalise_todo(text)
        if not reported:
            return ""
        # Normalise the case: the report pattern is case-insensitive, so
        # "todo done: ..." must reach the applier, which compares exact
        # uppercase heads.
        applied = self._apply_todo_report(head.strip().upper(), text.strip(), reported)
        if not applied:
            # Already applied inline earlier in this stream (or a done
            # item re-reported): return empty so the renderer swallows
            # the raw protocol line instead of showing it again.
            return ""
        # ASCII checkboxes, dimmed: an open item carries the [ ] marker
        # (the same mark /todo shows), a finished one the [x] with
        # the text struck through - so a note reads exactly like a row of
        # the checklist, only quieter than the reply around it.
        if head.strip().upper() == "TODO DONE":
            return self.style.dim(
                self.style.hair("[x]") + " " + self.style.strike(text.strip())
            )
        open_count = sum(1 for t in self.todos if not t["done"])
        plural = "" if open_count == 1 else "s"
        return self.style.dim(
            "[ ] " + text.strip()
            + f" ({open_count} open item{plural})"
        )


    def _apply_todo_report(self, head: str, text: str, reported: str) -> bool:
        """Apply one TODO ADD / TODO DONE report. Shared by both paths.

        Returns True when a change was applied (an item added or checked
        off). Callers decide how to announce it - the stream path returns
        a styled note inline, the end-of-turn path prints after the turn.
        Either way the state change happens once: reports already applied
        inline are recorded in ``_turn_todo_reports`` so the end-of-turn
        pass never re-adds, re-checks, or re-announces them.
        """
        if (head, reported) in self._turn_todo_reports:
            return False  # already applied inline while the reply streamed
        if head == "TODO ADD":
            if any(self._normalise_todo(t["text"]) == reported for t in self.todos):
                return False  # already tracked - do not stack duplicates
            self.todos.append({"text": text, "done": False})
            self._turn_todo_reports.append(("TODO ADD", reported))
            return True
        if head == "TODO DONE":
            for item in self.todos:
                if item["done"]:
                    continue
                if self._normalise_todo(item["text"]) == reported:
                    item["done"] = True
                    self._turn_todo_reports.append(("TODO DONE", reported))
                    return True
        return False

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
            max_steps=self.max_steps,
            on_delta=self._on_delta,
            context=self.context,
            abort=self._abort,
            approver=self.approvals,
            on_tool_result=self._on_tool_observation,
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

    def _record_usage(self, result: RunResult) -> None:
        self.totals["turns"] += 1
        tin = int(result.metrics.get("tokens_in", 0))
        tout = int(result.metrics.get("tokens_out", 0))
        cache = int(result.metrics.get("cache_hit", 0))
        # Fallback to estimated tokens when provider gave no usage or all zeros.
        # This keeps /cost and the top-bar CACHE useful even when the
        # endpoint does not return usage (e.g. after stream_options downgrade).
        if tin == 0 and tout == 0:
            est_in = self.context.tokens
            est_out = max(1, len(result.final_message) // 4) if result.final_message else 0
            # If cache was reported but prompt was 0, use cache as at least part of input
            if cache > 0 and est_in == 0:
                est_in = cache
            # Only estimate if we have something to estimate from
            if est_in > 0 or est_out > 0:
                if tin == 0:
                    tin = est_in
                    result.metrics["tokens_in"] = tin
                if tout == 0:
                    tout = est_out
                    result.metrics["tokens_out"] = tout
                result.metrics["usage_estimated"] = 1
        # Cache-hit tokens also ride inside tokens_in when the provider
        # reports them, and they are re-counted in cache_hit below; the
        # input total keeps tin as reported so the same tokens are not
        # counted twice in the /cost figures.
        self.totals["tokens_in"] += tin
        self.totals["tokens_out"] += tout
        self.totals["tool_errors"] += int(result.metrics.get("tool_errors", 0))
        self.totals["cache_hit"] += cache
        # Record per-turn metrics for trend analysis.
        rate = min(100, cache * 100 // tin) if tin > 0 else 0
        self.turn_history.append({
            "turn": self.totals["turns"],
            "tokens_in": tin,
            "tokens_out": tout,
            "cache_hit": cache,
            "cache_rate": rate,
        })
        # Wire CACHE instantly — top bar shows hit rate without waiting for next chrome redraw
        if self.layout is not None and getattr(self.layout, "active", False):
            try:
                self.layout.draw_chrome()
            except Exception:
                pass  # duck-typed bridge: chrome redraw is cosmetic, never fatal

    def _usage_line(self, result: RunResult) -> str:
        tin = int(result.metrics.get("tokens_in", 0))
        tout = int(result.metrics.get("tokens_out", 0))
        cache = int(result.metrics.get("cache_hit", 0))
        steps = result.steps_used
        elapsed = time.monotonic() - self._turn_started
        elapsed_str = _format_elapsed(elapsed)
        cache_bit = f" · {_short(cache)} CACHED" if cache else ""
        if not tin and not tout:
            return f"{elapsed_str} · {steps} STEP{cache_bit} · CTX {_short(self.context.tokens)}"
        return (
            f"{elapsed_str} · {steps} STEP · "
            f"I/O {_short(tin)} / {_short(tout)}{cache_bit} · CTX {_short(self.context.tokens)}"
        )

    def _record_memory(self, task: dict, result: RunResult) -> None:
        final = (result.final_message or "").strip().replace("\n", " ")[:300]
        entry = (
            f"- {time.strftime('%Y-%m-%d %H:%M')} | {task['task_id']} | "
            f"{result.stopped_reason}: {final} | status=active"
        )
        # Search-before-write: a near-duplicate is skipped entirely, and
        # a new entry on a topic already covered marks the old one
        # superseded instead of stacking copies (memory rot).
        existing = read_raw_tail(self.memory_path)
        action, updated = plan_memory_write(existing, entry)
        if action == "skip":
            return
        if action == "supersede":
            rewrite_memory(self.memory_path, updated, new_entry=entry)
            return
        ok = append_memory(
            self.memory_path,
            entry,
        )
        if not ok:
            self._print("(memory write skipped: store busy or unwritable)")

    def _report_changes(self) -> None:
        """Announce files the agent touched, newest first, once each."""
        changed = getattr(self.sandbox, "changed", set()) or set()
        fresh = sorted(changed - self.reported_changes)
        if not fresh:
            return
        self.reported_changes.update(fresh)
        shown = fresh[:8]
        more = "" if len(fresh) <= 8 else f" (+{len(fresh) - 8} more)"
        self._print(f"  {self.style.dim('changed: ' + ', '.join(shown) + more)}")

    # ---- context management ----------------------------------------------

    def _auto_compact(self) -> None:
        # merge_defaults rejects non-integer values, but stay defensive:
        # a bad value must disable compaction, not crash the first turn.
        raw = self.config.get("auto_compact_tokens", 0) or 0
        limit = raw if isinstance(raw, int) and not isinstance(raw, bool) and raw > 0 else 0
        if limit and self.context.tokens > limit:
            self._print(self.style.dim(f"  (context ~{self.context.tokens} tokens, compacting)"))
            self.compact()

    def compact(self) -> bool:
        """Summarise the conversation, keeping the system prompt and summary."""
        if len(self.context.messages) <= 3:
            return False
        transcript = _transcript(self.context.messages)
        request = (
            "Summarise this coding session so work can continue without the "
            "original transcript. Cover: the goal, every file created or "
            "modified and why, commands that were run and their outcome, any "
            "error encountered and how it was resolved, and the exact state "
            "left off at. Be dense and concrete; no preamble.\n\n"
            f"{transcript}"
        )
        try:
            response = self.llm.chat(
                [{"role": "user", "content": request}], tools=None, on_delta=None
            )
        except HarnessError as exc:
            self._print(self.style.ember(f"  compaction failed: {exc}"))
            return False
        summary = (response.content or "").strip()
        if not summary:
            return False
        before = self.context.tokens
        self.context.replace_body(
            [
                {
                    "role": "user",
                    "content": "Earlier in this session (compressed summary):\n" + summary,
                }
            ]
        )
        self._print(
            self.style.dim(f"  compacted: ~{before} -> ~{self.context.tokens} tokens")
        )
        return True

    # ---- session persistence ---------------------------------------------

    def save_session(self, path: str) -> bool:
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
            with open(path, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
        except (OSError, TypeError, ValueError) as exc:
            self._print(self.style.ember(f"  save failed: {exc}"))
            return False
        self._print(self.style.dim(f"  saved {len(self.context.messages)} messages to {path}"))
        return True

    def load_session(self, path: str) -> bool:
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

    # ---- resumable sessions ----------------------------------------------

    def autosave(self) -> None:
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

    def model_name(self) -> str:
        return str(self.config.get("llm", {}).get("model", "") or "")

    def resume_session(self, name: str) -> bool:
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
                    content = " ".join(part.get("text","") for part in content if isinstance(part, dict) and part.get("type")=="text") or str(content)
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
                    calls = ", ".join((c.get("function") or {}).get("name","?") for c in msg.get("tool_calls") or [])
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

    def _is_same_workspace(self, saved_ws: str) -> bool:
        try:
            cur = os.path.realpath(os.path.abspath(self.workspace or ""))
            saved = os.path.realpath(os.path.abspath(saved_ws or ""))
            if os.name == "nt":
                return cur.lower() == saved.lower()
            return cur == saved
        except (OSError, ValueError):
            # Unresolvable paths (deleted cwd, bad chars): compare raw.
            return (saved_ws or "") == (self.workspace or "")

    def show_sessions(self) -> None:
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

    def pick_session(self) -> bool:
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
        chosen = _menu(
            self,
            "Resume a session",
            options,
            hint="up/down move · Enter resume · Esc cancel",
        )
        if not chosen:
            return False
        return self.resume_session(chosen)

    # ---- inspection commands ---------------------------------------------

    def _git(self, *args: str) -> str:
        try:
            completed = subprocess.run(
                ["git", *args], cwd=self.workspace,
                capture_output=True, text=True, timeout=30,
            )
        except (OSError, subprocess.SubprocessError):
            return ""
        return completed.stdout if completed.returncode == 0 else ""

    def _git_ok(self, *args: str) -> bool:
        """Run git and report success; stdout alone cannot distinguish it."""
        try:
            completed = subprocess.run(
                ["git", *args], cwd=self.workspace,
                capture_output=True, text=True, timeout=30,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return completed.returncode == 0

    def show_workspace(self) -> None:
        root = self.sandbox.root
        self._print(f"workspace: {root}")
        try:
            entries = sorted(os.listdir(root))[:50] if os.path.isdir(root) else []
        except OSError as exc:
            self._print(self.style.ember(f"  cannot list workspace: {exc}"))
            return
        for entry in entries:
            try:
                full = os.path.join(root, entry)
                self._print(("> " if os.path.isdir(full) else "") + entry)
            except OSError:
                self._print(entry)
        if not entries:
            self._print("(empty)")

    def show_diff(self) -> None:
        stat = self._git("diff", "--stat")
        status = self._git("status", "--short")
        if not stat and not status:
            self._print("(no uncommitted changes)")
            return
        if status:
            self._print(status.rstrip())
        if stat:
            self._print(stat.rstrip())
        diff = self._git("diff")
        if diff:
            # Same before/after panes as the live edit previews and the
            # agent's git-diff boxes.
            rows = self._diff_pane_rows(diff, max_lines=400)
            if rows is not None:
                self._print(self._box("git diff", rows))
                return
            lines = diff.splitlines()
            cap = 400  # plain-text fallback: same cap as the boxed panes
            self._print("\n".join(lines[:cap]))
            if len(lines) > cap:
                self._print(f"... ({len(lines) - cap} more lines)")

    def undo_changes(self) -> None:
        status = self._git("status", "--porcelain")
        if not status:
            self._print("(nothing to undo - working tree is clean)")
            return
        self._print(f"{len(status.strip().splitlines())} file(s) would be reverted:")
        self._print(status.rstrip())
        try:
            if self.ui is not None:
                answer = self.ui.ask_line('type "yes" to revert tracked changes').strip()
            else:
                answer = input('type "yes" to revert tracked changes: ').strip()
        except (KeyboardInterrupt, EOFError):
            self._print("cancelled")
            return
        if answer.lower() != "yes":
            self._print("cancelled")
            return
        self._print("reverted" if self._git_ok("checkout", "--", ".") else "revert failed")

    def show_cost(self, compact: bool = False, as_json: bool = False) -> None:
        t = self.totals
        tokens_in = t['tokens_in']
        tokens_out = t['tokens_out']
        cache_hit = t['cache_hit']
        context_tokens = self.context.tokens
        context_chars = self.context.chars

        # Derived metrics.
        cache_rate = (cache_hit * 100 // tokens_in) if tokens_in > 0 else 0
        cache_saved = cache_hit // 2  # ~50% discount

        if as_json:
            payload = {
                "turns": t['turns'],
                "tokens_in": tokens_in,
                "tokens_out": tokens_out,
                "cache_hit": cache_hit,
                "cache_rate": cache_rate,
                "cache_saved": cache_saved,
                "tool_errors": t['tool_errors'],
                "context_tokens": context_tokens,
                "context_chars": context_chars,
            }
            if self.turn_history:
                payload["turn_history"] = self.turn_history[-10:]
            self._print(json.dumps(payload, indent=2))
            return

        self._print(f"turns        {t['turns']}")
        self._print(f"tokens in    {tokens_in}")
        self._print(f"tokens out   {tokens_out}")
        # Cache analytics: show hit rate and estimated savings.
        if tokens_in > 0 and cache_hit > 0:
            self._print(f"cache hit    {cache_hit} ({cache_rate}% of prompt)")
            self._print(f"cache saved  {cache_saved} tokens (~50% discount)")
        else:
            self._print(f"cache hit    {cache_hit}")
        self._print(f"tool errors  {t['tool_errors']}")
        self._print(f"context      ~{context_tokens} tokens ({context_chars} chars)")
        # Per-turn cache trend (last 5 turns) — skipped in compact mode.
        if not compact and self.turn_history:
            recent = self.turn_history[-5:]
            self._print("")
            self._print("  turn  in      out     cached  rate")
            self._print("  " + "─" * 40)
            for entry in recent:
                tin = entry['tokens_in']
                tout = entry['tokens_out']
                ch = entry['cache_hit']
                cr = entry['cache_rate']
                self._print(
                    f"  {entry['turn']:<6}{tin:<8}{tout:<8}{ch:<8}{cr}%"
                )

    def set_model(self, name: str, quiet: bool = False) -> None:
        self.config.setdefault("llm", {})["model"] = name
        try:
            self.llm = build_llm(self.config["llm"])
        except HarnessError as exc:
            self._print(self.style.ember(f"  could not switch model: {exc}"))
            return
        if not quiet:
            self._print(self.style.dim(f"  model is now {name}"))
        self.refresh_title()
        self._warn_if_key_missing()
        # Wire top info instantly so MODEL shows new name without waiting for next turn
        if self.layout is not None and getattr(self.layout, "active", False):
            try:
                self.layout.draw_chrome()
            except Exception:
                pass  # duck-typed bridge: chrome redraw is cosmetic, never fatal

    def set_reasoning(self, level: str, quiet: bool = False) -> None:
        """Set the thinking budget for the current model.

        Not every endpoint understands the field; the client sheds it on a
        400 and the reply simply arrives without the extra thinking.
        """
        wanted = level.strip().lower()
        if wanted in ("off", "none", ""):
            wanted = None
        elif wanted not in REASONING_EFFORTS:
            self._print(
                self.style.warn(f"  reasoning must be one of {', '.join(REASONING_EFFORTS)} or off")
            )
            return
        llm = self.config.setdefault("llm", {})
        llm["reasoning_effort"] = wanted
        try:
            self.llm = build_llm(llm)
        except HarnessError as exc:
            self._print(self.style.ember(f"  could not set reasoning: {exc}"))
            return
        if not quiet:
            self._print(
                self.style.dim(f"  reasoning is now {wanted}" if wanted else "  reasoning off")
            )
        self.refresh_title()
        if self.layout is not None and getattr(self.layout, "active", False):
            try:
                self.layout.draw_chrome()
            except Exception:
                pass  # duck-typed bridge: chrome redraw is cosmetic, never fatal

    def show_reasoning(self) -> None:
        effort = self.config.get("llm", {}).get("reasoning_effort")
        current = effort or "off"
        options = " ".join(
            f"[{e}]" if e == effort else e for e in REASONING_EFFORTS
        )
        self._print(f"  reasoning  {current}   {self.style.dim(options + '  off')}")
        self._print(
            self.style.dim(
                "  higher means more thorough and slower; ignored by models "
                "that do not reason"
            )
        )

    @property
    def endpoint_name(self) -> str:
        """Which saved endpoint the current base URL belongs to, if any."""
        llm = self.config.get("llm", {})
        return endpoint_name_for_url(llm.get("base_url", "")) or ""

    def use_endpoint(self, name: str, model: str | None = None) -> bool:
        """Point the agent at a saved endpoint. True on success."""
        entry = known_endpoints().get(name.lower())
        if entry is None:
            self._print(self.style.warn(f"  no endpoint named '{name}'"))
            self._print(self.style.dim("  add one with /model, or list them: /model"))
            return False
        llm = self.config.setdefault("llm", {})
        llm["base_url"] = entry["base_url"]
        llm["api_key_env"] = entry.get("api_key_env") or ""
        # A model name rarely survives a move between endpoints, so take
        # the first one this endpoint offers unless one was asked for.
        llm["model"] = model or (entry.get("models") or [""])[0] or llm.get("model", "")
        try:
            self.llm = build_llm(llm)
        except HarnessError as exc:
            self._print(self.style.ember(f"  could not switch endpoint: {exc}"))
            return False
        set_active(endpoint=name.lower(), model=llm.get("model", ""))
        self._print(self.style.dim(f"  endpoint is now {entry['base_url']}"))
        self.refresh_title()
        self._warn_if_key_missing()
        if self.layout is not None and getattr(self.layout, "active", False):
            try:
                self.layout.draw_chrome()
            except Exception:
                pass  # duck-typed bridge: chrome redraw is cosmetic, never fatal
        return True

    def _warn_if_key_missing(self) -> None:
        """Say so up front when the key variable is unset.

        Silence here turns into a confusing 401 three steps into a task.
        """
        llm = self.config.get("llm", {})
        key_env = llm.get("api_key_env") or ""
        if not provider_needs_key(llm.get("base_url", ""), key_env):
            return
        if os.environ.get(key_env) or has_stored(key_env):
            return
        self._print(self.style.warn(f"  warning: no key for ${key_env}"))
        self._print(
            self.style.dim(
                f"  store one with /model, or edit {settings_path()}"
            )
        )

    def _warn_if_any_key_missing(self) -> None:
        """Warn once per saved endpoint whose key variable is unset.

        Called once at startup so a saved-but-unkeyed endpoint warns
        even when it is not the active one; the active endpoint is
        covered by _warn_if_key_missing on every switch.
        """
        llm = self.config.get("llm", {})
        current_url = (llm.get("base_url") or "").rstrip("/")
        warned: set[str] = set()
        for name, entry in known_endpoints().items():
            key_env = entry.get("api_key_env") or ""
            if not provider_needs_key(entry.get("base_url", ""), key_env):
                continue
            if (entry.get("base_url") or "").rstrip("/") == current_url:
                continue  # the active endpoint warns via the per-switch check
            if key_env in warned or os.environ.get(key_env) or has_stored(key_env):
                continue
            warned.add(key_env)
            self._print(self.style.warn(f"  warning: no key for ${key_env} ({name})"))
        if warned:
            self._print(
                self.style.dim(
                    f"  store keys with /model, or edit {settings_path()}"
                )
            )

    def show_endpoints(self) -> None:
        """List what the user has configured, and where the file is."""
        llm = self.config.get("llm", {})
        current = (llm.get("base_url") or "").rstrip("/")
        known = known_endpoints()
        if not known:
            self._print(self.style.dim("  no endpoints yet - add one with /model"))
            # Name the file even here: an empty list is exactly when
            # somebody is most likely to want to type one in by hand.
            self._print(self.style.dim(f"  or add one to {settings_path()}"))
            return
        self._print(self.style.bold("  endpoints"))
        for name in sorted(known):
            entry = known[name]
            marker = "*" if entry["base_url"] == current else " "
            key_env = entry.get("api_key_env") or ""
            if not provider_needs_key(entry["base_url"], key_env):
                key_state = "no key needed"
            elif os.environ.get(key_env):
                key_state = "key in env"
            elif has_stored(key_env):
                key_state = f"stored {mask(stored_keys().get(key_env))}"
            else:
                key_state = "no key"
            count = len(entry.get("models") or [])
            model_bit = f"{count} model" + ("" if count == 1 else "s")
            tail = " · ".join(p for p in (key_state, model_bit) if p)
            self._print(
                f"  {marker} {name:<12} {entry['base_url']:<38}"
                f" {self.style.dim(tail)}"
            )
        self._print(self.style.dim("  * = current. add or switch: /model"))
        self._print(self.style.dim(f"  or edit by hand: {settings_path()}"))

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
        # Case-fold only where the filesystem is case-insensitive; on
        # POSIX a differently-cased sibling must not slip through the
        # containment check (realpath does not normalize case).
        if os.name == "nt":
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
        config["approvals"] = args.approve

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
    session = ConsoleSession(config, workspace, style)

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
            session.logger.close()
            return
        if not line:
            continue
        try:
            if not dispatch(session, line):
                session.handle(line)
        except SystemExit:
            session.logger.close()
            return


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
