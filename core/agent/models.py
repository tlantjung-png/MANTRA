"""Model discovery: fetch catalogue from endpoint."""

from __future__ import annotations

import http.client
import json
import re
import urllib.error
import urllib.request
from typing import Any

from core.agent.exceptions import LLMError
from core.agent.keys import resolve as resolve_key, warn_insecure_transport

DEFAULT_TIMEOUT = 20.0

# Same response cap the chat client applies: a hostile endpoint must not
# be able to exhaust memory through the catalogue endpoint.
_MAX_RESPONSE_BYTES = 5_000_000

# Substrings that mark a model as one that thinks before it answers.
# This is a hint used to offer an effort choice, not a gate: a wrong
# guess costs the user one extra prompt, nothing more.
_REASONING_HINTS = (
    "o1", "o3", "o4", "gpt-5", "reasoning", "thinking", "-think",
    "deepseek-r1", "r1-", "qwq", "magistral", "kimi-k2-thinking",
)
_REASONING_RE = re.compile("|".join(re.escape(h) for h in _REASONING_HINTS), re.IGNORECASE)

# Entries chat completions cannot use are dropped, not shown: an
# embedding or speech model in a chat picker is a wrong answer the
# operator would have to skip. Only legacy completion families are
# filtered; ordinary chat models stay in the catalogue.
_NOISE_RE = re.compile(
    r"("
    r"embedding|whisper|transcri\w*|speech|audio|realtime|"
    r"tts|dall-e|image|sora|video|moderation|"
    r"rerank|re-rank|similarity|babbage|davinci|"
    r"turbo-instruct|instruct-?\d+\.?\d*$|"
    r"computer-use|codex-mini"
    r")",
    re.IGNORECASE,
)

# Dated snapshots: -YYYY-MM-DD or -YYYYMMDD. Rank last.
_SNAPSHOT_RE = re.compile(r"-(20\d{2}-\d{2}-\d{2})$|-(20\d{6})$")


def is_reasoning_model(model_id: str) -> bool:
    """Whether to offer a thinking-effort choice for this model."""
    return bool(model_id and _REASONING_RE.search(model_id))


def _looks_like_a_model(model_id: str) -> bool:
    return bool(model_id) and not _NOISE_RE.search(model_id)


def _rank(model_id: str) -> tuple[int, str]:
    """Sort key: live names first, dated snapshots last, then A-Z.

    Alphabetical alone puts ``gpt-4o-2024-05-13`` above ``gpt-4o`` and
    scatters a family across the list. Only the snapshot flag is used
    to reorder - families still group together, which is what makes a
    long catalogue scannable.
    """
    return (1 if _SNAPSHOT_RE.search(model_id) else 0, model_id.lower())


def rank_models(model_ids: list[str]) -> list[str]:
    """Order a catalogue for a human to read."""
    return sorted(model_ids, key=_rank)


def fetch_models(
    base_url: str,
    api_key_env: str | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> list[str]:
    """Model ids advertised at ``{base_url}/models``, sorted and de-noised.

    Raises LLMError with a plain-English cause, because the two common
    failures - a wrong key and an endpoint without a catalogue - need
    different advice.
    """
    base = (base_url or "").rstrip("/")
    if not base:
        raise LLMError("no base URL to ask for models")

    api_key = resolve_key(api_key_env)
    if api_key:
        # A key crosses the network in cleartext on plain http; warn once
        # per process so local http servers stay usable.
        warn_insecure_transport(base, True)
    headers = {"Accept": "application/json", "User-Agent": "MANTRA/1.0 (coding harness; +https://github.com/mantra)"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    request = urllib.request.Request(f"{base}/models", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            try:
                raw_bytes = response.read(_MAX_RESPONSE_BYTES + 1)
            except TypeError:
                # Fallback for response objects whose read() takes no size
                # argument: pull bounded chunks so a hostile read(n) cannot
                # defeat the cap. A truly argument-less reader is the last
                # resort; its result is still size-checked below.
                raw_bytes = b""
                try:
                    while len(raw_bytes) <= _MAX_RESPONSE_BYTES:
                        chunk = response.read(64 * 1024)
                        if not chunk:
                            break
                        raw_bytes += chunk
                except TypeError:
                    raw_bytes = response.read()
            if len(raw_bytes) > _MAX_RESPONSE_BYTES:
                raise LLMError("model catalogue response exceeds size cap")
            raw = raw_bytes.decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode(errors="replace")[:200]
        except OSError:
            pass
        if exc.code in (401, 403):
            raise LLMError(
                f"the endpoint refused the key (HTTP {exc.code}). Check the "
                f"key, or re-enter it with: /model key"
            ) from exc
        if exc.code == 404:
            raise LLMError(
                f"{base} has no /models listing (HTTP 404). Type the model "
                "name by hand: /model <name>"
            ) from exc
        raise LLMError(f"could not list models (HTTP {exc.code}): {detail}") from exc
    except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as exc:
        # IncompleteRead (a truncated body) is an HTTPException, not an
        # OSError, so it is named explicitly rather than left to escape as
        # a raw traceback into the /model menu.
        raise LLMError(f"could not reach {base}: {exc}") from exc

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise LLMError(f"{base}/models did not return JSON") from exc

    return _extract_ids(payload)


def _extract_ids(payload: Any) -> list[str]:
    """Pull model ids out of the handful of shapes endpoints return."""
    data = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(data, list):
        return []

    ids: list[str] = []
    for entry in data:
        if isinstance(entry, str):
            candidate = entry
        elif isinstance(entry, dict):
            candidate = entry.get("id") or entry.get("name") or entry.get("model") or ""
        else:
            continue
        candidate = str(candidate).strip()
        if candidate and candidate not in ids and _looks_like_a_model(candidate):
            ids.append(candidate)
    return rank_models(ids)
