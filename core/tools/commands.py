"""Command and git tools: run_command, shell_output, kill_shell, git_diff, git_reset.

- background requires an explicit boolean opt-in (background=true)
- from_offset cursor reads (shell_output)
- middle-out truncation with omitted counts + full log on disk
- honest exits (137/143, grep 1 note)
- untrusted fencing
- kill by task id/pid/port, sigterm→sigkill
- timeout 30s/600 cap, model-settable
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
import threading
import time
import uuid
from typing import Any

from core.agent.approvals import _redact_sensitive
# Process-group isolation for background tasks (the flags and tree-kill
# helper below) is shared with the sandboxes via core.procutil.
from core.procutil import POPEN_GROUP_KWARGS as _POPEN_GROUP_KWARGS
from core.procutil import kill_process_tree as _kill_task_tree
from core.sandbox import _filtered_env
from core.types import ExecResult, Sandbox
from core.types import Tool

# Background task registry — bounded to prevent unbounded growth.
# Entries are pruned after completion and max size is enforced.
_TASKS: dict[str, dict[str, Any]] = {}
_TASKS_LOCK = threading.Lock()
_TASK_COUNTER = 0
_MAX_TASKS = 100
_TASK_TTL_SECONDS = 3600  # 1 hour; completed tasks older than this are pruned

# Full-output log files written by _format_result when a command's output
# exceeds the inline budget. Each is a tempfile that nothing removes, so
# the files are tracked here and pruned on every new allocation: entries
# older than the TTL are deleted, and the registry is size-capped.
_FULL_LOG_FILES: dict[str, float] = {}
_FULL_LOG_LOCK = threading.Lock()
_FULL_LOG_TTL_SECONDS = 3600
_MAX_FULL_LOGS = 50

# Background task logs stop growing past this ceiling; a chatty command
# can run up to 600s and must not be able to fill the disk. The pumper
# writes a marker and the truncated flag is surfaced by shell_output.
_LOG_BYTE_CEILING = 10 * 1024 * 1024


def _register_full_log(path: str) -> None:
    """Track a full-output log file and prune expired/surplus ones."""
    now = time.monotonic()
    with _FULL_LOG_LOCK:
        for old_path, created in list(_FULL_LOG_FILES.items()):
            if now - created > _FULL_LOG_TTL_SECONDS:
                _FULL_LOG_FILES.pop(old_path, None)
                try:
                    os.remove(old_path)
                except OSError:
                    pass
        _FULL_LOG_FILES[path] = now
        if len(_FULL_LOG_FILES) > _MAX_FULL_LOGS:
            # Remove the oldest first; dict preserves insertion order.
            excess = len(_FULL_LOG_FILES) - _MAX_FULL_LOGS
            for old_path in list(_FULL_LOG_FILES.keys())[:excess]:
                _FULL_LOG_FILES.pop(old_path, None)
                try:
                    os.remove(old_path)
                except OSError:
                    pass


def _prune_tasks_locked() -> None:
    """Prune old completed tasks when registry grows too large."""
    now = time.monotonic()
    # First, remove expired completed tasks
    expired = [
        tid for tid, info in _TASKS.items()
        if info.get("done") and (now - info.get("end_time", info.get("start_time", now))) > _TASK_TTL_SECONDS
    ]
    for tid in expired:
        info = _TASKS.pop(tid, None)
        # Clean up log file for pruned task
        lp = (info or {}).get("log_path")
        if lp and os.path.exists(lp):
            try:
                os.remove(lp)
            except OSError:
                pass
    # If still over capacity, remove oldest completed first. A registry
    # full of running tasks is left over capacity: evicting a live entry
    # would orphan its process — unfindable by kill_shell, unreadable by
    # shell_output — which is worse than a temporarily larger registry.
    while len(_TASKS) > _MAX_TASKS:
        # Prefer to evict oldest completed task
        oldest_completed = None
        oldest_time = float("inf")
        for tid, info in _TASKS.items():
            if info.get("done"):
                t = info.get("start_time", oldest_time)
                if t < oldest_time:
                    oldest_time = t
                    oldest_completed = tid
        victim = oldest_completed
        if victim is None:
            break
        info = _TASKS.pop(victim, None)
        lp = (info or {}).get("log_path")
        if lp and os.path.exists(lp):
            try:
                os.remove(lp)
            except OSError:
                pass


def _next_task_id() -> str:
    global _TASK_COUNTER
    with _TASKS_LOCK:
        _TASK_COUNTER += 1
        return f"tsk_{_TASK_COUNTER:04d}_{uuid.uuid4().hex[:6]}"


def _finish_task(
    task_id: str,
    log_path: str,
    exit_code: int,
    note: str = "",
    timed_out: bool = False,
    duration: float = 0.0,
) -> None:
    """Write the closing block to the task log and mark the task done."""
    log_error: str | None = None
    try:
        with open(log_path, "a", encoding="utf-8", errors="replace") as f:
            if note:
                f.write(f"\n[{note}]\n")
            f.write(f"\nexit_code: {exit_code}\n")
    except Exception as exc:
        # Surface a failed log write instead of swallowing it.
        log_error = f"log write failed: {exc}"
    with _TASKS_LOCK:
        if task_id in _TASKS:
            _TASKS[task_id].update({
                "exit_code": exit_code,
                "stdout": "",
                "stderr": (note + (f"; {log_error}" if log_error else "")) or log_error or "",
                "timed_out": timed_out,
                "done": True,
                "end_time": time.monotonic(),
                "duration": duration,
                # Task done: release the Popen handle and pid so finished
                # entries cannot pin process handles.
                "process": None,
                "pid": None,
                "log_error": log_error or _TASKS[task_id].get("log_error"),
            })
    # Prune finished workspace task logs, keeping the most recent, so
    # list_dir stops exposing them.
    _prune_done_logs(log_path)


_DONE_LOG_KEEP = 5


def _prune_done_logs(log_path: str) -> None:
    """Prune completed workspace task logs, keeping the most recent.

    Only logs whose task entries are marked done are candidates, so a
    still-running background task's log is never touched.
    """
    marker = os.sep + ".mantra" + os.sep + "logs" + os.sep
    if marker not in log_path.replace("/", os.sep):
        return
    try:
        logs_dir = os.path.dirname(log_path)
        done_paths: list[tuple[float, str]] = []
        with _TASKS_LOCK:
            for info in _TASKS.values():
                if not info.get("done"):
                    continue
                lp = info.get("log_path") or ""
                if lp and os.path.dirname(lp) == logs_dir and os.path.exists(lp):
                    try:
                        done_paths.append((os.path.getmtime(lp), lp))
                    except OSError:
                        pass
        done_paths.sort(reverse=True)
        for _, old in done_paths[_DONE_LOG_KEEP:]:
            try:
                os.remove(old)
            except OSError:
                pass
    except OSError:
        pass


def _map_exit_status(result: ExecResult, command: str) -> tuple[int, str]:
    """Display exit code and its note for one finished command.

    Signal deaths map to 128+n, a timed-out run reads 143, a screen refusal
    or cap kill keeps -1, and the grep/Select-String "no matches" status 1 is
    annotated as benign. Kept separate from formatting so the honest-exit
    rules can be tested on their own.
    """
    exit_code = result.exit_code
    if exit_code == -1 and result.timed_out:
        exit_code = 143
    elif exit_code == -1:
        exit_code = -1
    elif exit_code is not None and exit_code < 0:
        exit_code = 128 - exit_code
    elif exit_code is None:
        exit_code = 128
    display_code = exit_code
    exit_note = ""
    if display_code == 137:
        exit_note = " (SIGKILL, 128+9)"
    elif display_code == 143:
        exit_note = " (SIGTERM, 128+15)"
    elif display_code == -1:
        exit_note = " (blocked/refused — not a signal death)"
    elif display_code == 128 and (os.name != "nt" or (result.exit_code is not None and result.exit_code < 0)):
        exit_note = " (signal death, 128)"
    elif display_code == 1 and re.search(r"(^|\s|;)grep(\.exe)?\b", command, re.IGNORECASE):
        exit_note = " (grep: no matches — not an error)"
    elif display_code == 1 and re.search(r"Select-String", command, re.IGNORECASE):
        exit_note = " (Select-String: no matches — not an error)"
    return display_code, exit_note


class RunCommandTool(Tool):
    name = "run_command"
    description = (
        "Execute a shell command in the sandbox workspace. "
        "For long builds use background=true to return instantly with task id. "
        "Use shell_output to read from_offset. "
        "Exit codes are honest: 137 sigkill, 143 sigterm, grep 1 is 'no matches' not error."
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "Shell command to run"},
            "timeout": {
                "type": "number",
                "description": "Seconds before kill (default 30, max 600, model-settable)",
            },
            "background": {
                "type": "boolean",
                "description": "Run in background, return task info immediately",
            },
        },
        "required": ["command"],
    }

    def execute(self, sandbox: Sandbox, command: str, timeout: float = 30.0, background: Any = None) -> str:  # type: ignore[override]
        try:
            timeout_f = float(timeout) if timeout is not None else 30.0
        except (TypeError, ValueError):
            return f"ERROR: timeout must be a number, got {timeout!r}"
        if not 0 < timeout_f <= 600:
            return "ERROR: timeout must be between 0 and 600 seconds"
        if not isinstance(command, str) or not command.strip():
            return "ERROR: command must be a non-empty string"
        if "\x00" in command:
            return "ERROR: command contains NUL bytes"
        if len(command) > 10000:
            return "ERROR: command too long"

        # Background requires an explicit boolean opt-in; anything else
        # is an error so the model learns to send the right type.
        if background is not None and not isinstance(background, bool):
            return "ERROR: background must be a boolean (true or false)"
        use_bg = bool(background)

        if use_bg:
            return self._execute_background(sandbox, command, timeout_f)

        # Foreground: use sandbox exec with caps
        result = sandbox.exec(command, timeout=timeout_f)
        return self._format_result(result, command)

    def _execute_background(self, sandbox: Sandbox, command: str, timeout: float) -> str:
        # Background execution spawns the process directly on the host, so
        # it is only safe for the host-local sandbox. Any other sandbox
        # (e.g. a container) must refuse here rather than silently escape
        # its isolation boundary.
        if getattr(sandbox, "root", None) is None or not hasattr(sandbox, "screen_command"):
            return (
                "ERROR: background execution is only supported by the host "
                "sandbox; run this command in the foreground instead"
            )
        task_id = _next_task_id()
        # Prefer workspace-private logs with owner-only permissions; avoid
        # world-writable shared temp directory. Fall back to a private
        # per-process directory with restricted permissions.
        root = getattr(sandbox, "root", None)
        if root and os.path.isdir(root):
            # Use workspace-private directory .mantra/logs if available
            candidate_dir = os.path.join(root, ".mantra", "logs")
            try:
                os.makedirs(candidate_dir, exist_ok=True)
                try:
                    os.chmod(candidate_dir, 0o700)
                except OSError:
                    pass
                log_path = os.path.join(candidate_dir, f"task_{task_id}.log")
            except Exception:
                # Fallback to private per-process dir
                root = None
                log_path = ""
        if not root or not os.path.isdir(root or ""):
            # Create private per-process dir with 0o700 — use mkdtemp to avoid
            # predictable symlink race in world-writable temp.
            try:
                private_base = os.path.join(tempfile.gettempdir(), f"mantra-{os.getpid()}")
                # Mitigate symlink attack: if path exists and is symlink, remove it
                if os.path.islink(private_base):
                    try:
                        os.unlink(private_base)
                    except OSError:
                        pass
                os.makedirs(private_base, exist_ok=True)
                # Verify it is not a symlink after creation
                try:
                    if os.path.islink(private_base):
                        # Fallback to secure mkdtemp
                        private_base = tempfile.mkdtemp(prefix=f"mantra-{os.getpid()}-")
                    else:
                        os.chmod(private_base, 0o700)
                except OSError:
                    pass
                log_path = os.path.join(private_base, f"mantra_{task_id}.log")
            except Exception:
                try:
                    private_base = tempfile.mkdtemp(prefix="mantra-")
                    log_path = os.path.join(private_base, f"mantra_{task_id}.log")
                except Exception:
                    log_path = os.path.join(tempfile.gettempdir(), f"mantra_{task_id}.log")
        # Create log file with owner-only perms; the command header goes in
        # immediately so readers always see what the task runs.
        try:
            with open(log_path, "w", encoding="utf-8") as _lf:
                _lf.write(f"$ {_redact_sensitive(command)}\n")
            try:
                os.chmod(log_path, 0o600)
            except OSError:
                pass
        except Exception:
            # Last resort: a unique file in the system temp directory. The
            # primary path writes under .mantra/logs, which list_dir
            # exposes; completed logs there are pruned at _finish_task.
            log_path = os.path.join(tempfile.gettempdir(), f"mantra_{task_id}.log")
            try:
                with open(log_path, "w", encoding="utf-8") as _lf:
                    _lf.write(f"$ {_redact_sensitive(command)}\n")
                try:
                    os.chmod(log_path, 0o600)
                except OSError:
                    pass
            except Exception:
                pass

        def _run():
            start = time.monotonic()
            # Screen before spawning: a background task must obey the same
            # command screening as the foreground path. A screening failure
            # fails closed — the task is refused, not silently spawned.
            try:
                reason = sandbox.screen_command(command)
            except Exception as exc:
                _finish_task(task_id, log_path, -1, note=f"command screening failed: {exc}", duration=0.0)
                screened.set()
                return
            if reason:
                _finish_task(task_id, log_path, -1, note=reason, duration=0.0)
                screened.set()
                return
            abort = getattr(sandbox, "abort", None)
            try:
                proc = subprocess.Popen(
                    command,
                    shell=True,
                    cwd=getattr(sandbox, "root", None) or os.getcwd(),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    # Same isolation as the foreground path: the task runs
                    # in its own process group so a timeout or abort can
                    # kill the whole tree instead of orphaning descendants.
                    # The env is filtered so background children do not
                    # inherit the harness's credential-shaped variables.
                    env=_filtered_env(os.environ),
                    **_POPEN_GROUP_KWARGS,
                )
            except Exception as exc:
                _finish_task(task_id, log_path, -1, note=str(exc), duration=0.0)
                screened.set()
                return
            # Store pid
            with _TASKS_LOCK:
                if task_id in _TASKS:
                    _TASKS[task_id]["pid"] = proc.pid
                    _TASKS[task_id]["process"] = proc
            screened.set()

            # Stream output incrementally so shell_output can follow the
            # run's progress instead of seeing nothing until exit. Writes
            # are capped at a byte ceiling so a chatty command cannot grow
            # the log without bound; past the ceiling the tail is dropped
            # and a marker + flag record the truncation.
            def _pump():
                try:
                    written = 0
                    with open(log_path, "a", encoding="utf-8", errors="replace") as lf:
                        for line in proc.stdout or []:
                            if written >= _LOG_BYTE_CEILING:
                                break
                            line_bytes = len(line.encode("utf-8", errors="replace"))
                            if written + line_bytes > _LOG_BYTE_CEILING:
                                # A single line may itself cross the ceiling;
                                # keep the head of it, then stop.
                                lf.write(_redact_sensitive(line[:_LOG_BYTE_CEILING - written]) + "\n")
                                written = _LOG_BYTE_CEILING
                            else:
                                lf.write(_redact_sensitive(line))
                                written += line_bytes
                            lf.flush()
                            if written >= _LOG_BYTE_CEILING:
                                lf.write("[log truncated — output exceeded the byte ceiling]\n")
                                lf.flush()
                                with _TASKS_LOCK:
                                    if task_id in _TASKS:
                                        _TASKS[task_id]["truncated"] = True
                                break
                except Exception as exc:
                    # Surface a failed log append so shell_output can
                    # report it.
                    with _TASKS_LOCK:
                        if task_id in _TASKS:
                            _TASKS[task_id]["log_error"] = f"output pump failed: {exc}"

            pumper = threading.Thread(target=_pump, daemon=True)
            pumper.start()
            deadline = time.monotonic() + timeout
            exit_code: int | None = None
            timed_out = False
            while True:
                if abort is not None and abort.is_set():
                    # Operator abort: kill the whole tree (the task leads
                    # its own process group), join the pumper, and finalize
                    # the task as interrupted.
                    _kill_task_tree(proc)
                    try:
                        proc.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        pass
                    pumper.join(timeout=2)
                    _finish_task(
                        task_id, log_path,
                        proc.returncode if proc.returncode is not None else -1,
                        note="interrupted by operator",
                        duration=time.monotonic() - start,
                    )
                    return
                try:
                    proc.wait(timeout=0.1)
                    exit_code = proc.returncode
                    break
                except subprocess.TimeoutExpired:
                    if time.monotonic() < deadline:
                        continue
                    timed_out = True
                    # Timeout: kill the whole tree, not just the shell, so
                    # descendants cannot keep running past the deadline.
                    _kill_task_tree(proc)
                    try:
                        proc.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        pass
                    exit_code = 143 if proc.returncode is None else proc.returncode
                    break
            pumper.join(timeout=2)
            _finish_task(
                task_id, log_path,
                exit_code if exit_code is not None else -1,
                note="(command timed out — sigterm→sigkill)" if timed_out else "",
                timed_out=timed_out,
                duration=time.monotonic() - start,
            )

        # Register task — prune expired and excess entries on every
        # registration so the one-hour TTL actually applies.
        with _TASKS_LOCK:
            _prune_tasks_locked()
            if len(_TASKS) >= _MAX_TASKS:
                _prune_tasks_locked()
            _TASKS[task_id] = {
                "task_id": task_id,
                "command": command,
                "log_path": log_path,
                "pid": None,
                "process": None,
                "start_time": time.monotonic(),
                "timeout": timeout,
                "done": False,
            }

        # The worker screens the command before spawning; the event is set
        # on every early exit (refusal, spawn failure) and right after the
        # pid is recorded. Waiting on it keeps the response honest: it
        # never claims success for a refused command and never shows pid
        # "?" for one that did start. A timeout only fires if the screener
        # itself stalls; the old "?" fallback remains for that case.
        screened = threading.Event()
        thread = threading.Thread(target=_run, daemon=True)
        thread.start()
        screened.wait(timeout=0.5)
        with _TASKS_LOCK:
            # The task may already have been pruned by a concurrent
            # registration; never assume the entry still exists.
            task = _TASKS.get(task_id) or {}
            done = task.get("done", False)
            pid = task.get("pid", "?")
        if done and pid is None:
            # The command never spawned: screening refused it (or the
            # spawn itself failed). Report the refusal instead of success.
            reason = task.get("stderr") or "unknown"
            return (
                f"background task {task_id} did not start\n"
                f"  reason: {reason}\n"
                f"  log: {log_path}\n"
                f"  use shell_output task_id={task_id} from_offset=0 to read"
            )
        if pid is None:
            pid = "?"
        return (
            f"background task {task_id} started\n"
            f"  pid: {pid}\n"
            f"  log: {log_path}\n"
            f"  use shell_output task_id={task_id} from_offset=0 to read"
        )

    def _format_result(self, result: ExecResult, command: str) -> str:
        # Honest exits: signal deaths map to 128+n; grep/Select-String
        # exiting 1 means "no matches", not an error. Timed-out runs read
        # 143; screen refusals and cap kills surface as -1, not a signal
        # death.
        result_exit, exit_note = _map_exit_status(result, command)

        parts = [f"exit_code: {result_exit}{exit_note}"]
        if result.timed_out:
            parts.append("(command timed out — sigterm→sigkill)")

        # Middle-out truncation with omitted count + full log
        stdout = result.stdout or ""
        stderr = result.stderr or ""
        combined_len = len(stdout) + len(stderr)

        # Untrusted fencing
        def _fence(text: str) -> str:
            if not text:
                return ""
            return f"<<<UNTRUSTED_TASK_OUTPUT\n{text}\n>>>"

        # If output large, middle-out keep head and tail
        MAX_HEAD = 8000
        MAX_TAIL = 8000
        MAX_TOTAL = 16000
        full_log_path = None
        if combined_len > MAX_TOTAL:
            # Write full log to temp with owner-only perms (may hold secrets)
            try:
                fd, full_log_path = tempfile.mkstemp(prefix="mantra_cmd_", suffix=".log")
                try:
                    os.chmod(full_log_path, 0o600)
                except OSError:
                    pass
                _register_full_log(full_log_path)
                with os.fdopen(fd, "w", encoding="utf-8", errors="replace") as f:
                    f.write(f"$ {_redact_sensitive(command)}\n")
                    f.write(_redact_sensitive(stdout))
                    if stderr:
                        f.write("\n[stderr]\n" + _redact_sensitive(stderr))
                # Keep head and tail; the omitted count is per stream and
                # reflects what was actually cut.
                if len(stdout) > MAX_TOTAL:
                    omitted_out = len(stdout) - (MAX_HEAD + MAX_TAIL)
                    head = stdout[:MAX_HEAD]
                    tail = stdout[-MAX_TAIL:]
                    stdout = head + f"\n... [{omitted_out} chars omitted, counted] ...\n" + tail
                    if full_log_path:
                        stdout += f"\n[full log at {full_log_path} — grep it, don't rerun]"
                if len(stderr) > MAX_TOTAL // 2:
                    # Stderr gets half the head/tail budget of stdout.
                    kept_head = MAX_HEAD // 2
                    kept_tail = MAX_TAIL // 2
                    omitted_err = len(stderr) - (kept_head + kept_tail)
                    stderr = (
                        stderr[:kept_head]
                        + f"\n... [{omitted_err} chars omitted] ...\n"
                        + stderr[-kept_tail:]
                    )
            except Exception:
                pass

        if stdout:
            # Truncate for display but note
            display_stdout = stdout[:20000]
            if len(stdout) > 20000:
                display_stdout += f"\n... [{len(stdout)-20000} chars more in log]"
            parts.append(f"stdout:\n{_fence(display_stdout)}")
        if stderr:
            display_stderr = stderr[:10000]
            if len(stderr) > 10000:
                display_stderr += f"\n... [{len(stderr)-10000} chars more]"
            parts.append(f"stderr:\n{_fence(display_stderr)}")
        if full_log_path:
            parts.append(f"log: {full_log_path}")
        return "\n".join(parts)


class ShellOutputTool(Tool):
    name = "shell_output"
    description = (
        "Read background task output from offset. "
        "from_offset must be the next_offset value returned by a previous "
        "call (a byte offset into the task log) — never re-read. "
        "Wait modes: now (instant), next_write, exit."
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "Background task id"},
            "from_offset": {"type": "number", "description": "Cursor from a previous next_offset"},
            "wait": {"type": "string", "enum": ["now", "next_write", "exit"], "description": "Wait mode"},
            "timeout": {"type": "number", "description": "Wait timeout seconds"},
        },
        "required": ["task_id"],
    }

    def execute(self, sandbox: Sandbox, task_id: str, from_offset: int = 0, wait: str = "now", timeout: float = 5.0) -> str:  # type: ignore[override]
        try:
            from_offset = int(from_offset) if from_offset else 0
        except Exception:
            from_offset = 0
        if from_offset < 0:
            from_offset = 0
        wait = (wait or "now").lower()
        if wait not in ("now", "next_write", "exit"):
            wait = "now"
        try:
            timeout_f = float(timeout) if timeout else 5.0
        except Exception:
            timeout_f = 5.0
        timeout_f = max(0, min(timeout_f, 30))

        with _TASKS_LOCK:
            task = _TASKS.get(task_id)
        if not task:
            return f"ERROR: no such task {task_id!r}"

        log_path = task.get("log_path")
        if not log_path or not os.path.exists(log_path):
            # Explain a failed log write instead of looking like the
            # log merely vanished.
            log_err = task.get("log_error")
            if log_err:
                return f"ERROR: log not found for {task_id} ({log_err})"
            return f"ERROR: log not found for {task_id}"

        # Wait handling
        if wait == "exit":
            deadline = time.monotonic() + timeout_f
            while time.monotonic() < deadline:
                with _TASKS_LOCK:
                    done = _TASKS.get(task_id, {}).get("done", False)
                if done:
                    break
                time.sleep(0.1)
        elif wait == "next_write":
            # Poll for new bytes past the cursor (both sides are byte counts).
            deadline = time.monotonic() + timeout_f
            while time.monotonic() < deadline:
                try:
                    cur = os.path.getsize(log_path)
                    if cur > from_offset:
                        break
                    with _TASKS_LOCK:
                        if _TASKS.get(task_id, {}).get("done"):
                            break
                except Exception:
                    pass
                time.sleep(0.1)

        # The cursor is a byte offset, in the same unit the next_write wait
        # compares against (os.path.getsize). Reading in binary keeps the
        # two in one unit; a text-mode stream's tell() cookie is an opaque
        # number that drifts away from the byte size as soon as multi-byte
        # characters enter the log, which used to make the wait spin past
        # its timeout while output sat unread.
        try:
            with open(log_path, "rb") as f:
                f.seek(from_offset)
                # Cap a single read; the cursor advances by what was
                # actually read.
                raw = f.read(50000)
                next_offset = from_offset + len(raw)
            data = raw.decode("utf-8", errors="replace")
        except OSError as exc:
            return f"ERROR: cannot read log: {exc}"

        if not data:
            with _TASKS_LOCK:
                done = _TASKS.get(task_id, {}).get("done", False)
                truncated = bool(_TASKS.get(task_id, {}).get("truncated"))
            trunc_note = (
                "\n[log truncated — output exceeded the byte ceiling and was dropped]"
                if truncated else ""
            )
            log_err = task.get("log_error")
            fail_note = f"\n[task log write failed: {log_err}]" if log_err else ""
            if done:
                return f"<<<UNTRUSTED_TASK_OUTPUT\n(no new output, task done)\n>>>\nnext_offset: {next_offset}{trunc_note}{fail_note}"
            return f"<<<UNTRUSTED_TASK_OUTPUT\n(no new output)\n>>>\nnext_offset: {next_offset}{trunc_note}{fail_note}"

        with _TASKS_LOCK:
            truncated = bool(_TASKS.get(task_id, {}).get("truncated"))
        trunc_note = (
            "\n[log truncated — output exceeded the byte ceiling and was dropped]"
            if truncated else ""
        )
        return f"<<<UNTRUSTED_TASK_OUTPUT\n{data}\n>>>\nnext_offset: {next_offset} (use from_offset={next_offset} next){trunc_note}"


class KillShellTool(Tool):
    name = "kill_shell"
    description = "Kill background task by task id, pid, or port."
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "task_id": {"type": "string"},
            "pid": {"type": "number"},
            "port": {"type": "number"},
        },
        "required": [],
    }

    def execute(self, sandbox: Sandbox, task_id: str = "", pid: Any = None, port: Any = None) -> str:  # type: ignore[override]
        # Direct pid/port signalling reaches the host operating system, so
        # it must never fire from inside a container sandbox — the tool
        # would be an isolation escape exactly like an unguarded background
        # exec. Only the host sandbox may use those forms; task_id killing
        # is registry-local and safe everywhere.
        host_sandbox = (
            hasattr(sandbox, "screen_command")
            and getattr(sandbox, "root", None) is not None
        )
        # Try task_id first
        if task_id:
            with _TASKS_LOCK:
                task = _TASKS.get(task_id)
            if not task:
                return f"ERROR: no such task {task_id!r}"
            proc = task.get("process")
            if proc and proc.poll() is None:
                # Tree-kill, matching the timeout/abort paths, and mark
                # the task interrupted so the worker cannot outlive it.
                _kill_task_tree(proc)
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass
                start = task.get("start_time") or time.monotonic()
                _finish_task(
                    task_id,
                    task.get("log_path") or "",
                    proc.returncode if proc.returncode is not None else -1,
                    note="interrupted by operator",
                    duration=time.monotonic() - start,
                )
                return f"OK: killed task {task_id}"
            return f"OK: task {task_id} already done (exit {task.get('exit_code')})"

        # Try pid
        if pid is not None:
            if not host_sandbox:
                return (
                    "ERROR: killing by pid is only supported by the host "
                    "sandbox; use task_id for background tasks"
                )
            try:
                pid_int = int(pid)
            except (TypeError, ValueError):
                return f"ERROR: invalid pid {pid!r}"
            if pid_int <= 1 or pid_int == os.getpid():
                return f"ERROR: refusing to kill pid {pid_int}"
            # Restrict direct pid targeting to processes this harness
            # spawned: the registry is the allowlist.
            with _TASKS_LOCK:
                known = {
                    info.get("pid") for info in _TASKS.values() if info.get("pid")
                }
            if pid_int not in known:
                return (
                    f"ERROR: pid {pid_int} was not started by a background "
                    "task of this session; kill by task_id or port instead"
                )
            try:
                import signal
                os.kill(pid_int, signal.SIGTERM)
                time.sleep(0.5)
                if os.name != "nt":
                    # POSIX: check liveness and force-kill if still alive.
                    try:
                        os.kill(pid_int, 0)
                    except OSError:
                        return f"OK: pid {pid_int} terminated"
                    forced = getattr(signal, "SIGKILL", None)
                    if forced is not None:
                        os.kill(pid_int, forced)
                    return f"OK: killed pid {pid_int} (sigterm→sigkill)"
                # win32: os.kill with SIGTERM is TerminateProcess — an
                # immediate hard kill, not a signal handshake.
                return f"OK: killed pid {pid_int} (terminated)"
            except Exception as exc:
                return f"ERROR: kill pid failed: {exc}"

        # Try port
        if port is not None:
            if not host_sandbox:
                return (
                    "ERROR: killing by port is only supported by the host "
                    "sandbox; use task_id for background tasks"
                )
            try:
                port_int = int(port)
            except (TypeError, ValueError):
                return f"ERROR: invalid port {port!r}"
            # Find the pid listening on the port, parsing per platform so
            # the first integer in the output (often part of an address)
            # is never mistaken for the PID column.
            found = None
            my_pid = os.getpid()
            try:
                if os.name == "nt":
                    res = subprocess.run(
                        "netstat -ano",
                        shell=True, capture_output=True, text=True, timeout=5,
                    )
                    for line in res.stdout.splitlines():
                        if "LISTENING" not in line.upper():
                            continue
                        # Parse the local address column exactly. A substring
                        # test would match :80 against :8080 and kill the
                        # wrong process (High-severity port-kill bug).
                        m = re.search(
                            r"(?:([0-9.]+):(\d+)|\[([0-9a-fA-F:]*)\]):(\d+)\s+",
                            line,
                        )
                        if not m:
                            continue
                        local_port = m.group(2) or m.group(4)
                        if local_port is None or int(local_port) != port_int:
                            continue
                        pidm = re.search(r"(\d+)\s*$", line.strip())
                        if pidm:
                            found = int(pidm.group(1))
                            break
                else:
                    res = subprocess.run(
                        f"lsof -ti tcp:{port_int}",
                        shell=True, capture_output=True, text=True, timeout=5,
                    )
                    for line in res.stdout.splitlines():
                        m = re.fullmatch(r"\s*(\d+)\s*", line)
                        if m:
                            found = int(m.group(1))
                            break
                    if found is None:
                        res = subprocess.run(
                            f"ss -lptn 'sport = :{port_int}'",
                            shell=True, capture_output=True, text=True, timeout=5,
                        )
                        m = re.search(r"pid=(\d+)", res.stdout)
                        if m:
                            found = int(m.group(1))
            except Exception:
                found = None
            # Never signal pid 0/1 (process groups, init) or ourselves.
            if found is None or found <= 1 or found == my_pid:
                return f"ERROR: no killable process found on port {port_int}"
            # The harness-spawned background-task registry is the allowlist
            # for kill-by-port, matching the pid branch: never signal a host
            # process this session did not start.
            with _TASKS_LOCK:
                known_pids = {t.get("pid") for t in _TASKS.values() if t.get("pid")}
            if found not in known_pids:
                return (
                    f"ERROR: process {found} on port {port_int} was not started "
                    "by a background task; refusing to kill an unknown process"
                )
            try:
                import signal
                os.kill(found, signal.SIGTERM)
                time.sleep(0.5)
                if os.name != "nt":
                    try:
                        os.kill(found, 0)
                    except OSError:
                        return f"OK: killed port {port_int} (pid {found})"
                    forced = getattr(signal, "SIGKILL", None)
                    if forced is not None:
                        os.kill(found, forced)
                    return f"OK: killed port {port_int} (pid {found})"
                # win32: os.kill is TerminateProcess — immediate hard kill.
                return f"OK: killed port {port_int} (pid {found}, terminated)"
            except Exception as exc:
                return f"ERROR: kill port failed: {exc}"

        return "ERROR: provide task_id or pid or port"


class GitDiffTool(Tool):
    """Show the uncommitted diff of the repository."""

    name = "git_diff"
    description = "Show the current uncommitted diff of the repository."
    parameters: dict[str, Any] = {"type": "object", "properties": {}}

    def execute(self, sandbox: Sandbox) -> str:
        result = sandbox.exec("git diff", timeout=30)
        if result.exit_code != 0:
            return f"ERROR: git diff failed: {result.stderr[:500] or 'unknown'}"
        return result.stdout or "(no changes)"


class GitResetTool(Tool):
    name = "git_reset"
    description = (
        "Discard all uncommitted changes (git checkout -- .). "
        "Use to start over after bad edits."
    )
    parameters: dict[str, Any] = {"type": "object", "properties": {}}

    def execute(self, sandbox: Sandbox) -> str:
        result = sandbox.exec("git checkout -- . && git reset", timeout=30)
        if result.exit_code != 0:
            return f"ERROR: {result.stderr}"
        return "OK: working tree reset"

