"""Headless runner: task validation, exit codes, approval policy."""

from __future__ import annotations

import json


from core.main import _HeadlessApprover, _resolve_path, main
from core.scripted import final_response


def _write_config(tmp_path) -> str:
    config = {
        "llm": {"provider": "scripted"},
        "evaluator": {"type": "none"},
        "logging": {"type": "jsonl", "path": str(tmp_path / "run.jsonl")},
    }
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    return str(path)


def _write_task(tmp_path, statement="do it", extra=None) -> str:
    task = {"task_id": "t", "problem_statement": statement}
    task.update(extra or {})
    path = tmp_path / "task.json"
    path.write_text(json.dumps(task), encoding="utf-8")
    return str(path)


def test_main_passes_on_final_answer(tmp_path, capsys) -> None:
    # Scripted client returns a final answer; the null evaluator passes.
    import core.main as main_mod

    def _fake(config):
        from core.scripted import ScriptedLLMClient

        return ScriptedLLMClient([final_response("all done")])

    original = main_mod.build_llm
    main_mod.build_llm = _fake
    try:
        code = main_mod.main(["--config", _write_config(tmp_path), "--task", _write_task(tmp_path)])
    finally:
        main_mod.build_llm = original
    assert code == 0
    out = capsys.readouterr().out
    assert "PASS" in out


def test_main_rejects_missing_problem_statement(tmp_path, capsys) -> None:
    config = _write_config(tmp_path)
    task_path = tmp_path / "task.json"
    task_path.write_text(json.dumps({"task_id": "t"}), encoding="utf-8")
    code = main(["--config", config, "--task", str(task_path)])
    assert code == 2
    assert "problem_statement" in capsys.readouterr().err


def test_main_rejects_non_object_task(tmp_path, capsys) -> None:
    config = _write_config(tmp_path)
    task_path = tmp_path / "task.json"
    task_path.write_text("[1]", encoding="utf-8")
    code = main(["--config", config, "--task", str(task_path)])
    assert code == 2


def test_main_rejects_broken_task_json(tmp_path, capsys) -> None:
    config = _write_config(tmp_path)
    task_path = tmp_path / "task.json"
    task_path.write_text("{", encoding="utf-8")
    code = main(["--config", config, "--task", str(task_path)])
    assert code == 2


def test_main_rejects_bad_config(tmp_path, capsys) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"bogus_key": 1}), encoding="utf-8")
    code = main(["--config", str(config_path), "--task", _write_task(tmp_path)])
    assert code == 2


def test_resolve_path_prefers_existing_cwd(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "local.txt").write_text("x", encoding="utf-8")
    # An existing cwd-relative path is returned as-is.
    assert _resolve_path("local.txt") == "local.txt"
    # A missing path is returned unchanged so the error path reports it.
    assert _resolve_path("absent.txt") == "absent.txt"


def test_headless_approver_maps_modes() -> None:
    # plan refuses every mutation.
    approver = _HeadlessApprover("plan")
    assert approver.check("write_file", {"path": "a"}) is False
    assert approver.check("read_file", {"path": "a"}) is True
    # default/auto allow ordinary mutations, refuse destructive ones.
    for mode in ("default", "auto"):
        approver = _HeadlessApprover(mode)
        assert approver.check("write_file", {"path": "a"}) is True
        assert approver.check("run_command", {"command": "rm -rf /"}) is False
        assert approver.check("run_command", {"command": "ls"}) is True
    # yolo allows everything.
    approver = _HeadlessApprover("yolo")
    assert approver.check("run_command", {"command": "rm -rf /"}) is True


def test_main_anchors_relative_log_path(tmp_path, monkeypatch) -> None:
    import os

    import core.main as main_mod

    config = {
        "llm": {"provider": "scripted"},
        "evaluator": {"type": "none"},
        "logging": {"type": "jsonl", "path": "logs/relative-run.jsonl"},
    }
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")

    original = main_mod.build_llm

    def _fake(config_dict):
        from core.scripted import ScriptedLLMClient

        return ScriptedLLMClient([final_response("ok")])

    main_mod.build_llm = _fake
    try:
        code = main_mod.main(["--config", str(config_path), "--task", _write_task(tmp_path)])
    finally:
        main_mod.build_llm = original
    assert code == 0
    # The relative log path is anchored to the project root.
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(main_mod.__file__)))
    assert (tmp_path / "run.jsonl").exists() or os.path.isfile(
        os.path.join(project_root, "logs", "relative-run.jsonl")
    )
