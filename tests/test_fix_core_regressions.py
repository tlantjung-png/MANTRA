"""Regressions for core root modules and the console facade.

Fixes covered: High-1/2/6, Medium-1/2/3/4/18, Low-1/2/3/4, the console
halves of Low-6/Low-7, plus the headless and replay closing summaries.
All state lives in tmp_path; no real user store or project log is touched.
"""

from __future__ import annotations

import io
import json
import os
import stat
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from core.agent.exceptions import ConfigError, SandboxError
from core.config import DEFAULTS, merge_defaults
from core.container import DockerSandbox
from core.locking import acquire_lock, break_stale_lock, release_lock
from core.logs import JsonlLogger
from core.sandbox import LocalSandbox, warn_host_sandbox_risk
from core.urlcheck import is_safe_commit, is_safe_repo_url

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _write_config(tmp_path, **extra) -> str:
    data = {
        "evaluator": {"type": "none"},
        "logging": {"type": "jsonl", "path": str(tmp_path / "run.jsonl")},
    }
    data.update(extra)
    path = tmp_path / "config.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return str(path)


def _write_task(tmp_path) -> str:
    path = tmp_path / "task.json"
    path.write_text(json.dumps({"task_id": "t", "problem_statement": "do it"}), encoding="utf-8")
    return str(path)


class _Style:
    def dim(self, text): return text

    def warn(self, text): return text

    def ember(self, text): return text

    def brand(self, text): return text

    def _wrap(self, colour, text): return text


# ---- High-1: module invocation -------------------------------------------


def test_module_invocation_handles_slash_command(tmp_path) -> None:
    cfg = _write_config(tmp_path)
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "core.console",
            "--config",
            cfg,
            "--workspace",
            str(tmp_path),
            "--plain",
        ],
        input="/workspace\n/exit\n",
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        cwd=_PROJECT_ROOT,
        timeout=90,
        env={**os.environ, "NO_COLOR": "1"},
    )
    assert proc.returncode == 0, proc.stderr[-1500:]
    assert "NameError" not in proc.stderr
    assert "Traceback" not in proc.stderr


# ---- High-2: confirming default and permissive-mode warning --------------


def test_default_approval_mode_is_confirming() -> None:
    assert DEFAULTS["approvals"] == "default"
    assert merge_defaults({})["approvals"] == "default"


def test_headless_yolo_mode_warns(tmp_path, monkeypatch, capsys) -> None:
    import core.main as main_mod
    from core.scripted import ScriptedLLMClient, final_response

    monkeypatch.setattr(
        main_mod,
        "build_llm",
        lambda config: ScriptedLLMClient([final_response("done")]),
    )
    cfg = _write_config(tmp_path, approvals="yolo")
    code = main_mod.main(["--config", cfg, "--task", _write_task(tmp_path)])
    assert code == 0
    assert "yolo mode auto-approves" in capsys.readouterr().err


# ---- High-6: host-sandbox residual risk warning --------------------------


def test_host_sandbox_warning_is_explicit_and_once(monkeypatch) -> None:
    import core.sandbox as sandbox_mod

    monkeypatch.setattr(sandbox_mod, "_warned_host_sandbox", False)
    stream = io.StringIO()
    warn_host_sandbox_risk(stream)
    first = stream.getvalue()
    assert "containment" in first
    assert "container sandbox" in first
    warn_host_sandbox_risk(stream)
    assert stream.getvalue() == first  # one-time startup notice


# ---- Medium-1 / Medium-2: container and commit validation ----------------


def test_container_root_workdir_is_rejected() -> None:
    with pytest.raises(ValueError, match="root"):
        DockerSandbox._validate_workdir("/")
    good = DockerSandbox()
    assert good._is_safe_path("readme.md") is True
    assert good._is_safe_path("/etc/passwd") is False
    # Even a directly assigned root must not disable confinement.
    good.workdir = "/"
    assert good._is_safe_path("/etc/passwd") is False
    assert good._is_safe_path("../../etc/passwd") is False


def test_dash_prefixed_commits_rejected() -> None:
    assert is_safe_commit("abc123") is True
    for bad in ("--upload-pack=x", "--orphan", "-B", "-"):
        assert is_safe_commit(bad) is False
    assert LocalSandbox._is_safe_commit is is_safe_commit
    assert DockerSandbox._is_safe_commit is is_safe_commit


# ---- Medium-3: MCP client lifecycle --------------------------------------


def test_builder_failure_closes_spawned_clients(tmp_path, monkeypatch, capsys) -> None:
    import core.main as main_mod

    class _Client:
        closed = False

        def close(self) -> None:
            self.closed = True

    client = _Client()
    monkeypatch.setattr(main_mod, "build_llm", lambda config: object())
    monkeypatch.setattr(
        main_mod, "build_mcp_tools", lambda config, on_error=None: ([], [client])
    )

    def _boom(config):
        raise ConfigError("builder exploded")

    monkeypatch.setattr(main_mod, "build_evaluator", _boom)
    code = main_mod.main(["--config", _write_config(tmp_path), "--task", _write_task(tmp_path)])
    assert code == 2
    assert client.closed is True


