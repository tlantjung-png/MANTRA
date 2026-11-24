"""Offline tests for the opt-in web search tool.

No test here touches the network: the shared transport name is always
patched. The configured endpoint is a placeholder on a reserved domain.
"""

from __future__ import annotations

import gzip
import json
from unittest import mock
from urllib.error import HTTPError, URLError

import pytest

from core.agent.exceptions import ConfigError
from core.config import DEFAULTS, merge_defaults
from core.registry import TOOL_REGISTRY, build_tools
from core.tools.web_search import WebSearchTool, configure_web_search

BASE_URL = "https://search.example/v1"


class _Headers:
    def __init__(self, content_type="application/json", encoding="", charset="utf-8"):
        self._type = content_type
        self._encoding = encoding
        self._charset = charset

    def get(self, key, default=""):
        if key.lower() == "content-type":
            return self._type
        if key.lower() == "content-encoding":
            return self._encoding
        return default

    def get_content_charset(self):
        return self._charset


class _Response:
    def __init__(self, payload, url=BASE_URL, encoding="", content_type="application/json"):
        if isinstance(payload, str):
            body = payload.encode("utf-8")
        else:
            body = json.dumps(payload).encode("utf-8")
        if "gzip" in encoding:
            body = gzip.compress(body)
        self._body = body
        self._url = url
        self.status = 200
        self.headers = _Headers(content_type, encoding)

    def read(self, size=-1):
        return self._body if size is None or size < 0 else self._body[:size]

    def geturl(self):
        return self._url

    def getcode(self):
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _results(count=1, url="https://page.example/a"):
    return {
        "results": [
            {
                "url": url,
                "title": f"Result {index}",
                "snippet": f"Text for result {index}",
            }
            for index in range(count)
        ]
    }


@pytest.fixture(autouse=True)
def _reset_settings():
    configure_web_search({})
    yield
    configure_web_search({})


def _run(tool, sandbox, response=None, side_effect=None, **kwargs):
    captured = {}

    def fake(request, timeout=None):
        captured["request"] = request
        captured["timeout"] = timeout
        if side_effect is not None:
            raise side_effect
        return response

    with mock.patch("core.tools.web.urlopen", side_effect=fake):
        out = tool.execute(sandbox, **kwargs)
    return out, captured


def test_schema_registry_lookup_and_default_tool_list(sandbox):
    assert TOOL_REGISTRY["web_search"] is WebSearchTool
    tool = build_tools(["web_search"])[0]
    assert isinstance(tool, WebSearchTool)
    schema = tool.schema()
    params = schema["function"]["parameters"]
    assert params["required"] == ["query"]
    assert params["properties"]["query"]["type"] == "string"
    assert params["properties"]["limit"]["type"] == "integer"
    assert params["properties"]["limit"]["maximum"] >= 1
    assert params["properties"]["max_chars"]["type"] == "integer"
    assert params["properties"]["max_chars"]["minimum"] >= 0
    assert "web_search" not in DEFAULTS["tools"]
    default_names = [t.name for t in build_tools(DEFAULTS["tools"])]
    assert "web_search" not in default_names


def test_config_section_defaults_and_validation():
    section = DEFAULTS["web_search"]
    assert set(section) == {"base_url", "api_key_env", "limit", "timeout"}
    assert section["base_url"] == ""
    merged = merge_defaults({"web_search": {"base_url": BASE_URL, "limit": 3}})
    assert merged["web_search"]["limit"] == 3
    assert merged["web_search"]["api_key_env"] == ""
    with pytest.raises(ConfigError, match="unknown config keys in 'web_search'"):
        merge_defaults({"web_search": {"bogus": 1}})
    with pytest.raises(ConfigError, match="web_search.limit"):
        merge_defaults({"web_search": {"limit": "many"}})
    with pytest.raises(ConfigError, match="web_search.timeout"):
        merge_defaults({"web_search": {"timeout": "soon"}})


def test_direct_configuration_validation():
    with pytest.raises(ConfigError, match="unknown config keys"):
        configure_web_search({"bogus": 1})
    with pytest.raises(ConfigError, match="web_search.limit"):
        configure_web_search({"limit": 0})
    with pytest.raises(ConfigError, match="web_search.limit"):
        configure_web_search({"limit": True})
    with pytest.raises(ConfigError, match="web_search.timeout"):
        configure_web_search({"timeout": 0})
    with pytest.raises(ConfigError, match="web_search.base_url"):
        configure_web_search({"base_url": 7})
    with pytest.raises(ConfigError, match="must be an object"):
        configure_web_search(["not", "a", "section"])


