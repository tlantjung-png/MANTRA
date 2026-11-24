"""Opt-in web search: query a configured endpoint and return bounded text.

The endpoint is supplied by the operator; nothing is fetched unless a
``web_search`` section names one. All network access goes through the
validating transport helpers in :mod:`core.tools.web`, so private hosts,
unsafe redirects and oversized responses are refused by the same gate that
protects the other web tools.
"""

from __future__ import annotations

import json
import os
import urllib.parse
from typing import Any, ClassVar
from urllib.error import HTTPError, URLError
from urllib.request import Request

from core.agent.exceptions import ConfigError
from core.tools import web
from core.types import Sandbox, Tool

# Hard ceilings independent of operator settings.
_MAX_RESULTS = 20
_MAX_CHARS = 20_000
_MIN_CHARS = 200

_SETTING_KEYS = frozenset({"base_url", "api_key_env", "limit", "timeout"})

_DEFAULT_SETTINGS: dict[str, Any] = {
    "base_url": "",
    "api_key_env": "",
    "limit": 5,
    "timeout": web._TIMEOUT,
}

# Module-level settings, refreshed by ``configure_web_search`` whenever the
# tool list is built. An empty section disables the tool again.
_settings: dict[str, Any] = dict(_DEFAULT_SETTINGS)


def configure_web_search(section: dict) -> None:
    """Validate and store the endpoint settings for this process.

    An empty (or missing) section resets the tool to its disabled state, so
    a later build without the section cannot reuse stale settings.
    """
    if section is None:
        section = {}
    if not isinstance(section, dict):
        raise ConfigError(f"config web_search must be an object, got {type(section).__name__}")
    unknown = [key for key in section if key not in _SETTING_KEYS]
    if unknown:
        raise ConfigError(
            f"unknown config keys in 'web_search': {sorted(unknown)} "
            f"(known: {sorted(_SETTING_KEYS)})"
        )

    candidate = dict(_DEFAULT_SETTINGS)
    candidate.update(section)
    for key in ("base_url", "api_key_env"):
        if not isinstance(candidate[key], str):
            raise ConfigError(f"config web_search.{key} must be a string")
    limit = candidate["limit"]
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise ConfigError("config web_search.limit must be an integer")
    if not 1 <= limit <= _MAX_RESULTS:
        raise ConfigError(
            f"config web_search.limit must be between 1 and {_MAX_RESULTS}, got {limit!r}"
        )
    timeout = candidate["timeout"]
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
        raise ConfigError("config web_search.timeout must be a positive number")

    _settings.clear()
    _settings.update(candidate)