def test_console_one_shot_closes_clients(tmp_path, monkeypatch) -> None:
    import core.console as console_mod

    class _Client:
        closed = False

        def close(self) -> None:
            self.closed = True

    class _Logger:
        closed = False

        def close(self) -> None:
            self.closed = True

    client = _Client()
    logger = _Logger()

    class _Session:
        def __init__(self):
            self.mcp_clients = [client]
            self.logger = logger

        def _warn_if_key_missing(self): pass

        def _warn_if_any_key_missing(self): pass

        def handle(self, message):
            return SimpleNamespace(stopped_reason="final")

    monkeypatch.setattr(console_mod, "ConsoleSession", lambda *a, **k: _Session())
    code = console_mod.main(
        [
            "--once",
            "hello",
            "--config",
            _write_config(tmp_path),
            "--workspace",
            str(tmp_path),
            "--plain",
        ]
    )
    assert code == 0
    assert client.closed is True
    assert logger.closed is True


# ---- Medium-4: redaction at the log sink ---------------------------------


def test_logger_redacts_credentials_at_the_sink(tmp_path) -> None:
    path = tmp_path / "run.jsonl"
    logger = JsonlLogger(str(path))
    secret = "sk-abcdefgh1234567890"
    logger.log("evt", {"msg": f"using {secret}", "nested": {"detail": secret}})
    logger.close()
    text = path.read_text(encoding="utf-8")
    assert secret not in text
    assert "[REDACTED]" in text


# ---- Medium-18: credential prompts are masked only -----------------------


def _fake_key_session(printed: list) -> SimpleNamespace:
    return SimpleNamespace(
        style=_Style(),
        endpoint_name="",
        config={"llm": {"base_url": "https://example.invalid/v1", "api_key_env": "TEST_KEY_ENV"}},
        _print=printed.append,
        use_endpoint=lambda name: True,
        refresh_title=lambda: None,
    )


def test_replace_key_never_uses_echoing_reader(monkeypatch) -> None:
    import core.console as console_mod
    import core.console_model as model_mod

    printed: list[str] = []
    session = _fake_key_session(printed)

    def _no_hidden(*args, **kwargs):
        raise RuntimeError("masked reader unavailable")

    def _echo(*args, **kwargs):
        raise AssertionError("an echoing reader must never read a credential")

    monkeypatch.setattr(console_mod, "_read_secret", _no_hidden)
    monkeypatch.setattr(console_mod, "_read_choice", _echo)
    assert model_mod._replace_key(session) is False
    assert any("hidden key entry unavailable" in line for line in printed)


def test_connect_new_uses_masked_reader(monkeypatch) -> None:
    import core.console as console_mod
    import core.console_model as model_mod

    prompts: list[str] = []

    def _hidden(session, prompt):
        prompts.append(prompt)
        return "sk-abcdefgh1234567890"

    def _echo(*args, **kwargs):
        raise AssertionError("an echoing reader must never read a credential")

    monkeypatch.setattr(console_mod, "_read_secret", _hidden)
    monkeypatch.setattr(console_mod, "_read_choice", _echo)
    monkeypatch.setattr(model_mod, "validate_endpoint", lambda entry: None)
    monkeypatch.setattr(model_mod, "add_endpoint", lambda *a, **k: None)
    monkeypatch.setattr(model_mod, "store_key", lambda *a, **k: None)
    monkeypatch.setattr(console_mod, "_choose_model", lambda session: True)
    session = _fake_key_session([])
    assert model_mod._connect_new(session, url="https://example.invalid/v1") is True
    assert prompts and "hidden" in prompts[0]


# ---- Low-1: non-object logging configuration -----------------------------


def test_non_object_logging_config_rejected(tmp_path, capsys) -> None:
    import core.main as main_mod

    with pytest.raises(ConfigError, match="sections must be objects"):
        merge_defaults({"logging": "not-an-object"})
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"logging": "not-an-object"}), encoding="utf-8")
    code = main_mod.main(["--config", str(cfg), "--task", _write_task(tmp_path)])
    assert code == 2
    assert "AttributeError" not in capsys.readouterr().err


# ---- Low-2: direct-write fallback does not follow a swapped target -------


def test_direct_write_refuses_swapped_target(tmp_path, monkeypatch) -> None:
    import tempfile

    box = LocalSandbox(workspace_root=str(tmp_path))
    box.setup({})

    def _no_temp(*args, **kwargs):
        raise OSError("no temp file")

    def _no_replace(*args, **kwargs):
        raise OSError("replace failed")

    monkeypatch.setattr(tempfile, "mkstemp", _no_temp)
    monkeypatch.setattr(os, "replace", _no_replace)
    real_lstat = os.lstat

    def _swapped_lstat(path):
        st = real_lstat(path)
        values = (
            st.st_mode, st.st_ino + 1, st.st_dev, st.st_nlink, st.st_uid,
            st.st_gid, st.st_size, st.st_atime, st.st_mtime, st.st_ctime,
        )
        return os.stat_result(values)

    monkeypatch.setattr(os, "lstat", _swapped_lstat)
    with pytest.raises(SandboxError, match="escapes"):
        box.write_file("f.txt", "payload")


