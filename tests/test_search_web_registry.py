"""Search tools, web-fetch safety helpers, and the component registry."""

from __future__ import annotations

import os

import pytest

from core.agent.exceptions import ConfigError
from core.registry import build_evaluator, build_llm, build_sandbox, build_tools
from core.sandbox import LocalSandbox
from core.tools.search import FindFileTool, SearchCodeTool
from core.tools.web import _is_private_hostname, _safe_url


@pytest.fixture()
def sandbox(tmp_path) -> LocalSandbox:
    box = LocalSandbox(workspace_root=str(tmp_path))
    box.setup({})
    yield box
    box.cleanup()


def _write(root: str, rel: str, text: str) -> None:
    full = os.path.join(root, rel)
    os.makedirs(os.path.dirname(full) or root, exist_ok=True)
    with open(full, "w", encoding="utf-8") as fh:
        fh.write(text)


def test_search_code_finds_literal(tmp_path, sandbox) -> None:
    _write(str(tmp_path), "src/a.py", "alpha beta\n")
    _write(str(tmp_path), "src/b.py", "gamma\n")
    tool = SearchCodeTool()
    out = tool.execute(sandbox, query="beta")
    assert "beta" in out and "a.py" in out
    assert "b.py" not in out


def test_search_code_reports_no_match(tmp_path, sandbox) -> None:
    _write(str(tmp_path), "a.py", "hello\n")
    out = SearchCodeTool().execute(sandbox, query="absent-token")
    assert "no match" in out.lower()


def test_find_file_by_substring(tmp_path, sandbox) -> None:
    _write(str(tmp_path), "src/parse_file.py", "x")
    out = FindFileTool().execute(sandbox, pattern="parse")
    assert "parse_file.py" in out


def test_search_skips_ignored_dirs(tmp_path, sandbox) -> None:
    _write(str(tmp_path), ".git/config", "alpha\n")
    _write(str(tmp_path), "src/real.py", "alpha\n")
    out = SearchCodeTool().execute(sandbox, query="alpha")
    assert "config" not in out
    assert "real.py" in out


def test_is_private_hostname_blocks_loopback_and_metadata() -> None:
    assert _is_private_hostname("localhost") is True
    assert _is_private_hostname("127.0.0.1") is True
    assert _is_private_hostname("169.254.169.254") is True
    assert _is_private_hostname("metadata.google.internal") is True
    assert _is_private_hostname("10.1.2.3") is True
    assert _is_private_hostname("192.168.0.5") is True


def test_is_private_hostname_allows_public() -> None:
    assert _is_private_hostname("example.com") is False
    assert _is_private_hostname("8.8.8.8") is False


def test_safe_url_strips_userinfo_and_redacts_query() -> None:
    url = _safe_url("https://user:secret@example.com/path?token=abc&x=1")
    assert "user" not in url
    assert "secret" not in url
    assert "token=[REDACTED]" in url
    assert "x=1" in url


def test_registry_builds_known_components() -> None:
    llm = build_llm({"provider": "scripted", "script": []})
    assert llm is not None
    sandbox = build_sandbox({"provider": "local"})
    assert isinstance(sandbox, LocalSandbox)
    evaluator = build_evaluator({"type": "none"})
    assert evaluator is not None
    tools = build_tools(["read_file", "write_file", "webfetch", "web_fetch"])
    names = [t.name for t in tools]
    # Aliases dedupe to one tool.
    assert names.count("web_fetch") == 1


def test_registry_rejects_unknown_names() -> None:
    with pytest.raises(ConfigError, match="unknown llm provider"):
        build_llm({"provider": "bogus"})
    with pytest.raises(ConfigError, match="unknown sandbox"):
        build_sandbox({"provider": "bogus"})
    with pytest.raises(ConfigError, match="unknown evaluator"):
        build_evaluator({"type": "bogus"})
    with pytest.raises(ConfigError, match="unknown tool"):
        build_tools(["no_such_tool"])


def test_registry_rejects_unknown_constructor_keys() -> None:
    with pytest.raises(ConfigError, match="unknown config keys"):
        build_sandbox({"provider": "local", "bogus_key": 1})


def test_registry_shares_one_edit_ledger() -> None:
    tools = build_tools(["read_file", "edit_file", "write_file"])
    ledgers = {id(t.ledger) for t in tools if hasattr(t, "ledger")}
    assert len(ledgers) == 1


def test_tool_schema_shape() -> None:
    tools = build_tools(["read_file"])
    schema = tools[0].schema()
    assert schema["type"] == "function"
    assert schema["function"]["name"] == "read_file"
    # A fresh copy per call: callers may mutate their schema safely.
    schema["function"]["parameters"]["properties"]["path"]["type"] = "number"
    assert tools[0].schema()["function"]["parameters"]["properties"]["path"]["type"] == "string"
