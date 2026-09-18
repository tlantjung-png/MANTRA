"""Container sandbox via docker CLI; one container per run."""

from __future__ import annotations

import os
import subprocess
import tempfile
import threading
import time
import uuid

from core.agent.exceptions import AbortError, SandboxError
from core.procutil import POPEN_GROUP_KWARGS as _POPEN_GROUP_KWARGS
from core.procutil import read_pipe_into as _read_pipe_into
from core.urlcheck import is_safe_commit as _is_safe_commit_impl
from core.urlcheck import is_safe_repo_url as _is_safe_repo_url_impl
from core.types import ExecResult, Sandbox

_EXEC_TIMEOUT = 600.0
_MAX_READ_BYTES = 500_000
_MAX_EXEC_BYTES = 1_000_000


class DockerSandbox(Sandbox):
    """One container per run."""

    def __init__(
        self,
        image: str = "python:3.11-slim",
        mem_limit: str = "2g",
        # Applies for the container's whole lifetime: docker network options
        # are fixed at run time, so a disabled network cannot be enabled for
        # setup alone. The name says what the flag actually does.
        network_enabled_for_lifetime: bool = True,
        workdir: str = "/workspace",
    ) -> None:
        # Validate inputs early to surface config errors near construction,
        # not deep inside setup.
        self.image = self._validate_image(image)
        self.mem_limit = self._validate_mem_limit(mem_limit)
        self.network_enabled_for_lifetime = bool(network_enabled_for_lifetime)
        self.workdir = self._validate_workdir(workdir)
        self._container_id: str | None = None

    def setup(self, task: dict) -> None:
        self._container_id = f"mantra-{uuid.uuid4().hex[:12]}"
        network = "--network none" if not self.network_enabled_for_lifetime else ""
        # Detached container kept alive with `sleep infinity` so later
        # docker exec calls have a running target. Hardened: pids-limit
        # bounds runaway processes, cap-drop ALL and no-new-privileges
        # shrink the root-user attack surface, and a non-root --user keeps
        # the container from operating as root at all.
        completed = self._run_cli(
            [
                "docker",
                "run",
                "-d",
                "--name",
                self._container_id,
                "--memory",
                self.mem_limit,
                "--cpus",
                "1.0",
                "--pids-limit",
                "512",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                "--user",
                "1000:1000",
                "-w",
                self.workdir,
                *network.split(),
                self.image,
                "sleep",
                "infinity",
            ]
        )
        if completed is None or completed.returncode != 0:
            stderr = (completed.stderr or "").strip() if completed else "docker CLI failed to run"
            self._container_id = None
            raise SandboxError(f"docker run failed to start the container: {stderr[:500]}")

        # A setup failure after the container starts must not leak the
        # container (it holds its memory/cpus reservation): remove it
        # before surfacing the error.
        try:
            repo_url = task.get("repo_url")
            if repo_url:
                repo_str = str(repo_url)
                if not self._is_safe_repo_url(repo_str):
                    raise SandboxError(f"repo_url rejected: {repo_str!r}")
                result = self._exec_no_shell(
                    ["git", "clone", repo_str, "."], timeout=task.get("clone_timeout", 300)
                )
                if result.exit_code != 0:
                    raise SandboxError(f"git clone failed: {result.stderr[:2000]}")
                commit = task.get("base_commit")
                if commit:
                    commit_str = str(commit)
                    if not self._is_safe_commit(commit_str):
                        raise SandboxError(f"base_commit rejected: {commit_str!r}")
                    if self._exec_no_shell(
                        ["git", "checkout", commit_str], timeout=60
                    ).exit_code != 0:
                        raise SandboxError(f"git checkout failed for {commit_str}")

            setup_cmd = task.get("setup_cmd")
            if setup_cmd and self.exec(setup_cmd, timeout=task.get("setup_timeout", 600)).exit_code != 0:
                raise SandboxError(f"setup_cmd failed in container {self._container_id}")
        except Exception:
            try:
                self.cleanup()
            except Exception:
                pass  # cleanup is best effort; the setup error still propagates
            raise

    def exec(self, command: str, timeout: float = 120.0) -> ExecResult:
        if self._container_id is None:
            raise SandboxError("sandbox not set up")
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
                ["docker", "exec", self._container_id, "sh", "-lc", command],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except OSError as exc:
            return ExecResult(exit_code=-1, stdout="", stderr=str(exc), timed_out=False)
        return self._pump(proc, timeout_f)

    def _pump(self, proc: subprocess.Popen, timeout_f: float) -> ExecResult:
        """Collect output incrementally until exit, cap, deadline, or abort.

        Reader threads fill capped buffers as bytes arrive, so a chatty
        command never buffers its full output in RAM. A timeout or abort
        kills the docker exec client and then reaps in-container children
        so the runaway command cannot keep the container's CPU/memory
        reservation past the turn.
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
        limit = min(timeout_f, _EXEC_TIMEOUT)  # clamp: no exec runs forever
        deadline = time.monotonic() + limit
        try:
            while True:
                if abort is not None and abort.is_set():
                    self._kill_exec(proc)
                    raise AbortError("interrupted by operator")
                if out_done.is_set() and err_done.is_set():
                    break
                if len(out_buf) >= _MAX_EXEC_BYTES or len(err_buf) >= _MAX_EXEC_BYTES:
                    self._kill_exec(proc)
                    break
                if time.monotonic() >= deadline:
                    timed_out = True
                    self._kill_exec(proc)
                    break
                time.sleep(interval)
        finally:
            # After a kill the pipes close once the exec client is gone;
            # the reader threads then hit EOF and release the handles.
            t_out.join(timeout=2)
            t_err.join(timeout=2)
        stdout = out_buf.decode("utf-8", errors="replace")
        stderr = err_buf.decode("utf-8", errors="replace")
        if len(out_buf) >= _MAX_EXEC_BYTES:
            stdout = stdout[:_MAX_EXEC_BYTES] + "\n... [truncated]"
        if len(err_buf) >= _MAX_EXEC_BYTES:
            stderr = stderr[:_MAX_EXEC_BYTES] + "\n... [truncated]"
        if timed_out:
            return ExecResult(exit_code=-1, stdout=stdout, stderr=stderr, timed_out=True)
        exit_code = proc.poll()
        if exit_code is None:
            exit_code = -1
        return ExecResult(exit_code=exit_code, stdout=stdout, stderr=stderr)

    def _is_safe_path(self, path: str) -> bool:
        if not path or "\x00" in path or "\n" in path or "\r" in path or ":" in path:
            return False
        # Validate fully joined container path. Container paths are POSIX
        # paths regardless of the host OS, so posixpath is used throughout:
        # os.path on a Windows host would treat "/workspace" as relative
        # and normalise with backslashes.
        import posixpath

        joined = path if posixpath.isabs(path) else posixpath.join(self.workdir, path)
        normalized = posixpath.normpath(joined.replace("\\", "/"))
        wd = self.workdir.rstrip("/")
        if normalized == wd or normalized.startswith(wd + "/"):
            if ".." in normalized.split("/"):
                return False
            return True
        return False

    def _resolved_path(self, path: str) -> str:
        """The container's view of the path with symlinks resolved; best effort."""
        import posixpath

        joined = path if posixpath.isabs(path) else posixpath.join(self.workdir, path)
        # Single sh -lc invocation so the path arrives as one argv element
        # and no shell word-splitting can alter it.
        result = self._exec_no_shell(
            ["sh", "-lc", 'readlink -f -- "$1"', "sh", joined], timeout=15
        )
        if result.exit_code == 0:
            resolved = result.stdout.strip()
            if resolved:
                return resolved
        return posixpath.normpath(joined.replace("\\", "/"))

    def read_file(self, path: str) -> str:
        if not self._is_safe_path(path):
            raise SandboxError(f"path escapes sandbox workspace: {path}")
        # Resolve and read in a single exec so a symlink cannot be swapped
        # between a separate readlink round-trip and the read. The shell
        # re-validates that the resolved target stays inside the workdir
        # before cat'ing it; exit 9 means the resolved path escaped.
        import posixpath

        joined = path if posixpath.isabs(path) else posixpath.join(self.workdir, path)
        wd = self.workdir.rstrip("/")
        script = (
            'p=$(readlink -f -- "$1"); '
            'case "$p" in '
            '"$2"|"$2"/*) ;; '
            '*) exit 9 ;; '
            'esac; '
            'cat "$p"'
        )
        result = self._exec_no_shell(
            ["sh", "-lc", script, "sh", joined, wd], timeout=15
        )
        if result.exit_code == 9:
            raise SandboxError(f"path escapes sandbox workspace: {path}")
        if result.exit_code != 0:
            raise SandboxError(f"read_file failed for {path}: {result.stderr[:500]}")
        if len(result.stdout) > _MAX_READ_BYTES:
            return result.stdout[:_MAX_READ_BYTES] + "\n... [truncated]"
        return result.stdout

    def write_file(self, path: str, content: str) -> None:
        if self._container_id is None:
            raise SandboxError("sandbox not set up")
        if not self._is_safe_path(path):
            raise SandboxError(f"path escapes sandbox workspace: {path}")
        # Resolve before copying too: an existing symlink at the
        # destination must not redirect the write outside the workdir.
        resolved = self._resolved_path(path)
        if not self._is_safe_path(resolved):
            raise SandboxError(f"path escapes sandbox workspace: {path}")
        # A directory destination would make the copy nest the staged
        # file inside itself instead of writing the intended path.
        dir_check = self._exec_no_shell(["test", "-d", resolved])
        if dir_check.exit_code == 0:
            raise SandboxError(f"write_file destination is a directory: {path}")
        content_bytes = len(content.encode("utf-8"))
        if content_bytes > _MAX_READ_BYTES * 2:
            raise SandboxError(f"content too large ({content_bytes} bytes)")
        # Stage the content in a host temp file (owner-only) and copy it
        # in, avoiding shell-quoting issues with arbitrary content.
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", newline="\n", suffix=".harness", delete=False
        ) as handle:
            handle.write(content)
            temp_path = handle.name
        try:
            os.chmod(temp_path, 0o600)
        except OSError:
            pass
        try:
            # Re-resolve and re-validate immediately before the copy: a
            # symlink swapped in since the earlier check must not redirect
            # the write outside the workdir.
            resolved = self._resolved_path(path)
            if not self._is_safe_path(resolved):
                raise SandboxError(f"path escapes sandbox workspace: {path}")
            try:
                completed = subprocess.run(
                    [
                        "docker",
                        "cp",
                        temp_path,
                        f"{self._container_id}:{resolved}",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
            except subprocess.TimeoutExpired:
                raise SandboxError(
                    f"write_file timed out copying to {path}"
                ) from None
            if completed.returncode != 0:
                raise SandboxError(
                    f"write_file failed for {path}: {(completed.stderr or '')[:500]}"
                )
        finally:
            if os.path.exists(temp_path):
                os.unlink(temp_path)

    def cleanup(self) -> None:
        if self._container_id is not None:
            completed = self._run_cli(["docker", "rm", "-f", self._container_id])
            if completed is None or completed.returncode != 0:
                # A failed removal leaks the container (memory/cpus stay
                # reserved); both call sites swallow cleanup errors, so
                # raise with the diagnostic rather than hiding it.
                stderr = (completed.stderr or "").strip() if completed else "docker CLI failed to run"
                raise SandboxError(
                    f"failed to remove container {self._container_id}: {stderr[:500]}"
                )
            self._container_id = None

    def _kill_exec(self, proc: subprocess.Popen) -> None:
        """Kill the docker exec client, then reap the in-container command.

        Killing the client does not stop what runs inside the container,
        so the reap runs after the kill on every timeout/abort path.
        """
        try:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=2)
                except (OSError, subprocess.SubprocessError):
                    pass
        except OSError:
            try:
                proc.kill()
            except OSError:
                pass
        self._kill_container_children()

    def _kill_container_children(self) -> None:
        """Best-effort reap of in-container processes after a timeout/abort.

        Killing the docker exec client does not stop the command running
        inside the container, so ask the container to reap its own
        children. Only children of PID 1 are signalled; PID 1 itself is
        the ``sleep infinity`` keeper and must survive, so a broadcast
        to every process is never used. Never raises: best effort only.
        """
        if self._container_id is None:
            return
        try:
            subprocess.run(
                [
                    "docker",
                    "exec",
                    self._container_id,
                    "sh",
                    "-lc",
                    "pkill -TERM -P 1 2>/dev/null; "
                    "for p in $(ps -o pid= --ppid 1 2>/dev/null); do kill -TERM \"$p\" 2>/dev/null; done; "
                    "sleep 1; pkill -KILL -P 1 2>/dev/null; true",
                ],
                capture_output=True,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass

    def _exec_no_shell(self, args: list[str], timeout: float = 120.0) -> ExecResult:
        if self._container_id is None:
            raise SandboxError("sandbox not set up")
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
                ["docker", "exec", self._container_id, *args],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except OSError as exc:
            return ExecResult(exit_code=-1, stdout="", stderr=str(exc), timed_out=False)
        return self._pump(proc, timeout_f)

    # Shared refusal rules live in core.urlcheck; re-exported as
    # staticmethods so callers and tests keep the DockerSandbox seams.
    _is_safe_repo_url = staticmethod(_is_safe_repo_url_impl)
    _is_safe_commit = staticmethod(_is_safe_commit_impl)

    @staticmethod
    def _validate_image(image: str) -> str:
        import re
        img = (image or "").strip()
        if not img or len(img) > 256 or "\n" in img or "\r" in img or "\x00" in img:
            raise ValueError(f"invalid container image {image!r}")
        # Allow repo/image:tag@digest with alnum, ., -, _, /, :
        if not re.match(r"^[a-zA-Z0-9._\-/:@]+$", img):
            raise ValueError(f"invalid container image {image!r}")
        if ".." in img or img.startswith("-") or img.startswith("/"):
            raise ValueError(f"invalid container image {image!r}")
        return img

    @staticmethod
    def _validate_mem_limit(mem: str) -> str:
        import re
        m = (mem or "").strip().lower()
        if not re.match(r"^\d+(\.\d+)?[kmg]?b?$", m):
            raise ValueError(f"invalid memory limit {mem!r} (expected like 512m, 2g, 1g)")
        # Parse numeric part and enforce sane range 64m to 16g
        num_str = re.match(r"^(\d+(?:\.\d+)?)", m).group(1)  # type: ignore
        try:
            num = float(num_str)
        except ValueError:
            raise ValueError(f"invalid memory limit {mem!r}")
        unit = m[len(num_str):]
        # Normalize to bytes for range check
        mult = 1
        if unit.startswith("k"):
            mult = 1024
        elif unit.startswith("m"):
            mult = 1024 * 1024
        elif unit.startswith("g"):
            mult = 1024 * 1024 * 1024
        bytes_val = num * mult
        if bytes_val < 64 * 1024 * 1024 or bytes_val > 16 * 1024 * 1024 * 1024:
            raise ValueError(f"memory limit {mem!r} out of range (64m to 16g)")
        return m

    @staticmethod
    def _validate_workdir(wd: str) -> str:
        import posixpath
        w = (wd or "").strip()
        if not w.startswith("/"):
            raise ValueError(f"workdir must be an absolute POSIX path, got {wd!r}")
        if "\x00" in w or "\n" in w or "\r" in w or ":" in w:
            raise ValueError(f"invalid workdir {wd!r}")
        norm = posixpath.normpath(w)
        if norm != w:
            raise ValueError(f"workdir must be normalized, got {wd!r} expected {norm!r}")
        return w

    @staticmethod
    def _run_cli(args: list[str]) -> subprocess.CompletedProcess | None:
        """Run a docker CLI command, returning the result (None on failure).

        The caller inspects ``returncode``/``stderr``; a swallowed failure
        would otherwise leave no diagnostic for a failed run or cleanup.
        """
        try:
            return subprocess.run(args, capture_output=True, text=True, timeout=120)
        except (OSError, subprocess.TimeoutExpired):
            return None
