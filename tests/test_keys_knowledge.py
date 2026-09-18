"""Credential store and knowledge assembly: keys, memory, prompt caps."""

from __future__ import annotations

import json

import pytest

from core.agent import keys
from core.agent.knowledge import (
    MEMORY_CAP_CHARS,
    active_entries,
    append_memory,
    assemble_system_prompt,
    parse_entries,
    plan_memory_write,
)
from core.agent.repairs import canonical_command, repair_arguments, validate_arguments


@pytest.fixture(autouse=True)
def _isolated_store(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("MANTRA_CREDENTIALS", str(tmp_path / "credentials.json"))


def test_store_and_resolve_roundtrip(monkeypatch) -> None:
    keys.store("MY_KEY", "abc123")
    # Environment wins over the store.
    assert keys.resolve("MY_KEY") == "abc123"
    monkeypatch.delenv("MY_KEY", raising=False)
    assert keys.resolve("MY_KEY") == "abc123"


def test_store_refuses_empty() -> None:
    with pytest.raises(ValueError):
        keys.store("", "x")
    with pytest.raises(ValueError):
        keys.store("K", "  ")


def test_remove_reports_presence() -> None:
    keys.store("K1", "v1")
    assert keys.remove("K1") is True
    assert keys.remove("K1") is False
    assert keys.resolve("K1") is None


def test_env_overrides_store(monkeypatch) -> None:
    keys.store("K2", "stored")
    monkeypatch.setenv("K2", "environ")
    assert keys.resolve("K2") == "environ"
    monkeypatch.delenv("K2")
    assert keys.resolve("K2") == "stored"


def test_mask_never_reveals_full_key() -> None:
    assert keys.mask(None) == "(none)"
    assert keys.mask("short") == "****"
    masked = keys.mask("sk-abcdefghijklmnopqrst")
    assert "sk-a" in masked and "qrst" in masked
    assert "sk-abcdefghijklmnopqrst" not in masked


def test_corrupt_store_quarantined(monkeypatch, tmp_path) -> None:
    path = tmp_path / "credentials.json"
    path.write_text("{broken", encoding="utf-8")
    monkeypatch.setenv("MANTRA_CREDENTIALS", str(path))
    assert keys.stored_keys() == {}
    backups = list(tmp_path.glob("credentials.json.corrupt-*"))
    assert backups, "corrupt store must be copied aside"


def test_append_memory_caps_size(tmp_path) -> None:
    mem = tmp_path / "MEMORY.md"
    for i in range(50):
        append_memory(str(mem), f"- 2026-01-01 00:00 | task-{i} | done: entry {i} " + "y" * 100, cap=MEMORY_CAP_CHARS)
    body = mem.read_text(encoding="utf-8")
    assert len(body) <= MEMORY_CAP_CHARS
    # Newest entries are the tail.
    assert "task-49" in body


def test_plan_memory_write_dedupes_and_supersedes(tmp_path) -> None:
    existing = "- 2026-01-01 00:00 | task-alpha | done: fixed the parser module bug | status=active\n"
    # Near-duplicate of the newest entry: skipped.
    action, _ = plan_memory_write(existing, "- 2026-01-02 00:00 | task-alpha2 | done: fixed the parser module bug")
    assert action == "skip"

    # Same topic, new content: the old entry is superseded.
    action, updated = plan_memory_write(existing, "- 2026-01-02 00:00 | task-beta | done: rewrote the parser module entirely")
    assert action == "supersede"
    assert "status=superseded" in updated

    # Unrelated topic: appended.
    action, _ = plan_memory_write(existing, "- 2026-01-02 00:00 | task-gamma | done: unrelated database migration work")
    assert action == "append"


def test_parse_entries_reads_status() -> None:
    entries = parse_entries(
        "- d | t | done: a | status=active\n"
        "- d | t2 | done: b | status=superseded\n"
    )
    assert entries[0]["status"] == "active"
    assert entries[1]["status"] == "superseded"
    assert active_entries(
        "- d | t | done: a | status=active\n- d | t2 | done: b | status=superseded\n"
    )[0]["line"].startswith("- d | t ")


def test_assemble_system_prompt_sections_and_cap() -> None:
    prompt = assemble_system_prompt(
        "BASE",
        known_failures_path=None,
        memory_path=None,
        instructions_path=None,
        environment="- os: test",
    )
    assert prompt.startswith("BASE")
    assert "Environment" in prompt


def test_assemble_system_prompt_caps_total(tmp_path) -> None:
    big = tmp_path / "kf.md"
    big.write_text("k" * 30_000, encoding="utf-8")
    prompt = assemble_system_prompt("BASE", known_failures_path=str(big))
    # The section read itself is capped to MEMORY_CAP_CHARS, so the total
    # stays far below the 20k prompt ceiling.
    assert len(prompt) < 20_000
    assert prompt.startswith("BASE")


def test_canonical_command_aliases() -> None:
    assert canonical_command({"command": "ls"}) == "ls"
    assert canonical_command({"cmd": "ls -la"}) == "ls -la"
    assert canonical_command({}) == ""


def test_repair_arguments_alias_and_numeric() -> None:
    schema = {
        "type": "object",
        "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}},
        "required": ["path"],
    }
    repaired, notes = repair_arguments("read_file", {"file_path": "a.txt", "limit": "20"}, schema)
    assert repaired["path"] == "a.txt"
    assert repaired["limit"] == 20
    assert any("aliased" in n for n in notes)


def test_validate_arguments_reports_issues() -> None:
    schema = {
        "type": "object",
        "properties": {"limit": {"type": "integer"}},
        "required": ["path", "limit"],
    }
    issues = validate_arguments({"limit": "x"}, schema)
    assert any("missing required field 'path'" in i for i in issues)
    assert any("expected integer" in i for i in issues)


def test_validate_arguments_accepts_valid() -> None:
    schema = {"type": "object", "properties": {"limit": {"type": "integer"}}, "required": ["limit"]}
    assert validate_arguments({"limit": 5}, schema) == []
