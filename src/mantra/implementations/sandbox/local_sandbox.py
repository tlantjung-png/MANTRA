"""Host sandbox: executes in workspace dir, no isolation."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import time

from mantra.core.exceptions import AbortError, SandboxError
from mantra.interfaces.sandbox import ExecResult, Sandbox

# Heuristic patterns for shell traversal; best effort, not a boundary.
# The host sandbox executes with shell=True so containment cannot be
# guaranteed; this check is defence-in-depth only. Use the container
# sandbox when strong isolation is required.
_ABSOLUTE_WIN_RE = re.compile(r"[a-zA-Z]:[\\/]")
_ENCODED_TRAVERSAL_RE = re.compile(r"(%2e%2e|%252e|\\u002e|\\x2e)", re.IGNORECASE)

_MAX_READ_BYTES = 500_000
_MAX_EXEC_BYTES = 1_000_000


def _strip_quoted(s: str) -> str:
    """Remove content inside single/double quotes to avoid false positives."""
    # Replace quoted segments with spaces so positions preserved
    def _repl(m: re.Match[str]) -> str:
        return " " * len(m.group(0))
    # \" or \' handling is imperfect but sufficient for heuristic
    s = re.sub(r'"[^"]*"', _repl, s)
    s = re.sub(r"'[^']*'", _repl, s)
    return s

def _contains_traversal(command: str) -> bool:
    """Heuristic: is the command likely to escape the workspace?

    Defence in depth only: the host sandbox uses shell=True and cannot
    guarantee containment. Quoted spans are data, not shell-resolved paths.
    """
    if not command:
        return False
    # Skip checks for URL like strings inside the command to avoid
    # flagging https:// as an absolute path. Strip URL schemes before test.
    stripped = re.sub(r"https?://[^\s]+", "", command, flags=re.IGNORECASE)
    stripped = re.sub(r"file://[^\s]+", "", stripped, flags=re.IGNORECASE)
    # Decode common URL-encoding that can hide ".." (e.g. %2e%2e, %252e).
    try:
        import urllib.parse as _up
        # Two rounds of unquote to catch double-encoding.
        decoded = _up.unquote(_up.unquote(stripped))
    except Exception:
        decoded = stripped
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
    # Check absolute paths in arguments only, not the executable name.
    # Split into tokens and check from the second token onwards; quoted
    # spans are excluded for the same reason as above.
    for target in (stripped_unquoted, decoded_unquoted):
        tokens = target.split()
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
                text=True,
                errors="replace",
            )
            interval = 0.1
            deadline = time.monotonic() + timeout_f
            while True:
                if abort is not None and abort.is_set():
                    try:
                        proc.terminate()
                        try:
                            proc.wait(timeout=2)
                        except subprocess.TimeoutExpired:
                            proc.kill()
                    except OSError:
                        pass
                    raise AbortError("interrupted by operator")
                try:
                    stdout, stderr = proc.communicate(timeout=interval)
                    # Cap output to prevent OOM (host still buffers, but truncate)
                    if stdout and len(stdout) > _MAX_EXEC_BYTES:
                        stdout = stdout[:_MAX_EXEC_BYTES] + "\n... [truncated]"
                    if stderr and len(stderr) > _MAX_EXEC_BYTES:
                        stderr = stderr[:_MAX_EXEC_BYTES] + "\n... [truncated]"
                    return ExecResult(
                        exit_code=proc.returncode,
                        stdout=stdout or "",
                        stderr=stderr or "",
                    )
                except subprocess.TimeoutExpired:
                    if time.monotonic() >= deadline:
                        try:
                            proc.kill()
                            stdout, stderr = proc.communicate(timeout=2)
                        except Exception:
                            stdout, stderr = "", ""
                        if stdout and len(stdout) > _MAX_EXEC_BYTES:
                            stdout = stdout[:_MAX_EXEC_BYTES] + "\n... [truncated]"
                        if stderr and len(stderr) > _MAX_EXEC_BYTES:
                            stderr = stderr[:_MAX_EXEC_BYTES] + "\n... [truncated]"
                        return ExecResult(
                            exit_code=-1,
                            stdout=stdout or "",
                            stderr=stderr or "",
                            timed_out=True,
                        )
                    continue
        except OSError as exc:
            return ExecResult(exit_code=-1, stdout="", stderr=str(exc), timed_out=False)

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
        # Atomic tmp+replace; ensure tmp not symlink.
        tmp = full + ".tmp"
        if os.path.islink(tmp):
            raise SandboxError(f"path escapes sandbox workspace: {path}")
        try:
            with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(content)
            os.replace(tmp, full)
        except OSError:
            # Fallback direct write. The symlink checks above ran before
            # the tmp attempt; re-validate immediately so a path swapped
            # to a symlink in the meantime cannot redirect the write
            # outside the workspace.
            if os.path.islink(full) or os.path.islink(tmp):
                try:
                    if os.path.exists(tmp):
                        os.remove(tmp)
                except OSError:
                    pass
                raise SandboxError(f"path escapes sandbox workspace: {path}")
            with open(full, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(content)
            try:
                if os.path.exists(tmp):
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
                text=True,
                errors="replace",
            )
            interval = 0.1
            deadline = time.monotonic() + timeout_f
            while True:
                if abort is not None and abort.is_set():
                    try:
                        proc.terminate()
                        try:
                            proc.wait(timeout=2)
                        except subprocess.TimeoutExpired:
                            proc.kill()
                    except OSError:
                        pass
                    raise AbortError("interrupted by operator")
                try:
                    stdout, stderr = proc.communicate(timeout=interval)
                    if stdout and len(stdout) > _MAX_EXEC_BYTES:
                        stdout = stdout[:_MAX_EXEC_BYTES] + "\n... [truncated]"
                    if stderr and len(stderr) > _MAX_EXEC_BYTES:
                        stderr = stderr[:_MAX_EXEC_BYTES] + "\n... [truncated]"
                    return ExecResult(
                        exit_code=proc.returncode,
                        stdout=stdout or "",
                        stderr=stderr or "",
                    )
                except subprocess.TimeoutExpired:
                    if time.monotonic() >= deadline:
                        try:
                            proc.kill()
                            stdout, stderr = proc.communicate(timeout=2)
                        except Exception:
                            stdout, stderr = "", ""
                        if stdout and len(stdout) > _MAX_EXEC_BYTES:
                            stdout = stdout[:_MAX_EXEC_BYTES] + "\n... [truncated]"
                        if stderr and len(stderr) > _MAX_EXEC_BYTES:
                            stderr = stderr[:_MAX_EXEC_BYTES] + "\n... [truncated]"
                        return ExecResult(
                            exit_code=-1,
                            stdout=stdout or "",
                            stderr=stderr or "",
                            timed_out=True,
                        )
                    continue
        except OSError as exc:
            return ExecResult(exit_code=-1, stdout="", stderr=str(exc), timed_out=False)

    @staticmethod
    def _is_safe_repo_url(url: str) -> bool:
        url = url.strip()
        if not url or len(url) > 2048 or "\n" in url or "\r" in url or "\x00" in url:
            return False
        if url.startswith(("http://", "https://", "git@", "ssh://", "git://")):
            return True
        # File scheme is disabled by default because it allows reading
        # arbitrary local paths. Enable only for tests via env.
        if url.startswith("file://"):
            return bool(os.environ.get("MANTRA_ALLOW_FILE_URL"))
        return False

    @staticmethod
    def _is_safe_commit(commit: str) -> bool:
        commit = commit.strip()
        if not commit or len(commit) > 256 or "\n" in commit or "\r" in commit or "\x00" in commit:
            return False
        # block shell metacharacters
        if any(c in commit for c in (";", "&", "|", "`", "$", "(", ")", "<", ">", '"', "'")):
            return False
        return True
