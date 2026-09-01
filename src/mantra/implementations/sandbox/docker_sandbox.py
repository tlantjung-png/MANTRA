"""Container sandbox via docker CLI; one container per run."""

from __future__ import annotations

import os
import subprocess
import tempfile
import time
import uuid

from mantra.core.exceptions import AbortError, SandboxError
from mantra.interfaces.sandbox import ExecResult, Sandbox

_EXEC_TIMEOUT = 600.0
_MAX_READ_BYTES = 500_000
_MAX_EXEC_BYTES = 1_000_000


class DockerSandbox(Sandbox):
    """One container per run."""

    def __init__(
        self,
        image: str = "python:3.11-slim",
        mem_limit: str = "2g",
        network_enabled_during_setup: bool = True,
        workdir: str = "/workspace",
    ) -> None:
        # Validate inputs early to surface config errors near construction,
        # not deep inside setup.
        self.image = self._validate_image(image)
        self.mem_limit = self._validate_mem_limit(mem_limit)
        self.network_enabled_during_setup = bool(network_enabled_during_setup)
        self.workdir = self._validate_workdir(workdir)
        self._container_id: str | None = None

    def setup(self, task: dict) -> None:
        self._container_id = f"mantra-{uuid.uuid4().hex[:12]}"
        network = "--network none" if not self.network_enabled_during_setup else ""
        if not self._run_cli(
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
                "-w",
                self.workdir,
                *network.split(),
                self.image,
                "sleep",
                "infinity",
            ]
        ):
            raise SandboxError("docker run failed to start the container")

        repo_url = task.get("repo_url")
        if repo_url:
            repo_str = str(repo_url)
            if not self._is_safe_repo_url(repo_str):
                raise SandboxError(f"repo_url rejected: {repo_str!r}")
            result = self._exec_no_shell(
                ["git", "clone", repo_str, "."], timeout=300
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
        if setup_cmd and self.exec(setup_cmd, timeout=600).exit_code != 0:
            raise SandboxError(f"setup_cmd failed in container {self._container_id}")

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
                text=True,
                errors="replace",
            )
            interval = 0.1
            limit = min(timeout_f, _EXEC_TIMEOUT)
            deadline = time.monotonic() + limit
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
                            exit_code=-1, stdout=stdout or "", stderr=stderr or "", timed_out=True
                        )
                    continue
        except OSError as exc:
            return ExecResult(exit_code=-1, stdout="", stderr=str(exc), timed_out=False)

    def _is_safe_path(self, path: str) -> bool:
        if not path or "\x00" in path or "\n" in path or "\r" in path or ":" in path:
            return False
        # Validate fully joined container path.
        import posixpath

        joined = path if os.path.isabs(path) else posixpath.join(self.workdir, path)
        normalized = posixpath.normpath(joined.replace("\\", "/"))
        wd = self.workdir.rstrip("/")
        if normalized == wd or normalized.startswith(wd + "/"):
            if ".." in normalized.split("/"):
                return False
            return True
        return False

    def read_file(self, path: str) -> str:
        if not self._is_safe_path(path):
            raise SandboxError(f"path escapes sandbox workspace: {path}")
        result = self._exec_no_shell(["cat", path])
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
        if len(content) > _MAX_READ_BYTES * 2:
            raise SandboxError(f"content too large ({len(content)} bytes)")
        # Stage locally, then docker cp; avoids shell-quoting hazards entirely.
        # Ensure destination is absolute inside workdir to avoid relative ambiguity
        dest = path
        if not os.path.isabs(dest):
            dest = os.path.join(self.workdir, dest)
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
            completed = subprocess.run(
                [
                    "docker",
                    "cp",
                    temp_path,
                    f"{self._container_id}:{dest}",
                ],
                capture_output=True,
                text=True,
                timeout=60,
            )
            if completed.returncode != 0:
                raise SandboxError(
                    f"write_file failed for {path}: {(completed.stderr or '')[:500]}"
                )
        finally:
            if os.path.exists(temp_path):
                os.unlink(temp_path)

    def cleanup(self) -> None:
        if self._container_id is not None:
            self._run_cli(["docker", "rm", "-f", self._container_id])
            self._container_id = None

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
                text=True,
                errors="replace",
            )
            interval = 0.1
            limit = min(timeout_f, _EXEC_TIMEOUT)
            deadline = time.monotonic() + limit
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
                            exit_code=-1, stdout=stdout or "", stderr=stderr or "", timed_out=True
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
        if url.startswith("file://"):
            return bool(os.environ.get("MANTRA_ALLOW_FILE_URL"))
        return False

    @staticmethod
    def _is_safe_commit(commit: str) -> bool:
        commit = commit.strip()
        if not commit or len(commit) > 256 or "\n" in commit or "\r" in commit or "\x00" in commit:
            return False
        if any(c in commit for c in (";", "&", "|", "`", "$", "(", ")", "<", ">", '"', "'")):
            return False
        return True

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
    def _run_cli(args: list[str]) -> bool:
        try:
            completed = subprocess.run(
                args, capture_output=True, text=True, timeout=120
            )
            return completed.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False
