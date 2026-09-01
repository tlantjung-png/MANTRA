"""Command and git tools — TEF-optimized shell.

Implements Command Code shell TEF capabilities:
- background by default for long work (task id + log path instant)
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
import shlex
import subprocess
import tempfile
import threading
import time
import uuid
from typing import Any

from mantra.interfaces.sandbox import ExecResult, Sandbox
from mantra.interfaces.tool import Tool

# Background task registry — bounded to prevent unbounded growth.
# Entries are pruned after completion and max size is enforced.
_TASKS: dict[str, dict[str, Any]] = {}
_TASKS_LOCK = threading.Lock()
_TASK_COUNTER = 0
_MAX_TASKS = 100
_TASK_TTL_SECONDS = 3600  # 1 hour; completed tasks older than this are pruned


def _prune_tasks_locked() -> None:
    """Prune old completed tasks when registry grows too large."""
    import time as _t
    now = _t.monotonic()
    # First, remove expired completed tasks
    expired = [
        tid for tid, info in _TASKS.items()
        if info.get("done") and (now - info.get("end_time", info.get("start_time", now))) > _TASK_TTL_SECONDS
    ]
    for tid in expired:
        info = _TASKS.pop(tid, None)
        # Clean up log file for pruned task
        try:
            lp = info.get("log_path") if info else None
            if lp and os.path.exists(lp):
                os.remove(lp)
        except Exception:
            pass
    # If still over capacity, remove oldest completed first, then oldest overall
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
            # No completed tasks — evict oldest overall
            victim = min(_TASKS.items(), key=lambda kv: kv[1].get("start_time", float("inf")))[0]
        info = _TASKS.pop(victim, None)
        try:
            lp = info.get("log_path") if info else None
            if lp and os.path.exists(lp):
                os.remove(lp)
        except Exception:
            pass

def _next_task_id() -> str:
    global _TASK_COUNTER
    with _TASKS_LOCK:
        _TASK_COUNTER += 1
        return f"tsk_{_TASK_COUNTER:04d}_{uuid.uuid4().hex[:6]}"

def _is_long_command(cmd: str) -> bool:
    # Heuristic: long builds, dev servers, watch modes
    long_markers = ["npm run", "pnpm ", "yarn ", "pytest", "cargo test", "go test", "sleep ", "watch", "dev", "serve", "build"]
    return any(m in cmd for m in long_markers) or len(cmd) > 80

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
        if len(command) > 10000:
            return "ERROR: command too long"

        # Background now requires explicit opt-in (background=true).
        # Previously a heuristic auto-backgrounded based on substrings, which
        # surprised operators who expected immediate output. Explicit is
        # predictable and production-safe.
        use_bg = False
        if background is True:
            use_bg = True
        elif background is not None and background not in (False, None):
            # Truthy non-bool (e.g. 1, "true") also opts in; explicit only.
            use_bg = bool(background)

        if use_bg:
            return self._execute_background(sandbox, command, timeout_f)

        # Foreground: use sandbox exec with caps
        result = sandbox.exec(command, timeout=timeout_f)
        return self._format_result(result, command)

    def _execute_background(self, sandbox: Sandbox, command: str, timeout: float) -> str:
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
        # Create log file with owner-only perms
        try:
            with open(log_path, "w", encoding="utf-8") as _lf:
                pass
            try:
                os.chmod(log_path, 0o600)
            except OSError:
                pass
        except Exception:
            # Last resort: workspace fallback
            try:
                ws_root = getattr(sandbox, "root", None) or os.getcwd()
                log_path = os.path.join(ws_root, f".mantra_{task_id}.log")
                with open(log_path, "w", encoding="utf-8") as _lf:
                    pass
                try:
                    os.chmod(log_path, 0o600)
                except OSError:
                    pass
            except Exception:
                log_path = os.path.join(tempfile.gettempdir(), f"mantra_{task_id}.log")

        def _run():
            start = time.monotonic()
            # Use shell for background too
            try:
                proc = subprocess.Popen(
                    command,
                    shell=True,
                    cwd=getattr(sandbox, "root", None) or os.getcwd(),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    errors="replace",
                )
                # Store pid
                with _TASKS_LOCK:
                    if task_id in _TASKS:
                        _TASKS[task_id]["pid"] = proc.pid
                        _TASKS[task_id]["process"] = proc
                # Wait with timeout
                try:
                    stdout, stderr = proc.communicate(timeout=timeout)
                    exit_code = proc.returncode
                    timed_out = False
                except subprocess.TimeoutExpired:
                    # sigterm -> poll -> sigkill
                    try:
                        proc.terminate()
                        time.sleep(0.5)
                        if proc.poll() is None:
                            proc.kill()
                        stdout, stderr = proc.communicate(timeout=2)
                    except Exception:
                        stdout, stderr = "", "killed after timeout"
                    exit_code = 143 if proc.returncode is None else proc.returncode
                    timed_out = True

                # Write full log
                try:
                    with open(log_path, "w", encoding="utf-8", errors="replace") as f:
                        f.write(f"$ {command}\n")
                        if stdout:
                            f.write(stdout)
                        if stderr:
                            f.write("\n[stderr]\n" + stderr)
                        f.write(f"\nexit_code: {exit_code}\n")
                except Exception:
                    pass

                # Update task
                with _TASKS_LOCK:
                    if task_id in _TASKS:
                        _TASKS[task_id].update({
                            "exit_code": exit_code,
                            "stdout": stdout or "",
                            "stderr": stderr or "",
                            "timed_out": timed_out,
                            "done": True,
                            "end_time": time.monotonic(),
                            "duration": time.monotonic() - start,
                        })
            except Exception as exc:
                with _TASKS_LOCK:
                    if task_id in _TASKS:
                        _TASKS[task_id].update({
                            "exit_code": -1,
                            "stderr": str(exc),
                            "done": True,
                        })

        # Register task — prune first if at capacity
        with _TASKS_LOCK:
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

        thread = threading.Thread(target=_run, daemon=True)
        thread.start()
        # Return instantly
        time.sleep(0.05)  # brief to get pid
        with _TASKS_LOCK:
            pid = _TASKS[task_id].get("pid", "?")
        return (
            f"background task {task_id} started\n"
            f"  pid: {pid}\n"
            f"  log: {log_path}\n"
            f"  use shell_output task_id={task_id} from_offset=0 to read"
        )

    def _format_result(self, result: ExecResult, command: str) -> str:
        # Honest exits — harness lib-shell.ps1:5 Get-HonestExitCode + Get-BenignExitNote:20
        exit_code = result.exit_code
        # Handle Python negative signal codes (e.g., -9 -> 137)
        if exit_code is not None and exit_code < 0:
            exit_code = 128 - exit_code  # -9 -> 137
        elif exit_code is None:
            exit_code = 128
        # Update result for display
        display_code = exit_code
        exit_note = ""
        if display_code == 137:
            exit_note = " (SIGKILL, 128+9)"
        elif display_code == 143:
            exit_note = " (SIGTERM, 128+15)"
        elif display_code == 128:
            exit_note = " (signal death, 128)"
        elif display_code == 1 and re.search(r"(^|\s|;)grep(\.exe)?\b", command, re.IGNORECASE):
            exit_note = " (grep: no matches — not an error)"
        elif display_code == 1 and re.search(r"Select-String", command, re.IGNORECASE):
            exit_note = " (Select-String: no matches — not an error)"
        # Use honest code for parts
        result_exit = display_code

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
            # Write full log to temp
            try:
                fd, full_log_path = tempfile.mkstemp(prefix="mantra_cmd_", suffix=".log")
                with os.fdopen(fd, "w", encoding="utf-8", errors="replace") as f:
                    f.write(f"$ {command}\n")
                    f.write(stdout)
                    if stderr:
                        f.write("\n[stderr]\n" + stderr)
                omitted = combined_len - MAX_TOTAL
                # Keep head and tail
                if len(stdout) > MAX_TOTAL:
                    head = stdout[:MAX_HEAD]
                    tail = stdout[-MAX_TAIL:]
                    stdout = head + f"\n... [{omitted} chars omitted, counted] ...\n" + tail
                    if full_log_path:
                        stdout += f"\n[full log at {full_log_path} — grep it, don't rerun]"
                # Similar for stderr if needed
                if len(stderr) > 8000:
                    stderr = stderr[:4000] + f"\n... [{len(stderr)-8000} chars omitted] ..."
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
        "Use from_offset to get only new bytes, never re-read. "
        "Wait modes: now (instant), next_write, exit."
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "Background task id"},
            "from_offset": {"type": "number", "description": "Byte offset to read from"},
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
            # Poll for new bytes
            initial_size = 0
            try:
                initial_size = os.path.getsize(log_path)
            except Exception:
                initial_size = from_offset
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

        try:
            with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                f.seek(from_offset)
                data = f.read(50000)  # cap per read
                next_offset = f.tell()
        except OSError as exc:
            return f"ERROR: cannot read log: {exc}"

        if not data:
            with _TASKS_LOCK:
                done = _TASKS.get(task_id, {}).get("done", False)
            if done:
                return f"<<<UNTRUSTED_TASK_OUTPUT\n(no new output, task done)\n>>>\nnext_offset: {next_offset}"
            return f"<<<UNTRUSTED_TASK_OUTPUT\n(no new output)\n>>>\nnext_offset: {next_offset}"

        return f"<<<UNTRUSTED_TASK_OUTPUT\n{data}\n>>>\nnext_offset: {next_offset} (use from_offset={next_offset} next)"


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
        # Try task_id first
        if task_id:
            with _TASKS_LOCK:
                task = _TASKS.get(task_id)
            if not task:
                return f"ERROR: no such task {task_id!r}"
            proc = task.get("process")
            if proc and proc.poll() is None:
                try:
                    proc.terminate()
                    time.sleep(0.5)
                    if proc.poll() is None:
                        proc.kill()
                    return f"OK: killed task {task_id} (sigterm→sigkill)"
                except Exception as exc:
                    return f"ERROR: kill failed: {exc}"
            return f"OK: task {task_id} already done (exit {task.get('exit_code')})"

        # Try pid
        if pid is not None:
            try:
                pid_int = int(pid)
                import signal
                os.kill(pid_int, signal.SIGTERM)
                time.sleep(0.5)
                try:
                    os.kill(pid_int, 0)
                    os.kill(pid_int, signal.SIGKILL)
                    return f"OK: killed pid {pid_int} (sigterm→sigkill)"
                except OSError:
                    return f"OK: pid {pid_int} terminated"
            except Exception as exc:
                return f"ERROR: kill pid failed: {exc}"

        # Try port
        if port is not None:
            try:
                port_int = int(port)
                # Find pid by port (best effort via netstat/lsof)
                found = None
                for cmd in [
                    f"lsof -ti tcp:{port_int}",
                    f"netstat -ano | findstr :{port_int}",
                    f"ss -lptn 'sport = :{port_int}'",
                ]:
                    try:
                        # Use sandbox exec to run host command? For local, use subprocess
                        import subprocess
                        res = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=5)
                        if res.stdout.strip():
                            # Parse first pid
                            import re
                            m = re.search(r"\b(\d+)\b", res.stdout)
                            if m:
                                found = int(m.group(1))
                                break
                    except Exception:
                        continue
                if found:
                    import signal
                    os.kill(found, signal.SIGTERM)
                    time.sleep(0.5)
                    try:
                        os.kill(found, 0)
                        os.kill(found, signal.SIGKILL)
                    except OSError:
                        pass
                    return f"OK: killed port {port_int} (pid {found})"
                return f"ERROR: no process found on port {port}"
            except Exception as exc:
                return f"ERROR: kill port failed: {exc}"

        return "ERROR: provide task_id or pid or port"


class GitDiffTool(Tool):
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