class WebSearchTool(Tool):
    """Ask the configured web search service and return plain, bounded text.

    Failures are returned as strings: the agent can rephrase the query or
    tell the operator the endpoint is unavailable, instead of ending the
    turn on a traceback.
    """

    name = "web_search"
    description = (
        "Search the web through the configured web search service and return "
        "a short list of matching pages with titles, links and snippets. "
        "The endpoint must be configured by the operator; if it is not, the "
        "tool reports that the search is unavailable. Results are plain "
        "text and are bounded in number and length."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "The search terms to look up",
            },
            "limit": {
                "type": "integer",
                "description": (
                    f"Maximum number of results to show (1-{_MAX_RESULTS}, default 5)"
                ),
                "minimum": 1,
                "maximum": _MAX_RESULTS,
            },
            "max_chars": {
                "type": "integer",
                "description": (
                    "Truncate the returned text after this many characters "
                    f"(default {web._DEFAULT_MAX_CHARS})"
                ),
                "minimum": _MIN_CHARS,
                "maximum": _MAX_CHARS,
            },
        },
        "required": ["query"],
    }

    def execute(
        self,
        sandbox: Sandbox,  # noqa: ARG002 - shared tool signature; no sandbox access needed
        query: str = "",
        limit: int = 5,
        max_chars: int = web._DEFAULT_MAX_CHARS,
    ) -> str:
        try:
            return self._search(query, limit, max_chars)
        except Exception as exc:  # noqa: BLE001 - a tool failure is data, not a crash
            return f"search failed: {type(exc).__name__}: {exc}"

    @staticmethod
    def _search(query: str, limit: int, max_chars: int) -> str:
        base_url = (_settings.get("base_url") or "").strip()
        if not base_url:
            return "search failed: no web search endpoint is configured"
        blocked = web._check_url_allowed(base_url)
        if blocked:
            return blocked.replace("fetch failed", "search failed")

        query_text = query.strip() if isinstance(query, str) else ""
        if not query_text:
            return "search failed: no query given"

        try:
            budget = int(max_chars)
        except (TypeError, ValueError):
            budget = web._DEFAULT_MAX_CHARS
        budget = max(_MIN_CHARS, min(budget, _MAX_CHARS))
        try:
            wanted = int(limit)
        except (TypeError, ValueError):
            wanted = 5
        count = max(1, min(wanted, int(_settings["limit"]), _MAX_RESULTS))

        # Keep any operator-supplied path and query parameters, replacing
        # only the ones this tool owns. urlencode quotes the terms safely.
        parts = urllib.parse.urlsplit(base_url)
        pairs = [
            (key, value)
            for key, value in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
            if key not in ("q", "query", "limit")
        ]
        pairs.append(("q", query_text))
        pairs.append(("limit", str(count)))
        url = parts._replace(query=urllib.parse.urlencode(pairs)).geturl()

        headers = {"User-Agent": web._USER_AGENT}
        key_env = _settings.get("api_key_env") or ""
        secret = os.environ.get(key_env, "") if key_env else ""
        if secret:
            headers["Authorization"] = f"Bearer {secret}"
        request = Request(url, headers=headers)  # noqa: S310 - validating opener, URL checked above

        notes: list[str] = []
        try:
            timeout = float(_settings["timeout"])
            with web.urlopen(request, timeout=timeout) as response:
                if web._header_block_size(response.headers) > web._MAX_HEADER_BYTES:
                    return (
                        f"search failed: response headers too large "
                        f"(cap {web._MAX_HEADER_BYTES} bytes) for {web._safe_url(base_url)}"
                    )
                raw = response.read(web._MAX_BYTES + 1)
                encoding = response.headers.get_content_charset()
                compressed = response.headers.get("Content-Encoding", "")
                content_type = response.headers.get("Content-Type", "")
                final_url = response.geturl() if hasattr(response, "geturl") else url
                status = (
                    getattr(response, "status", None)
                    or getattr(response, "getcode", lambda: None)()
                )
        except HTTPError as exc:
            detail = str(exc.reason) if hasattr(exc, "reason") else str(exc)
            if "blocked" in detail.lower():
                return detail.replace("fetch failed", "search failed")
            return f"search failed: HTTP {exc.code} {exc.reason} for {web._safe_url(base_url)}"
        except URLError as exc:
            reason = str(exc.reason) if hasattr(exc, "reason") else str(exc)
            if "blocked" in reason.lower():
                return reason.replace("fetch failed", "search failed")
            return f"search failed: {reason} for {web._safe_url(base_url)}"
        except (TimeoutError, OSError) as exc:
            return f"search failed: {exc} for {web._safe_url(base_url)}"

        # The validating opener checks each redirect hop; re-check the final
        # URL as defence in depth before trusting any payload.
        blocked_final = web._check_url_allowed(final_url)
        if blocked_final:
            return blocked_final.replace("fetch failed", "search failed")

        if len(raw) > web._MAX_BYTES:
            raw = raw[: web._MAX_BYTES]
            notes.append(f"(response truncated at {web._MAX_BYTES} bytes)")
        raw, inflated_truncated = web._inflate(raw, compressed)
        if inflated_truncated:
            notes.append(f"(decompressed response truncated at {web._MAX_INFLATED} bytes)")
        body = web._decode(raw, encoding if web._looks_textual(content_type) else None, notes)

        try:
            payload = json.loads(body)
        except (ValueError, TypeError):
            return f"search failed: endpoint returned invalid JSON for {web._safe_url(base_url)}"
        if not isinstance(payload, dict):
            return "search failed: endpoint response was not an object"
        raw_results = payload.get("results")
        if not isinstance(raw_results, list):
            return "search failed: endpoint response had no results array"

        blocks: list[str] = []
        malformed = 0
        unsafe = 0
        for item in raw_results:
            if len(blocks) >= count:
                break
            if not isinstance(item, dict):
                malformed += 1
                continue
            url_value = item.get("url")
            if not isinstance(url_value, str) or not url_value.strip():
                malformed += 1
                continue
            result_url = url_value.strip()
            # Every URL is gated before its safe form is shown to the model.
            if web._check_url_allowed(result_url):
                unsafe += 1
                continue
            title = item.get("title")
            title = title.strip() if isinstance(title, str) else ""
            text = item.get("content")
            if not isinstance(text, str):
                text = item.get("snippet")
            text = text.strip() if isinstance(text, str) else ""
            block = f"{len(blocks) + 1}. {title or '(untitled)'}\n   {web._safe_url(result_url)}"
            if text:
                block += f"\n   {text}"
            blocks.append(block)

        header = f"search results from {web._safe_url(base_url)} (HTTP {status})"
        if malformed:
            notes.append(f"skipped {malformed} malformed result(s)")
        if unsafe:
            notes.append(f"skipped {unsafe} unsafe result URL(s)")
        if notes:
            header += " " + " ".join(notes)
        text = header + "\n\n" + ("\n\n".join(blocks) if blocks else "(no usable results)")
        if len(text) > budget:
            text = text[:budget].rstrip() + "\n... [truncated]"
        return text
