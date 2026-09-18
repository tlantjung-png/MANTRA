"""Approval classification: risk ladder, rules, fail-closed behavior."""

from __future__ import annotations

import pytest

from core.agent.approvals import (
    MUTATING_TOOLS,
    ApprovalPolicy,
    classify,
    classify_command,
)


def test_readonly_tools_are_safe() -> None:
    assert classify("read_file", {"path": "x"})[0] == "safe"
    assert classify("list_dir", {"path": "."})[0] == "safe"
    assert classify("web_fetch", {"url": "https://x"})[0] == "safe"


def test_write_and_edit_are_mutating() -> None:
    assert classify("write_file", {"path": "a.txt"})[0] == "mutating"
    assert classify("edit_file", {"path": "a.txt"})[0] == "mutating"


def test_fenced_files_require_confirmation() -> None:
    assert classify("write_file", {"path": "AGENTS.md"})[0] == "confirm"
    assert classify("edit_file", {"path": "sub/MEMORY.md"})[0] == "confirm"


def test_git_reset_and_kill_shell_destructive() -> None:
    assert classify("git_reset", {})[0] == "destructive"
    assert classify("kill_shell", {})[0] == "destructive"


@pytest.mark.parametrize(
    "command,expected",
    [
        ("ls -la", "safe"),
        ("git status", "safe"),
        ("git diff HEAD", "safe"),
        ("python -m pytest -q", "safe"),
        ("echo hello", "safe"),
        ("echo hello > out.txt", "mutating"),
        ("touch a.txt", "mutating"),
        ("rm file.txt", "destructive"),
        ("rm -rf /", "destructive"),
        ("del file", "destructive"),
        ("rmdir x", "destructive"),
        ("git reset --hard", "destructive"),
        ("git push --force", "destructive"),
        ("python -c 'print(1)'", "confirm"),
        ("bash -c 'echo hi'", "confirm"),
        ("cmd /c echo hi", "confirm"),
        ("echo 'rm -rf /'", "safe"),  # quoted data, echo is the sole segment
        ("echo 'rm' | bash", "destructive"),  # pipe makes it executable
    ],
)
def test_command_risk_ladder(command: str, expected: str) -> None:
    assert classify_command(command) == expected


def test_unknown_mutating_tool_fails_closed(monkeypatch) -> None:
    # A tool listed as mutating but without a dedicated classify branch
    # must confirm, never ride through.
    import core.agent.approvals as approvals_mod

    monkeypatch.setattr(
        approvals_mod, "MUTATING_TOOLS", frozenset(MUTATING_TOOLS | {"future_tool"})
    )
    risk, detail = approvals_mod.classify("future_tool", {})
    assert risk == "confirm"


def test_empty_command_is_safe() -> None:
    assert classify_command("") == "safe"
    assert classify_command("   ") == "safe"


def test_classification_never_crashes_fuzz() -> None:
    # Arbitrary junk must classify without raising; the ladder only
    # returns known verdicts.
    junk = ["\x00\x01", "'", '"', "\\", "&&&", "|||", "((( ", "$(x)", "`x`", "a" * 500]
    for text in junk:
        assert classify_command(text) in ("safe", "mutating", "confirm", "destructive")


def test_policy_default_mode_asks_for_mutating() -> None:
    answers = iter(["y"])
    policy = ApprovalPolicy(mode="default", ask=lambda prompt: next(answers))
    assert policy.check("write_file", {"path": "a.txt"}) is True
    answers = iter(["n"])
    policy = ApprovalPolicy(mode="default", ask=lambda prompt: next(answers))
    assert policy.check("write_file", {"path": "a.txt"}) is False


def test_policy_auto_allows_mutating_but_not_destructive() -> None:
    policy = ApprovalPolicy(mode="auto", ask=lambda prompt: "n")
    assert policy.check("write_file", {"path": "a.txt"}) is True
    assert policy.check("run_command", {"command": "rm file.txt"}) is False


def test_policy_plan_refuses_all_mutations() -> None:
    notes: list[str] = []
    policy = ApprovalPolicy(mode="plan", note=notes.append, ask=lambda prompt: "y")
    assert policy.check("write_file", {"path": "a.txt"}) is False
    assert notes and "plan mode" in notes[0]


def test_policy_yolo_allows_everything() -> None:
    policy = ApprovalPolicy(mode="yolo", ask=lambda prompt: "n")
    assert policy.check("write_file", {"path": "a.txt"}) is True
    assert policy.check("run_command", {"command": "rm -rf /"}) is True


def test_session_allow_and_reset() -> None:
    policy = ApprovalPolicy(mode="default", ask=lambda prompt: "a")
    assert policy.check("write_file", {"path": "a.txt"}) is True
    # Session-allowed now; a fresh policy with "n" still refuses.
    assert policy.check("write_file", {"path": "a.txt"}) is True
    policy.reset_session()
    policy2 = ApprovalPolicy(mode="default", ask=lambda prompt: "n")
    assert policy2.check("write_file", {"path": "a.txt"}) is False


def test_safe_commands_never_prompt() -> None:
    asked: list[str] = []
    policy = ApprovalPolicy(mode="default", ask=lambda p: asked.append(p) or "n")
    assert policy.check("run_command", {"command": "git status"}) is True
    assert asked == []


def test_mutating_tools_constant() -> None:
    assert "write_file" in MUTATING_TOOLS
    assert "run_command" in MUTATING_TOOLS
    assert "read_file" not in MUTATING_TOOLS