def test_query_encoding_and_credential_header(sandbox, monkeypatch):
    configure_web_search(
        {"base_url": BASE_URL + "?existing=keep", "api_key_env": "TEST_SEARCH_KEY"}
    )
    monkeypatch.setenv("TEST_SEARCH_KEY", "secret-value")
    out, captured = _run(
        WebSearchTool(), sandbox, _Response(_results()), query="hello world & more", limit=1
    )
    assert "search results" in out
    request = captured["request"]
    assert "q=hello+world+%26+more" in request.full_url
    assert "existing=keep" in request.full_url
    assert "limit=1" in request.full_url
    assert request.headers["Authorization"] == "Bearer secret-value"
    assert request.get_header("User-agent")
    assert captured["timeout"] > 0


def test_result_urls_are_safely_displayed(sandbox):
    configure_web_search({"base_url": BASE_URL})
    url = "https://user:pass@page.example/a?token=abc123&x=1"
    out, _ = _run(WebSearchTool(), sandbox, _Response(_results(url=url)), query="terms")
    assert "token=[REDACTED]" in out
    assert "x=1" in out
    assert "pass" not in out
    assert "abc123" not in out
    assert "Result 0" in out and "Text for result 0" in out


def test_private_result_urls_are_rejected(sandbox):
    configure_web_search({"base_url": BASE_URL})
    payload = {
        "results": [
            {"url": "http://127.0.0.1/admin", "title": "internal"},
            {"url": "http://localhost/a", "title": "loopback", "content": "secret text"},
            {"url": "https://page.example/safe", "title": "safe", "snippet": "shown"},
        ]
    }
    out, _ = _run(WebSearchTool(), sandbox, _Response(payload), query="terms", limit=5)
    assert "127.0.0.1" not in out
    assert "localhost" not in out
    assert "secret text" not in out
    assert "skipped 2 unsafe result URL(s)" in out
    assert "safe" in out and "shown" in out


def test_result_and_character_caps(sandbox):
    configure_web_search({"base_url": BASE_URL, "limit": 2})
    out, captured = _run(
        WebSearchTool(), sandbox, _Response(_results(count=6)), query="terms", limit=10
    )
    assert out.count("Text for result") == 2
    assert "limit=2" in captured["request"].full_url
    configure_web_search({"base_url": BASE_URL})
    short, _ = _run(
        WebSearchTool(), sandbox, _Response(_results(count=6)), query="terms", max_chars=200
    )
    assert "[truncated]" in short
    assert len(short) < 300


def test_compressed_payload_is_inflated(sandbox):
    configure_web_search({"base_url": BASE_URL})
    out, _ = _run(
        WebSearchTool(), sandbox, _Response(_results(), encoding="gzip"), query="terms"
    )
    assert "Result 0" in out


def test_no_endpoint_fails_without_network(sandbox):
    configure_web_search({})
    out, captured = _run(
        WebSearchTool(),
        sandbox,
        side_effect=AssertionError("must not fetch"),
        query="terms",
    )
    assert "no web search endpoint" in out
    assert "request" not in captured


def test_private_endpoint_is_rejected(sandbox):
    configure_web_search({"base_url": "http://127.0.0.1/v1"})
    out, captured = _run(
        WebSearchTool(),
        sandbox,
        side_effect=AssertionError("must not fetch"),
        query="terms",
    )
    assert "blocked private" in out
    assert "request" not in captured


@pytest.mark.parametrize(
    "response,side_effect,expected",
    [
        (None, HTTPError(BASE_URL, 503, "Unavailable", {}, None), "HTTP 503"),
        (None, URLError("dns failure"), "dns failure"),
        (None, TimeoutError("too slow"), "too slow"),
        (None, OSError("socket gone"), "socket gone"),
        (_Response("{not json"), None, "invalid JSON"),
        (_Response([]), None, "not an object"),
        (_Response({"other": []}), None, "no results array"),
        (_Response({"results": [None, "x", {}]}), None, "no usable results"),
    ],
)
def test_failures_are_strings_not_exceptions(sandbox, response, side_effect, expected):
    configure_web_search({"base_url": BASE_URL})
    out, _ = _run(WebSearchTool(), sandbox, response=response, side_effect=side_effect, query="q")
    if expected != "no usable results":
        assert out.startswith("search failed:")
    assert expected in out


def test_build_tools_config_wires_and_resets_settings(sandbox):
    tools = build_tools(["web_search"], {"web_search": {"base_url": BASE_URL}})
    out, _ = _run(tools[0], sandbox, _Response(_results()), query="terms")
    assert "Result 0" in out
    build_tools(["read_file"], {})
    out, captured = _run(
        WebSearchTool(),
        sandbox,
        side_effect=AssertionError("must not fetch"),
        query="terms",
    )
    assert "no web search endpoint" in out
    assert "request" not in captured


def test_empty_query_is_refused_without_network(sandbox):
    configure_web_search({"base_url": BASE_URL})
    out, captured = _run(
        WebSearchTool(),
        sandbox,
        side_effect=AssertionError("must not fetch"),
        query="   ",
    )
    assert "no query given" in out
    assert "request" not in captured
