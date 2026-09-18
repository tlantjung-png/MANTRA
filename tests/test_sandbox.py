"""Host sandbox: confinement, traversal screening, env filtering, caps."""

from __future__ import annotations

import os

import pytest

from core.agent.exceptions import SandboxError
from core.sandbox import LocalSandbox, _filtered_env


@pytest.fixture()
def sandbox(tmp_path) -> LocalSandbox:
    box = LocalSandbox(workspace_root=str(tmp_path))
    box.setup({})
    yield box
    box.cleanup()


def test_workspace_root_used(sandbox, tmp_path) -> None:
    assert os.path.realpath(sandbox.root) == os.path.realpath(str(tmp_path))


def test_owned_root_creates_and_cleans_temp_dir() -> None:
    box = LocalSandbox()
    box.setup({})
    root = box.root
    assert os.path.isdir(root)
    box.cleanup()
    assert not os.path.isdir(root)
    # Idempotent cleanup: a second call must not raise.
    box.cleanup()


def test_root_before_setup_raises() -> None:
    box = LocalSandbox()
    with pytest.raises(SandboxError):
        _ = box.root


def test_read_file_within_workspace(sandbox, tmp_path) -> None:
    (tmp_path / "f.txt").write_text("hello", encoding="utf-8")
    assert sandbox.read_file("f.txt") == "hello"


def test_read_file_escapes_rejected(sandbox) -> None:
    with pytest.raises(SandboxError):
        sandbox.read_file("../outside.txt")


def test_read_file_capped(sandbox, tmp_path) -> None:
    (tmp_path / "big.txt").write_text("a" * 600_000, encoding="utf-8")
    out = sandbox.read_file("big.txt")
    assert len(out) < 600_000
    assert "truncated" in out


def test_write_and_read_roundtrip(sandbox) -> None:
    sandbox.write_file("nested/dir/f.txt", "content")
    assert sandbox.read_file("nested/dir/f.txt") == "content"
    assert "nested/dir/f.txt".replace("\\", "/") in sandbox.changed


def test_write_escapes_rejected(sandbox, tmp_path) -> None:
    with pytest.raises(SandboxError):
        sandbox.write_file("../escape.txt", "nope")
    assert not (tmp_path.parent / "escape.txt").exists()


def test_write_via_symlink_parent_rejected(sandbox, tmp_path) -> None:
    outside = tmp_path.parent / "outside-dir"
    outside.mkdir(exist_ok=True)
    link = tmp_path / "link"
    try:
        os.symlink(str(outside), str(link))
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable on this platform")
    with pytest.raises(SandboxError):
        sandbox.write_file("link/escape.txt", "nope")


def test_write_content_cap(sandbox) -> None:
    with pytest.raises(SandboxError, match="too large"):
        sandbox.write_file("huge.txt", "x" * (500_000 * 2 + 1))


def test_exec_timeout_range(sandbox) -> None:
    result = sandbox.exec("echo hi", timeout=0)
    assert result.exit_code == -1
    assert "timeout out of range" in result.stderr


def test_exec_traversal_screened(sandbox) -> None:
    result = sandbox.exec("cat ../secrets.txt")
    assert result.exit_code == -1
    assert "blocked" in result.stderr


def test_exec_invalid_timeout_type(sandbox) -> None:
    result = sandbox.exec("echo hi", timeout="soon")  # type: ignore[arg-type]
    assert result.exit_code == -1
    assert "invalid timeout" in result.stderr


def test_screen_command_shared_by_background_path(sandbox) -> None:
    reason = sandbox.screen_command("rm -rf ../")
    assert reason is not None
    assert "blocked" in reason
    assert sandbox.screen_command("echo hello") is None


def test_filtered_env_strips_credentials() -> None:
    env = {
        "PATH": "/bin",
        "HOME": "/home/u",
        "API_KEY": "secret",
        "MY_TOKEN": "t",
        "DB_PASSWORD": "p",
        "MANTRA_CREDENTIALS": "c",
        "USERPROFILE": "/home/u",
    }
    filtered = _filtered_env(env)
    assert filtered["PATH"] == "/bin"
    assert "API_KEY" not in filtered
    assert "MY_TOKEN" not in filtered
    assert "DB_PASSWORD" not in filtered
    assert "MANTRA_CREDENTIALS" not in filtered
    assert filtered["USERPROFILE"] == "/home/u"


def test_exec_child_env_is_filtered(sandbox) -> None:
    os.environ["SANDBOX_PROBE_SECRET"] = "topsecret"
    try:
        result = sandbox.exec("echo $SANDBOX_PROBE_SECRET")
    finally:
        del os.environ["SANDBOX_PROBE_SECRET"]
    assert "topsecret" not in result.stdout


def test_repo_url_scheme_screening(tmp_path) -> None:
    box = LocalSandbox(workspace_root=str(tmp_path))
    box.setup({})
    try:
        with pytest.raises(SandboxError, match="repo_url rejected"):
            box.setup({"repo_url": "ftp://example.invalid/x.git"})
        with pytest.raises(SandboxError, match="repo_url rejected"):
            box.setup({"repo_url": "file:///etc"})
    finally:
        box.cleanup()


def test_base_commit_metacharacters_rejected() -> None:
    # Screened statically before any clone attempt.
    assert LocalSandbox._is_safe_commit("abc123") is True
    for bad in ("abc; rm -rf /", "abc & calc", "a`id`", "a$(id)", "a\nb", "a\x00b", ""):
        assert LocalSandbox._is_safe_commit(bad) is False, bad


def test_abort_event_interrupts_exec(sandbox) -> None:
    import threading

    sandbox.abort = threading.Event()
    sandbox.abort.set()
    with pytest.raises(Exception, match="interrupted"):
        sandbox.exec("echo hi")
