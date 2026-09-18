"""File tools: read windows, write/edit safety, ledger, blocked paths."""

from __future__ import annotations

import pytest

from core.sandbox import LocalSandbox
from core.tools.files import (
    EditFileTool,
    ListDirTool,
    ReadFileTool,
    WriteFileTool,
    _is_blocked_path_harness,
)
from core.tools.ledger import EditLedger


@pytest.fixture()
def sandbox(tmp_path) -> LocalSandbox:
    box = LocalSandbox(workspace_root=str(tmp_path))
    box.setup({})
    yield box
    box.cleanup()


@pytest.fixture()
def ledger() -> EditLedger:
    return EditLedger()


@pytest.fixture()
def tools(sandbox, ledger):
    read = ReadFileTool()
    write = WriteFileTool()
    edit = EditFileTool()
    for tool in (read, write, edit):
        tool.ledger = ledger
    return {"read": read, "write": write, "edit": edit}


def test_read_single_file(sandbox, tools) -> None:
    (sandbox.root_path() if hasattr(sandbox, "root_path") else None)
    import os

    with open(os.path.join(sandbox.root, "a.txt"), "w", encoding="utf-8") as fh:
        fh.write("l1\nl2\nl3")
    out = tools["read"].execute(sandbox, path="a.txt")
    assert "l1" in out and "l2" in out


def test_read_window_and_resume(sandbox, tools) -> None:
    import os

    with open(os.path.join(sandbox.root, "a.txt"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(f"line {i}" for i in range(50)))
    out = tools["read"].execute(sandbox, path="a.txt", offset=0, limit=10)
    assert "line 0" in out and "line 9" in out
    assert "line 10" not in out
    assert "more lines remain" in out


def test_read_offset_past_eof(sandbox, tools) -> None:
    import os

    with open(os.path.join(sandbox.root, "a.txt"), "w", encoding="utf-8") as fh:
        fh.write("one\ntwo")
    out = tools["read"].execute(sandbox, path="a.txt", offset=99)
    assert "beyond end of" in out


def test_read_empty_file(sandbox, tools) -> None:
    import os

    open(os.path.join(sandbox.root, "empty.txt"), "w").close()
    out = tools["read"].execute(sandbox, path="empty.txt")
    assert "is empty" in out


def test_read_invalid_path(sandbox, tools) -> None:
    assert tools["read"].execute(sandbox, path="../x").startswith("ERROR") or "Note" in tools["read"].execute(sandbox, path="../x")


def test_read_rejects_bad_offset_type(sandbox, tools) -> None:
    out = tools["read"].execute(sandbox, path="a.txt", offset="2abc")
    assert out.startswith("ERROR")
    out = tools["read"].execute(sandbox, path="a.txt", offset=1.5)
    assert out.startswith("ERROR")


def test_read_blocked_harness_paths(sandbox, tools) -> None:
    out = tools["read"].execute(sandbox, path="con")
    assert "blocked" in out or "device" in out
    out = tools["read"].execute(sandbox, path="file.txt.")
    assert "dot" in out


def test_is_blocked_path_harness_shapes() -> None:
    assert _is_blocked_path_harness("") == "path is empty"
    assert _is_blocked_path_harness("a/b.") is not None
    assert _is_blocked_path_harness("a/b ") is not None
    assert _is_blocked_path_harness("f.txt:ads") is not None
    assert _is_blocked_path_harness("COM1") is not None
    assert _is_blocked_path_harness("\\\\?\\C:\\x") is not None
    assert _is_blocked_path_harness("normal/file.txt") is None


def test_write_creates_and_reports(sandbox, tools) -> None:
    out = tools["write"].execute(sandbox, path="new.txt", content="data")
    assert out.startswith("OK")
    import os

    with open(os.path.join(sandbox.root, "new.txt"), encoding="utf-8") as fh:
        assert fh.read() == "data"


def test_write_rejects_escape(sandbox, tools) -> None:
    out = tools["write"].execute(sandbox, path="../evil.txt", content="x")
    assert out.startswith("ERROR")


def test_write_rejects_too_large(sandbox, tools) -> None:
    out = tools["write"].execute(sandbox, path="big.txt", content="x" * 1_000_001)
    assert out.startswith("ERROR")


def test_edit_requires_prior_read(sandbox, tools) -> None:
    import os

    with open(os.path.join(sandbox.root, "a.txt"), "w", encoding="utf-8") as fh:
        fh.write("hello world")
    out = tools["edit"].execute(sandbox, path="a.txt", old_string="world", new_string="there")
    assert "read" in out.lower()


def test_edit_happy_path(sandbox, tools) -> None:
    import os

    path = os.path.join(sandbox.root, "a.txt")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("hello world")
    tools["read"].execute(sandbox, path="a.txt")
    out = tools["edit"].execute(sandbox, path="a.txt", old_string="world", new_string="there")
    assert out.startswith("OK")
    with open(path, encoding="utf-8") as fh:
        assert fh.read() == "hello there"


def test_edit_rejects_stale_read(sandbox, tools) -> None:
    import os

    path = os.path.join(sandbox.root, "a.txt")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("hello world")
    tools["read"].execute(sandbox, path="a.txt")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("changed on disk")
    out = tools["edit"].execute(sandbox, path="a.txt", old_string="hello", new_string="bye")
    assert "changed on disk" in out


def test_edit_rejects_ambiguous_needle(sandbox, tools) -> None:
    import os

    path = os.path.join(sandbox.root, "a.txt")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("same same")
    tools["read"].execute(sandbox, path="a.txt")
    out = tools["edit"].execute(sandbox, path="a.txt", old_string="same", new_string="x")
    assert "ambiguous" in out


def test_edit_rejects_missing_needle(sandbox, tools) -> None:
    import os

    path = os.path.join(sandbox.root, "a.txt")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("hello")
    tools["read"].execute(sandbox, path="a.txt")
    out = tools["edit"].execute(sandbox, path="a.txt", old_string="absent", new_string="x")
    assert "not found" in out


def test_list_dir_root_and_escape(sandbox, tools) -> None:
    import os

    os.mkdir(os.path.join(sandbox.root, "sub"))
    with open(os.path.join(sandbox.root, "f.txt"), "w", encoding="utf-8") as fh:
        fh.write("x")
    out = tools["list"].execute(sandbox, path=".") if "list" in tools else ListDirTool().execute(sandbox, path=".")
    assert "sub/" in out and "f.txt" in out
    out = ListDirTool().execute(sandbox, path="../")
    assert out.startswith("ERROR")