# ---- Low-3: lock ownership -----------------------------------------------


def test_lock_is_owner_only_and_foreign_owner_cannot_break_it(tmp_path) -> None:
    path = tmp_path / "store.json.lock"
    fd = acquire_lock(str(path), wait=0.1, stale=1.0)
    assert fd is not None
    if os.name == "posix":
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    release_lock(str(path), fd)

    path.write_text("owner:somebody-else\n", encoding="utf-8")
    old = time.time() - 100
    os.utime(path, (old, old))
    assert break_stale_lock(str(path), stale_seconds=5.0) is False
    assert path.exists()


# ---- Low-4: strict file-URL opt-in ---------------------------------------


def test_file_url_needs_explicit_opt_in(monkeypatch) -> None:
    url = "file:///etc/passwd"
    for value in ("false", "no", "0", "yes", ""):
        monkeypatch.setenv("MANTRA_ALLOW_FILE_URL", value)
        assert is_safe_repo_url(url) is False
    for value in ("1", "true", "TRUE"):
        monkeypatch.setenv("MANTRA_ALLOW_FILE_URL", value)
        assert is_safe_repo_url(url) is True


# ---- Low-6 / Low-7: console state accessors ------------------------------


def test_pager_hint_check_is_locked() -> None:
    import core.console as console_mod

    session = object.__new__(console_mod.ConsoleSession)
    session._pager_lock = threading.Lock()
    session._pending_pages = []
    assert session._has_pending_pages() is False
    session._pending_pages.append(("title", ["line"]))
    assert session._has_pending_pages() is True


def test_turn_tool_text_has_a_locked_reader() -> None:
    import core.console as console_mod

    session = object.__new__(console_mod.ConsoleSession)
    ui = SimpleNamespace(lock=threading.Lock(), _turn_tool_text=[])
    session.ui = ui
    session._collect_turn_tool_text("run_command: ok")
    assert session.take_turn_tool_text() == ["run_command: ok"]
    assert session.take_turn_tool_text() == []


def test_existing_closing_summary_feature_is_preserved(monkeypatch) -> None:
    import core.console as console_mod

    printed: list[str] = []
    monkeypatch.setattr(
        console_mod.ConsoleSession,
        "_closing_summary",
        lambda self, result: "SUMMARY-SENTINEL",
    )
    monkeypatch.setattr(
        console_mod.ConsoleSession, "_print", lambda self, text="": printed.append(text)
    )
    session = object.__new__(console_mod.ConsoleSession)
    session._stream_tokens = 7
    session._end_turn(None)
    assert "SUMMARY-SENTINEL" in printed


# ---- Follow-up 1: headless closing summary -------------------------------


@pytest.mark.parametrize("passed", [True, False])
def test_headless_summary_is_last_stdout_line(tmp_path, monkeypatch, capsys, passed) -> None:
    import core.main as main_mod
    from core.agent.loop import RunResult

    result = RunResult(
        task_id="t",
        passed=passed,
        evaluation_detail="detail line",
        steps_used=2,
        stopped_reason="final" if passed else "error",
        elapsed_seconds=1.25,
    )

    class _Loop:
        def __init__(self, **kwargs): pass

        def run(self, task):
            return result

    monkeypatch.setattr(main_mod, "AgentLoop", _Loop)
    monkeypatch.setattr(main_mod, "build_llm", lambda config: object())
    code = main_mod.main(["--config", _write_config(tmp_path), "--task", _write_task(tmp_path)])
    assert code == (0 if passed else 1)
    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert lines[-1].startswith("Summary:")
    assert ("passed" if passed else "failed") in lines[-1]
    assert "2 steps" in lines[-1]


# ---- Follow-up 2: replay closing summary ---------------------------------


def test_replay_ends_with_a_closing_summary(tmp_path, monkeypatch) -> None:
    import core.agent.sessions as sessions
    from core.console_session_persist import PersistenceMixin

    printed: list[str] = []

    class _ReplaySession:
        resume_session = PersistenceMixin.resume_session

        def __init__(self):
            self.printed = printed
            self.style = _Style()
            self.context = SimpleNamespace(messages=[], tokens=12, enforce_budget=lambda: None)
            self.totals = {}
            self.message_count = 0
            self.todos = []
            self.session_name = ""
            self._show_tool_output = True
            self.layout = None
            self.workspace = str(tmp_path)

        def _print(self, text=""):
            printed.append(text)

        def _restore_pending_pages(self, data):
            return 0

        def _is_same_workspace(self, saved_ws):
            return True

    monkeypatch.setattr(
        sessions,
        "load",
        lambda name: {
            "workspace": str(tmp_path),
            "messages": [
                {"role": "user", "content": "hi"},
                {"role": "tool", "content": "raw tool output"},
            ],
        },
    )
    assert _ReplaySession().resume_session("demo") is True
    lines = [line for line in printed if line.strip()]
    assert lines[-1].startswith("Summary:")
    assert "replayed" in lines[-1]
