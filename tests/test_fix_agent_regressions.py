"""Regressions for the agent-subsystem audit fixes.

Each test uses temporary stores and self-contained stubs; nothing here
touches the operator's home or the project's logs.
"""

from __future__ import annotations

import builtins
import json
import threading

import pytest

import core.agent.approvals as approvals_mod
import core.agent.events as events_mod
import core.agent.keys as keys_mod
import core.agent.knowledge as knowledge_mod
import core.agent.permissions as permissions_mod
import core.agent.settings as settings_mod
import core.agent.sessions as sessions_mod
from core.agent.approvals import ApprovalPolicy, classify, register_external_tool
from core.agent.context import ContextManager
from core.agent.events import EventBus
from core.agent.loop import AgentLoop, _result_payload
from core.agent.settings import DEFAULT_FILE
from core.evaluators import NullEvaluator
from core.logs import JsonlLogger
from core.sandbox import LocalSandbox
from core.scripted import ScriptedLLMClient, final_response, tool_call_response
from core.tools.files import WriteFileTool


# ---- High-2 (settings half) ------------------------------------------------


def test_settings_default_approval_is_confirming(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("MANTRA_SETTINGS", str(tmp_path / "config.json"))
    assert DEFAULT_FILE["ui"]["approvals"] == "default"
    assert settings_mod.load()["ui"]["approvals"] == "default"


# ---- High-4 and Low-26 -----------------------------------------------------


def test_fenced_file_protection_covers_path_aliases() -> None:
    # Literal and alias path keys must both classify as confirm.
    assert classify("write_file", {"path": "AGENTS.md"})[0] == "confirm"
    assert classify("edit_file", {"path": "sub/MEMORY.md"})[0] == "confirm"
    assert classify("write_file", {"filePath": "AGENTS.md"})[0] == "confirm"
    assert classify("edit_file", {"filePath": "sub/MEMORY.md"})[0] == "confirm"
    assert classify("write_file", {"file_path": "AGENTS.md"})[0] == "confirm"


class _RecordingApprover:
    def __init__(self, allow: bool) -> None:
        self.allow = allow
        self.seen: list[tuple[str, dict]] = []

    def check(self, tool: str, arguments: dict) -> bool:
        self.seen.append((tool, dict(arguments)))
        return self.allow


class _RecordingWrite(WriteFileTool):
    def __init__(self) -> None:
        self.executed: list[dict] = []

    def execute(self, sandbox, **kwargs) -> str:  # type: ignore[override]
        self.executed.append(dict(kwargs))
        return "OK: recorded"


def _loop_with(llm, tmp_path, **kwargs) -> AgentLoop:
    sandbox = LocalSandbox(workspace_root=str(tmp_path))
    sandbox.setup({})
    return AgentLoop(
        llm=llm,
        sandbox=sandbox,
        tools=kwargs.pop("tools", []),
        evaluator=NullEvaluator(),
        logger=JsonlLogger(str(tmp_path / "run.jsonl")),
        **kwargs,
    )


def _serialized_call_args(loop: AgentLoop) -> dict:
    for message in loop.context.messages:
        for call in message.get("tool_calls") or []:
            return json.loads(call["function"]["arguments"])
    raise AssertionError("no serialized tool call found")


def test_alias_arguments_repaired_before_approval_and_serialization(tmp_path) -> None:
    script = ScriptedLLMClient(
        [
            tool_call_response("write_file", {"filePath": "AGENTS.md", "content": "x"}),
            final_response("stopped"),
        ]
    )
    approver = _RecordingApprover(allow=False)
    loop = _loop_with(script, tmp_path, tools=[_RecordingWrite()], approver=approver)
    loop.run({"task_id": "t", "problem_statement": "fenced write"})

    # The approver sees the repaired canonical arguments, not the raw alias.
    assert approver.seen and approver.seen[0][1] == {"path": "AGENTS.md", "content": "x"}
    # What is recorded in history is the same repaired object.
    assert _serialized_call_args(loop) == {"path": "AGENTS.md", "content": "x"}


def test_executed_arguments_match_the_repaired_and_serialized_arguments(tmp_path) -> None:
    script = ScriptedLLMClient(
        [
            tool_call_response("write_file", {"filePath": "notes.txt", "content": "x"}),
            final_response("done"),
        ]
    )
    tool = _RecordingWrite()
    approver = _RecordingApprover(allow=True)
    loop = _loop_with(script, tmp_path, tools=[tool], approver=approver)
    loop.run({"task_id": "t", "problem_statement": "alias write"})

    expected = {"path": "notes.txt", "content": "x"}
    assert approver.seen[0][1] == expected
    assert _serialized_call_args(loop) == expected
    assert tool.executed == [expected]


# ---- High-5 ---------------------------------------------------------------


class _SummaryLLM:
    def __init__(self, content: str | None = None, raises: bool = False) -> None:
        self.content = content
        self.raises = raises

    def chat(self, messages, **kwargs):
        if self.raises:
            raise RuntimeError("summariser temporarily down")
        from core.types import LLMResponse

        return LLMResponse(content=self.content)


def _context_with_pending() -> tuple[ContextManager, list[dict]]:
    context = ContextManager()
    context.seed("sys", "task")
    turns = [
        {"role": "assistant", "content": "A" * 300},
        {"role": "user", "content": "B" * 300},
    ]
    context.pending_evicted = list(turns)
    return context, turns


def _bare_loop(llm, tmp_path) -> AgentLoop:
    return AgentLoop(
        llm=llm,
        sandbox=LocalSandbox(workspace_root=str(tmp_path)),
        tools=[],
        evaluator=NullEvaluator(),
        logger=JsonlLogger(str(tmp_path / "digest.jsonl")),
    )


def test_digest_exception_keeps_evicted_turns(tmp_path) -> None:
    context, turns = _context_with_pending()
    loop = _bare_loop(_SummaryLLM(raises=True), tmp_path)
    metrics = {"digest_failures": 0}
    loop._fold_evicted(context, metrics)
    assert context.pending_evicted == turns
    assert context.digest is None
    assert metrics["digest_failures"] == 1
    assert context.evicted_without_digest == len(turns)


def test_empty_digest_keeps_evicted_turns(tmp_path) -> None:
    context, turns = _context_with_pending()
    loop = _bare_loop(_SummaryLLM(content=""), tmp_path)
    metrics = {"digest_failures": 0}
    loop._fold_evicted(context, metrics)
    assert context.pending_evicted == turns
    assert context.digest is None
    assert metrics["digest_failures"] == 1
    assert context.evicted_without_digest == len(turns)


def test_successful_digest_detaches_evicted_turns(tmp_path) -> None:
    context, _ = _context_with_pending()
    loop = _bare_loop(_SummaryLLM(content="a useful summary"), tmp_path)
    metrics = {"digest_failures": 0, "digest_turns": 0}
    loop._fold_evicted(context, metrics)
    assert context.pending_evicted == []
    assert context.digest == "a useful summary"
    assert metrics["digest_turns"] == 1
    assert metrics["digest_failures"] == 0


# ---- Medium-5 and Medium-6 -------------------------------------------------


def _reset_audit_globals(monkeypatch) -> None:
    monkeypatch.setattr(approvals_mod, "_REDACTION_OK", True)
    monkeypatch.setattr(approvals_mod, "_REDACTION_WARNED", False)
    monkeypatch.setattr(approvals_mod, "_LOG_WRITE_WARNED", False)
    monkeypatch.setattr(approvals_mod, "_AUDIT_HEALTHY", True)


def test_stored_credential_redaction_failure_is_reported(monkeypatch, capsys, tmp_path) -> None:
    _reset_audit_globals(monkeypatch)
    audit = tmp_path / "audit.log"
    monkeypatch.setenv("MANTRA_PRE_TOOL_USE_LOG", str(audit))

    def boom():
        raise OSError("credential store unreadable")

    monkeypatch.setattr(keys_mod, "stored_keys", boom)
    approvals_mod._redact_sensitive("token=abcdefghijklmnop")
    assert approvals_mod.redaction_ok() is False
    assert "redaction unavailable" in capsys.readouterr().err

    notes: list[str] = []
    policy = ApprovalPolicy(
        mode="default", ask=lambda prompt: "y", note=notes.append
    )
    assert policy.check("write_file", {"path": "a.txt"}) is False
    assert approvals_mod.audit_log_healthy() is False
    assert notes
    # No entry was written while redaction was incomplete.
    assert not audit.exists() or "write_file" not in audit.read_text(encoding="utf-8")


def test_unwritable_audit_log_refuses_unlogged_mutations(monkeypatch, tmp_path) -> None:
    _reset_audit_globals(monkeypatch)
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setenv("MANTRA_PRE_TOOL_USE_LOG", str(blocker / "audit.log"))

    notes: list[str] = []
    policy = ApprovalPolicy(
        mode="default", ask=lambda prompt: "y", note=notes.append
    )
    assert policy.check("write_file", {"path": "a.txt"}) is False
    assert approvals_mod.audit_log_healthy() is False
    assert notes
    # Read-only work and an explicitly permissive mode keep their behaviour.
    assert policy.check("read_file", {"path": "a.txt"}) is True
    yolo = ApprovalPolicy(mode="yolo", ask=lambda prompt: "n")
    assert yolo.check("write_file", {"path": "a.txt"}) is True


# ---- Medium-7 -------------------------------------------------------------


def test_plan_mode_uses_read_only_allowlist(monkeypatch) -> None:
    monkeypatch.setattr(
        approvals_mod, "EXTERNAL_TOOLS", set(approvals_mod.EXTERNAL_TOOLS | {"custom_reader"})
    )
    notes: list[str] = []
    policy = ApprovalPolicy(mode="plan", ask=lambda prompt: "y", note=notes.append)

    assert policy.check("read_file", {"path": "a.txt"}) is True
    assert policy.check("web_fetch", {"url": "https://example.invalid"}) is True
    assert policy.check("unknown_tool", {}) is False
    assert policy.check("custom_reader", {}) is False
    assert policy.check("write_file", {"path": "a.txt"}) is False


# ---- Medium-8 -------------------------------------------------------------


class _CleanupFailSandbox:
    def __init__(self) -> None:
        self.abort = None

    def setup(self, task) -> None:
        return None

    def cleanup(self) -> None:
        raise RuntimeError("container left behind")


def test_cleanup_failure_is_visible_in_result_and_events(tmp_path) -> None:
    seen: list[str] = []
    bus = EventBus()
    bus.subscribe(lambda name, payload: seen.append(name))
    loop = AgentLoop(
        llm=ScriptedLLMClient([final_response("done")]),
        sandbox=_CleanupFailSandbox(),
        tools=[],
        evaluator=NullEvaluator(),
        logger=JsonlLogger(str(tmp_path / "run.jsonl")),
        events=bus,
    )
    result = loop.run({"task_id": "t", "problem_statement": "cleanup fails"})
    assert result.cleanup_ok is False
    assert result.metrics["cleanup_errors"] == 1
    assert "sandbox_cleanup_error" in seen
    assert _result_payload(result)["cleanup_ok"] is False


# ---- Low-9 ----------------------------------------------------------------


def test_mask_reveals_no_key_characters() -> None:
    assert keys_mod.mask(None) == "(none)"
    key = "sk-abcdefghijklmnopqrst"
    masked = keys_mod.mask(key)
    assert "sk-a" not in masked
    assert "qrst" not in masked
    assert key not in masked
    assert masked == keys_mod.mask(key)  # stable, non-reversible fingerprint


# ---- Low-10 ---------------------------------------------------------------


def test_autosave_failures_rewarn_and_expose_health(monkeypatch, capsys, tmp_path) -> None:
    monkeypatch.setenv("MANTRA_SESSIONS", str(tmp_path / "sessions"))
    monkeypatch.setattr(sessions_mod, "_save_failures", 0)
    monkeypatch.setattr(sessions_mod, "_last_save_ok", True)
    monkeypatch.setattr(sessions_mod, "_last_save_warning", 0.0)
    real_acquire = sessions_mod.acquire_lock
    monkeypatch.setattr(sessions_mod, "acquire_lock", lambda *a, **k: None)

    assert sessions_mod.save("s", {"messages": []}) is None
    assert "autosave failed" in capsys.readouterr().err
    assert sessions_mod.save_health() == {"ok": False, "consecutive_failures": 1}

    # A second immediate failure must not re-announce the same condition.
    assert sessions_mod.save("s", {"messages": []}) is None
    assert "autosave failed" not in capsys.readouterr().err

    # After the re-warn interval it becomes visible again.
    monkeypatch.setattr(sessions_mod, "_last_save_warning", 0.0)
    sessions_mod.save("s", {"messages": []})
    assert "autosave failed" in capsys.readouterr().err

    monkeypatch.setattr(sessions_mod, "acquire_lock", real_acquire)
    assert sessions_mod.save("s", {"messages": []}) is not None
    assert sessions_mod.save_health()["ok"] is True


# ---- Low-22 ---------------------------------------------------------------


def test_memory_rewrite_holds_the_in_process_lock(monkeypatch, tmp_path) -> None:
    real_acquire = knowledge_mod.acquire_lock
    held: list[bool] = []

    def spy(*args, **kwargs):
        held.append(knowledge_mod._memory_lock.locked())
        return real_acquire(*args, **kwargs)

    monkeypatch.setattr(knowledge_mod, "acquire_lock", spy)
    path = tmp_path / "MEMORY.md"
    assert knowledge_mod.rewrite_memory(str(path), "- old entry") is True
    assert held and all(held)


# ---- Low-23 ---------------------------------------------------------------


def test_lock_release_failures_do_not_mask_outcomes(monkeypatch, tmp_path) -> None:
    def release_then_raise(module):
        real = module.release_lock

        def release(*args, **kwargs) -> None:
            real(*args, **kwargs)
            raise OSError("release failed")

        return release

    monkeypatch.setattr(knowledge_mod, "release_lock", release_then_raise(knowledge_mod))
    monkeypatch.setattr(settings_mod, "release_lock", release_then_raise(settings_mod))
    monkeypatch.setattr(sessions_mod, "release_lock", release_then_raise(sessions_mod))

    mem = tmp_path / "MEMORY.md"
    assert knowledge_mod.append_memory(str(mem), "- entry") is True
    assert knowledge_mod.rewrite_memory(str(mem), "- old entry") is True

    monkeypatch.setenv("MANTRA_SETTINGS", str(tmp_path / "settings.json"))
    assert settings_mod._write({}) is True

    monkeypatch.setenv("MANTRA_SESSIONS", str(tmp_path / "sessions"))
    assert sessions_mod.save("s", {"messages": []}) is not None


# ---- Low-24 ---------------------------------------------------------------


def test_icacls_failure_is_not_silently_ignored(monkeypatch) -> None:
    import subprocess

    class _Result:
        returncode = 1

    calls: list[list[str]] = []

    def fake_run(args, **kwargs):
        calls.append(list(args))
        return _Result()

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(permissions_mod, "_ACL_WARNED", False)
    with pytest.warns(UserWarning, match="icacls"):
        assert permissions_mod.icacls_owner_only("C:/workspace/secret") is False
    assert calls and calls[0][0] == "icacls"


# ---- Low-25 ---------------------------------------------------------------


def test_event_bus_guards_subscription_and_counts_failures() -> None:
    bus = EventBus()
    seen: list[str] = []

    def broken(name, payload) -> None:
        raise ValueError("observer failed")

    bus.subscribe(broken)
    bus.subscribe(lambda name, payload: seen.append(name))
    bus.emit("first", {})
    assert bus.failures == 1
    assert seen == ["first"]
    bus.unsubscribe(broken)

    errors: list[BaseException] = []

    def churn() -> None:
        try:
            for _ in range(200):
                handler = lambda name, payload: None
                bus.subscribe(handler)
                bus.unsubscribe(handler)
                bus.emit("tick", {})
        except BaseException as exc:  # pragma: no cover - failure signal
            errors.append(exc)

    threads = [threading.Thread(target=churn) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors
    assert seen.count("tick") == 800


# ---- Low-27 ---------------------------------------------------------------


def test_skipped_settings_save_is_reported(monkeypatch, capsys, tmp_path) -> None:
    monkeypatch.setattr(settings_mod, "_LOCK_WARNED", False)
    monkeypatch.setattr(settings_mod, "acquire_lock", lambda *a, **k: None)
    monkeypatch.setenv("MANTRA_SETTINGS", str(tmp_path / "settings.json"))
    assert settings_mod._write({}) is False
    assert "write lock unavailable" in capsys.readouterr().err


def test_degraded_credential_lock_is_reported(monkeypatch, capsys, tmp_path) -> None:
    monkeypatch.setattr(keys_mod, "_LOCK_DEGRADED_WARNED", False)
    monkeypatch.setenv("MANTRA_CREDENTIALS", str(tmp_path / "credentials.json"))
    real_open = builtins.open

    def fake_open(file, *args, **kwargs):
        if str(file).endswith(".lock"):
            raise OSError("lock unavailable")
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", fake_open)
    keys_mod.store("K", "value")
    assert "store lock unavailable" in capsys.readouterr().err
