"""Host sandbox: executes in workspace dir, no isolation."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import threading
import time

from core.agent.exceptions import AbortError, SandboxError
from core.procutil import POPEN_GROUP_KWARGS as _POPEN_GROUP_KWARGS
from core.procutil import kill_process_tree as _kill_process_tree
from core.procutil import read_pipe_into as _read_pipe_into
from core.urlcheck import is_safe_commit as _is_safe_commit_impl
from core.urlcheck import is_safe_repo_url as _is_safe_repo_url_impl
from core.types import ExecResult, Sandbox

# Heuristic patterns for shell traversal; best effort, not a boundary.
# The host sandbox executes with shell=True so containment cannot be
# guaranteed; this check is defence-in-depth only. Use the container
# sandbox when strong isolation is required.
_ABSOLUTE_WIN_RE = re.compile(r"[a-zA-Z]:[\\/]")
_ENCODED_TRAVERSAL_RE = re.compile(r"(%2e%2e|%252e|\\u002e|\\x2e)", re.IGNORECASE)

_MAX_READ_BYTES = 500_000
_MAX_EXEC_BYTES = 1_000_000

# Defense-in-depth: children of the host sandbox must not inherit the
# harness's credential-shaped environment variables. Approvals remain the
# real gate — this only stops a model-issued command from trivially
# printing secrets. PATH/SystemRoot/HOME etc. are kept as-is.
_SECRET_ENV_MARKERS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")
# Known MANTRA_* variables that are secret stores (the marker filter
# already catches most of these by name).
_MANTRA_SECRET_ENV = frozenset({"MANTRA_CREDENTIALS"})


def _filtered_env(env: dict) -> dict:
    """Return ``env`` minus credential-shaped variables."""
    return {
        name: value
        for name, value in env.items()
        if not any(marker in name.upper() for marker in _SECRET_ENV_MARKERS)
        and name.upper() not in _MANTRA_SECRET_ENV
    }


def _strip_quoted(s: str) -> str:
    """Remove content inside single/double quotes to avoid false positives."""
    # Blank out quoted spans, preserving length, so match positions hold.
    def _repl(m: re.Match[str]) -> str:
        return " " * len(m.group(0))
    # Escaped-quote handling is imperfect but sufficient for a heuristic screen.
    s = re.sub(r'"[^"]*"', _repl, s)
    s = re.sub(r"'[^']*'", _repl, s)
    return s

_WRAPPER_WORDS = ("cmd", "cmd.exe", "powershell", "powershell.exe", "pwsh", "pwsh.exe")
_WRAPPER_FLAGS = ("/c", "/k", "-c", "-command", "-encodedcommand")


def _wrapper_payload(command: str) -> str | None:
    """Nested command inside a wrapper invocation, if any.

    Only the known shell wrappers (cmd/powershell/pwsh) followed by a
    flag (/c, -c, -Command) are unwrapped; every other command keeps its
    quoted spans as plain data.
    """
    tokens = re.findall(r'"[^"]*"|\'[^\']*\'|\S+', command)
    if not tokens:
        return None
    first = re.split(r"[\\/]", tokens[0].strip('"\'').lower())[-1]
    if first not in _WRAPPER_WORDS:
        return None
    seen_flag = False
    for tok in tokens[1:]:
        low = tok.strip('"\'').lower()
        if not seen_flag:
            if low in _WRAPPER_FLAGS:
                seen_flag = True
            continue
        if len(tok) >= 2 and tok[0] == tok[-1] and tok[0] in "\"'":
            return tok[1:-1]
    return None


def _scan_traversal_core(text: str) -> bool:
    """Scan a single command string for workspace-escape patterns."""
    if not text:
        return False
    # Skip checks for URL like strings inside the command to avoid
    # flagging https:// as an absolute path. Strip URL schemes before test.
    stripped = re.sub(r"https?://[^\s]+", "", text, flags=re.IGNORECASE)
    stripped = re.sub(r"file://[^\s]+", "", stripped, flags=re.IGNORECASE)
    # Decode common URL-encoding that can hide ".." (e.g. %2e%2e, %252e).
    try:
        import urllib.parse as _up
        # Two rounds of unquote to catch double-encoding.
        decoded = _up.unquote(_up.unquote(stripped))
    except (UnicodeError, ValueError):
        decoded = stripped  # undecodable: screening continues on the raw form
    if _ENCODED_TRAVERSAL_RE.search(stripped) or _ENCODED_TRAVERSAL_RE.search(decoded):
        return True
    # Block shell expansions that can hide paths: $(...), `...`, ${...}, %VAR%
    # Check both the raw and the decoded forms so an expansion hidden by
    # encoding cannot slip past the gate.
    if re.search(r"(\$\(|\$\{|`)", stripped) or re.search(r"(\$\(|\$\{|`)", decoded):
        return True
    # Block home-directory expansion which escapes the workspace via shell
    # Avoid flagging quoted strings like echo "~" or echo "$HOME"
    stripped_unquoted = _strip_quoted(stripped)
    decoded_unquoted = _strip_quoted(decoded)
    if re.search(r"(?:^|[\s;|&])~", stripped_unquoted):
        return True
    if re.search(r"\$(?:HOME|USERPROFILE|HOMEPATH|\{HOME|\{USERPROFILE)", stripped_unquoted, flags=re.IGNORECASE):
        return True
    if re.search(r"%\s*USERPROFILE\s*%", stripped_unquoted, flags=re.IGNORECASE):
        return True
    if re.search(r"\$(?:HOME|USERPROFILE|HOMEPATH|\{HOME|\{USERPROFILE)", decoded_unquoted, flags=re.IGNORECASE):
        return True
    if re.search(r"%\s*USERPROFILE\s*%", decoded_unquoted, flags=re.IGNORECASE):
        return True
    # Block any parent directory reference, even without slash like `cd ..`
    # or `dir ..` which still escapes the workspace. Quoted spans are data
    # (echo "a .. b" is harmless), so only unquoted text is inspected.
    for target in (stripped_unquoted, decoded_unquoted):
        if re.search(r"(?:^|[\s\"'/\\:])\.\.(?:$|[\s\"'/\\])", target):
            return True
        # cmd.exe also accepts "cd.." with no separator before the dots.
        if re.search(r"(?i)(?:^|[\s;&|()])cd\.\.(?:$|[\s\\/])", target):
            return True
    # Check absolute paths in arguments only, not the executable name.
    # Split into tokens and check from the second token onwards; quoted
    # spans are excluded for the same reason as above.
    for target in (stripped_unquoted, decoded_unquoted):
        tokens = target.split()
        if not tokens:
            continue
        # The executable itself can also be a drive-letter absolute path
        # (C:\tools\script.cmd ...) and must not bypass the screen. A bare
        # leading slash as the first token stays exempt: it is cmd.exe
        # switch syntax (findstr /C:"...", dir /s /b), not a path.
        if _ABSOLUTE_WIN_RE.search(tokens[0]):
            return True
        if len(tokens) > 1:
            args_stripped = " ".join(tokens[1:])
            # Windows absolute paths are drive-letter rooted (C:\...), so
            # they are caught by _ABSOLUTE_WIN_RE above. A bare leading
            # slash on Windows is cmd.exe switch syntax (findstr /C:"...",
            # dir /s /b) and must not be mistaken for a POSIX absolute
            # path; only POSIX shells resolve /etc/passwd style paths.
            if _ABSOLUTE_WIN_RE.search(args_stripped):
                return True
            if os.name != "nt":
                for token in re.findall(r"(?:^|\s)(/[^\s]+)", args_stripped):
                    token = token.strip()
                    # //-prefixed tokens are UNC-style network paths.
                    if len(token) > 1 and not token.startswith("//"):
                        return True
    return False


def _contains_traversal(command: str) -> bool:
    """Heuristic: is the command likely to escape the workspace?

    Defence in depth only: the host sandbox uses shell=True and cannot
    guarantee containment. Quoted spans are data, not shell-resolved paths,
    except inside wrapper invocations (cmd /c "...") where the quotes hold
    a nested command that is scanned separately.
    """
    if not command:
        return False
    wrapper = _wrapper_payload(command)
    if wrapper is not None and _scan_traversal_core(wrapper):
        return True
    return _scan_traversal_core(command)


class LocalSandbox(Sandbox):
    """Runs commands in a scratch directory on the host."""

    def __init__(self, workspace_root: str | None = None) -> None:
        self._root = workspace_root
        self._owns_root = workspace_root is None
        self.changed: set[str] = set()  # paths written this session

    def setup(self, task: dict) -> None:
        if self._root is None:
            self._root = tempfile.mkdtemp(prefix="mantra-task-")
        os.makedirs(self._root, exist_ok=True)

        repo_url = task.get("repo_url")
        if repo_url:
            if not self._is_safe_repo_url(str(repo_url)):
                raise SandboxError(f"repo_url rejected: {repo_url!r}")
            result = self._exec_no_shell(
                ["git", "clone", str(repo_url), "."],
                timeout=task.get("clone_timeout", 300),
            )
            if result.exit_code != 0:
                raise SandboxError(
                    f"git clone failed ({result.exit_code}): {result.stderr[:2000]}"
                )
            commit = task.get("base_commit")
            if commit:
                if not self._is_safe_commit(str(commit)):
                    raise SandboxError(f"base_commit rejected: {commit!r}")
                result = self._exec_no_shell(
                    ["git", "checkout", str(commit)], timeout=60
                )
                if result.exit_code != 0:
                    raise SandboxError(
                        f"git checkout failed ({result.exit_code}): {result.stderr[:2000]}"
                    )

        setup_cmd = task.get("setup_cmd")
        if setup_cmd:
            result = self.exec(setup_cmd, timeout=task.get("setup_timeout", 600))
            if result.exit_code != 0:
                raise SandboxError(
                    f"setup_cmd failed ({result.exit_code}): {result.stderr[:2000]}"
                )

    @property
    def root(self) -> str:
        if self._root is None:
            raise SandboxError("sandbox not set up")
        return self._root

    def screen_command(self, command: str) -> str | None:
        """Reject a command before execution; the reason or None.

        Shared by the foreground ``exec`` path and the command tool's
        background path so background tasks cannot bypass screening.
        """
        if _contains_traversal(command):
            return (
                "blocked: command appears to access paths outside the workspace; "
                "use relative paths inside the workspace or use the container "
                "sandbox for stronger isolation"
            )
        return None

    def exec(self, command: str, timeout: float = 120.0) -> ExecResult:
        abort = getattr(self, "abort", None)
        if abort is not None and abort.is_set():
            raise AbortError("interrupted by operator")
        # Validate timeout is sane
        try:
            timeout_f = float(timeout)
        except (TypeError, ValueError):
            return ExecResult(exit_code=-1, stdout="", stderr=f"invalid timeout {timeout!r}", timed_out=False)
        if timeout_f <= 0 or timeout_f > 600:
            return ExecResult(exit_code=-1, stdout="", stderr="timeout out of range (0,600]", timed_out=False)
        reason = self.screen_command(command)
        if reason:
            return ExecResult(exit_code=-1, stdout="", stderr=reason, timed_out=False)
        # Use Popen so abort can interrupt a long-running command.
        try:
            proc = subprocess.Popen(
                command,
                shell=True,
                cwd=self.root,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=_filtered_env(os.environ),
                # Binary pipes: the reader threads decode incrementally,
                # so the output cap bounds memory for chatty commands.
                **_POPEN_GROUP_KWARGS,
            )
        except OSError as exc:
            return ExecResult(exit_code=-1, stdout="", stderr=str(exc), timed_out=False)
        return self._pump(proc, timeout_f)

    def _pump(self, proc: subprocess.Popen, timeout_f: float) -> ExecResult:
        """Collect output incrementally until exit, cap, deadline, or abort.

        Reader threads fill capped buffers as bytes arrive, so a chatty
        command never buffers its full output in RAM. A timeout, abort, or
        cap overflow kills the whole process tree, not just the shell, so
        descendants cannot keep the pipes open past the deadline.
        """
        abort = getattr(self, "abort", None)
        out_buf = bytearray()
        err_buf = bytearray()
        out_done = threading.Event()
        err_done = threading.Event()
        t_out = threading.Thread(
            target=_read_pipe_into,
            args=(proc.stdout, out_buf, _MAX_EXEC_BYTES, out_done),
            daemon=True,
        )
        t_err = threading.Thread(
            target=_read_pipe_into,
            args=(proc.stderr, err_buf, _MAX_EXEC_BYTES, err_done),
            daemon=True,
        )
        t_out.start()
        t_err.start()
        timed_out = False
        interval = 0.1
        deadline = time.monotonic() + timeout_f
        try:
            while True:
                if abort is not None and abort.is_set():
                    _kill_process_tree(proc)
                    raise AbortError("interrupted by operator")
                if out_done.is_set() and err_done.is_set():
                    break
                if len(out_buf) >= _MAX_EXEC_BYTES or len(err_buf) >= _MAX_EXEC_BYTES:
                    _kill_process_tree(proc)
                    break
                if time.monotonic() >= deadline:
                    timed_out = True
                    _kill_process_tree(proc)
                    break
                time.sleep(interval)
        finally:
            # After a kill the pipes close once the tree is gone; the
            # reader threads then hit EOF and release the handles.
            t_out.join(timeout=2)
            t_err.join(timeout=2)
        # Both pipes EOF does not prove the child exited (a descendant can
        # close its inherited handles and keep running); verify liveness
        # and tree-kill a survivor so no process is orphaned.
        if not timed_out and proc.poll() is None:
            try:
                proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                _kill_process_tree(proc)
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass
        stdout = out_buf.decode("utf-8", errors="replace")
        stderr = err_buf.decode("utf-8", errors="replace")
        if len(out_buf) >= _MAX_EXEC_BYTES:
            cut = out_buf[:_MAX_EXEC_BYTES]
            while cut and (cut[-1] & 0xC0) == 0x80:
                cut = cut[:-1]
            stdout = cut.decode("utf-8", errors="replace") + "\n... [truncated]"
        if len(err_buf) >= _MAX_EXEC_BYTES:
            cut = err_buf[:_MAX_EXEC_BYTES]
            while cut and (cut[-1] & 0xC0) == 0x80:
                cut = cut[:-1]
            stderr = cut.decode("utf-8", errors="replace") + "\n... [truncated]"
        if timed_out:
            return ExecResult(exit_code=-1, stdout=stdout, stderr=stderr, timed_out=True)
        exit_code = proc.poll()
        if exit_code is None:
            exit_code = -1
        return ExecResult(exit_code=exit_code, stdout=stdout, stderr=stderr)

    def read_file(self, path: str) -> str:
        full = self._resolve(path)
        # Cap the read in bytes (not characters) so the cap holds for
        # multi-byte content and the truncation marker is only appended
        # when content was actually cut.
        try:
            with open(full, "rb") as handle:
                data = handle.read(_MAX_READ_BYTES + 1)
        except OSError as exc:
            raise SandboxError(str(exc)) from exc
        if len(data) > _MAX_READ_BYTES:
            return data[:_MAX_READ_BYTES].decode("utf-8", errors="replace") + "\n... [truncated]"
        return data.decode("utf-8", errors="replace")

    def write_file(self, path: str, content: str) -> None:
        # Cap the encoded byte size: the limit is about the file that
        # lands on disk, and character counts understate multi-byte
        # content.
        if len(content.encode("utf-8")) > _MAX_READ_BYTES * 2:
            raise SandboxError(f"content too large ({len(content.encode('utf-8'))} bytes)")
        full = self._resolve(path)
        parent = os.path.dirname(full)
        if parent:
            os.makedirs(parent, exist_ok=True)
            # Re-validate after makedirs; narrow TOCTOU window.
            real_parent = os.path.realpath(parent)
            real_base = os.path.realpath(self.root)
            if not (real_parent == real_base or real_parent.startswith(real_base + os.sep)):
                raise SandboxError(f"path escapes sandbox workspace: {path}")
            cur = parent
            # Walk each parent component: a symlink anywhere in the chain
            # can point the write outside the workspace.
            while cur and cur != real_base and cur.startswith(real_base):
                if os.path.islink(cur):
                    raise SandboxError(f"path escapes sandbox workspace: {path}")
                nxt = os.path.dirname(cur)
                if nxt == cur:
                    break
                cur = nxt
            full = self._resolve(path)
            # Reject symlink target.
            if os.path.islink(full) or os.path.islink(parent):
                raise SandboxError(f"path escapes sandbox workspace: {path}")
        # Atomic tmp+replace with a unique temp name: a predictable
        # "<file>.tmp" path is a check-then-open race an attacker can win
        # by planting a file (or winning the replace) between the symlink
        # check and the open.
        import tempfile as _tempfile

        def _atomic_write(tmp_path: str) -> None:
            if os.path.islink(tmp_path):
                raise SandboxError(f"path escapes sandbox workspace: {path}")
            with open(tmp_path, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(content)
            os.replace(tmp_path, full)

        tmp = ""
        try:
            fd, tmp = _tempfile.mkstemp(
                dir=parent or ".", prefix=os.path.basename(full) + ".", suffix=".tmp"
            )
            os.close(fd)
        except OSError:
            tmp = ""
        if not tmp:
            tmp = os.path.join(parent or ".", f".{os.path.basename(full)}.{os.getpid()}.{time.time_ns()}.tmp")
        written = False
        try:
            _atomic_write(tmp)
            written = True
        except OSError:
            pass  # retried once below with a fresh temp name
        if not written:
            # Retry the atomic tmp+replace once with a second mkstemp
            # before the direct-write fallback: a transient os.replace
            # failure must not truncate the file in place.
            tmp2 = ""
            try:
                fd2, tmp2 = _tempfile.mkstemp(
                    dir=parent or ".", prefix=os.path.basename(full) + ".", suffix=".tmp"
                )
                os.close(fd2)
            except OSError:
                tmp2 = ""
            if tmp2:
                try:
                    _atomic_write(tmp2)
                    written = True
                except OSError:
                    pass
                try:
                    if os.path.exists(tmp2):
                        os.remove(tmp2)
                except OSError:
                    pass
                if written:
                    try:
                        if tmp and os.path.exists(tmp):
                            os.remove(tmp)
                    except OSError:
                        pass
        if not written:
            # Fallback direct write. Re-run the full confinement check so
            # a parent swapped to a symlink during the tmp attempt cannot
            # redirect the write outside the workspace.
            try:
                full = self._resolve(path)
                if parent:
                    real_parent = os.path.realpath(parent)
                    real_base = os.path.realpath(self.root)
                    if not (real_parent == real_base or real_parent.startswith(real_base + os.sep)):
                        raise SandboxError(f"path escapes sandbox workspace: {path}")
                    cur = parent
                    while cur and cur != real_base and cur.startswith(real_base):
                        if os.path.islink(cur):
                            raise SandboxError(f"path escapes sandbox workspace: {path}")
                        nxt = os.path.dirname(cur)
                        if nxt == cur:
                            break
                        cur = nxt
                if os.path.islink(full) or (tmp and os.path.islink(tmp)):
                    raise SandboxError(f"path escapes sandbox workspace: {path}")
            except SandboxError:
                try:
                    if tmp and os.path.exists(tmp):
                        os.remove(tmp)
                except OSError:
                    pass
                raise
            try:
                with open(full, "w", encoding="utf-8", newline="\n") as handle:
                    handle.write(content)
            except OSError as exc:
                # Remove the staged temp before surfacing: a failed direct
                # write must not leave litter in the workspace.
                try:
                    if tmp and os.path.exists(tmp):
                        os.remove(tmp)
                except OSError:
                    pass
                raise SandboxError(str(exc)) from exc
            try:
                if tmp and os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass
        # Normalized separators keep the changed set OS-independent.
        self.changed.add(path.replace("\\", "/"))

    def cleanup(self) -> None:
        """Release resources. Idempotent; safe for long-lived sandboxes.

        Only a sandbox that created its own scratch directory forgets its
        root. A sandbox handed a workspace (the console case) keeps pointing
        at it, so a second run cannot silently drift into a temp directory.
        """
        if self._owns_root and self._root and os.path.isdir(self._root):
            shutil.rmtree(self._root, ignore_errors=True)
            self._root = None

    def _resolve(self, path: str) -> str:
        """Join path onto the workspace and refuse escapes, resolving symlinks."""
        full = os.path.realpath(os.path.join(self.root, path))
        base = os.path.realpath(self.root)
        if not (full == base or full.startswith(base + os.sep)):
            raise SandboxError(f"path escapes sandbox workspace: {path}")
        return full

    def _exec_no_shell(self, args: list[str], timeout: float = 120.0) -> ExecResult:
        """Run a command without shell interpretation, abort-aware."""
        abort = getattr(self, "abort", None)
        if abort is not None and abort.is_set():
            raise AbortError("interrupted by operator")
        try:
            timeout_f = float(timeout)
        except (TypeError, ValueError):
            return ExecResult(exit_code=-1, stdout="", stderr=f"invalid timeout {timeout!r}", timed_out=False)
        if timeout_f <= 0 or timeout_f > 600:
            return ExecResult(exit_code=-1, stdout="", stderr="timeout out of range", timed_out=False)
        try:
            proc = subprocess.Popen(
                args,
                cwd=self.root,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=_filtered_env(os.environ),
                **_POPEN_GROUP_KWARGS,
            )
        except OSError as exc:
            return ExecResult(exit_code=-1, stdout="", stderr=str(exc), timed_out=False)
        return self._pump(proc, timeout_f)

    # Shared refusal rules live in core.urlcheck; re-exported as
    # staticmethods so callers and tests keep the LocalSandbox seams.
    _is_safe_repo_url = staticmethod(_is_safe_repo_url_impl)
    _is_safe_commit = staticmethod(_is_safe_commit_impl)
