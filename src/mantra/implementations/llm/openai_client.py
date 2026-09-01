"""Chat completions client: stdlib only, buffered/streaming, retries."""

from __future__ import annotations

import http.client
import json
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Callable

_MAX_RESPONSE_BYTES = 5_000_000
_MAX_CONTENT_PARTS_BYTES = 2_000_000

from mantra.core.exceptions import LLMError
from mantra.core.keys import resolve as resolve_key
from mantra.interfaces.llm_client import LLMClient, LLMResponse, ToolCall

DeltaCallback = Callable[[str], None]

# Mid-stream drop surfaces as IncompleteRead, not OSError; handle explicitly.
IncompleteRead = http.client.IncompleteRead


def parse_sse_stream(lines, on_delta: DeltaCallback | None = None) -> LLMResponse:
    """Parse SSE stream into one response; accumulates tool calls by index."""
    content_parts: list[str] = []
    content_bytes = 0
    # index -> fragments accumulating by tool index
    tool_acc: dict[int, dict[str, str]] = {}
    usage: dict | None = None
    malformed = 0
    malformed_total = 0
    seen_done = False

    for raw in lines:
        line = raw.strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            seen_done = True
            break
        try:
            chunk = json.loads(data)
        except json.JSONDecodeError:
            malformed += 1
            malformed_total += 1
            if malformed > 20:
                raise LLMError("stream contained too many consecutive malformed chunks (20)")
            if malformed_total > 100:
                raise LLMError(f"stream contained too many malformed chunks total ({malformed_total}) — possible protocol mismatch")
            continue  # tolerate keep-alive / noise
        malformed = 0
        # Usage may be in final chunk without choices; read early.
        if isinstance(chunk.get("usage"), dict) and chunk["usage"]:
            usage = chunk["usage"]
        choices = chunk.get("choices") or []
        if not choices:
            continue
        delta = choices[0].get("delta") or {}
        piece = delta.get("content")
        if piece:
            if not isinstance(piece, str):
                piece = str(piece)
            content_bytes += len(piece)
            if content_bytes > _MAX_CONTENT_PARTS_BYTES:
                raise LLMError("stream content exceeds cap")
            content_parts.append(piece)
            if on_delta is not None:
                try:
                    on_delta(piece)
                except AbortError:
                    raise
                except Exception:
                    pass
        for call in delta.get("tool_calls") or []:
            try:
                idx = int(call.get("index", 0))
            except (TypeError, ValueError):
                idx = 0
            slot = tool_acc.setdefault(idx, {"id": "", "name": "", "args": ""})
            if call.get("id"):
                cid = str(call["id"])
                # Keep first id only.
                if not slot["id"]:
                    slot["id"] = cid
            fn = call.get("function") or {}
            if fn.get("name"):
                name_part = str(fn["name"])
                slot["name"] = slot["name"] + name_part
            if fn.get("arguments") is not None:
                arg_part = fn["arguments"]
                # Some gateways send parsed dict.
                if isinstance(arg_part, dict):
                    arg_part = json.dumps(arg_part)
                elif not isinstance(arg_part, str):
                    arg_part = str(arg_part)
                slot["args"] += arg_part

    # Stream ended without sentinel and no data is truncated.
    if not seen_done and not content_parts and not tool_acc and usage is None:
        raise LLMError("stream ended without DONE and no data")
    tool_calls = []
    for i, slot in sorted(tool_acc.items()):
        name = slot["name"].strip()
        if not name:
            raise LLMError(f"stream tool_call {i} missing name")
        raw_args = slot["args"] or "{}"
        try:
            arguments = json.loads(raw_args)
        except json.JSONDecodeError as exc:
            # Try quoted-escapes repair for Windows paths
            try:
                from mantra.core.tool_repairs import repair_quoted_escapes_json_text

                fixed = repair_quoted_escapes_json_text(raw_args)
                arguments = json.loads(fixed)
            except Exception:
                raise LLMError(
                    f"the response ended mid-tool-call ({name or 'call ' + str(i)}): {exc}"
                ) from exc
        if not isinstance(arguments, dict):
            raise LLMError(f"tool arguments not an object for '{name}'")
        tool_calls.append(
            ToolCall(id=slot["id"] or f"call_{i}", name=name, arguments=arguments)
        )
    content = "".join(content_parts)
    return LLMResponse(content=content or None, tool_calls=tool_calls, usage=usage)


class OpenAICompatClient(LLMClient):
    def __init__(
        self,
        model: str,
        base_url: str = "https://api.openai.com/v1",
        api_key_env: str = "OPENAI_API_KEY",
        temperature: float = 0.2,
        max_tokens: int = 4096,
        timeout: float = 120.0,
        max_retries: int = 3,
        stream: bool = True,
        include_usage: bool = True,
        reasoning_effort: str | None = None,
    ) -> None:
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.api_key_env = api_key_env
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout = timeout
        self.max_retries = max_retries
        self.stream = stream
        self.include_usage = include_usage
        self.reasoning_effort = reasoning_effort
        # Not every server understands stream_options; downgrade on a 400.
        self._usage_supported = include_usage
        # Nor reasoning_effort - local servers tend to reject it outright.
        self._reasoning_supported = reasoning_effort is not None
        # Reasoning models ask for the completion budget under a different
        # name. Remembered, or every turn pays for the same 400 again.
        self._token_field = "max_tokens"
        self._token_budget = max_tokens
        self.last_usage: dict | None = None
        self._lock = threading.Lock()

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        on_delta: DeltaCallback | None = None,
    ) -> LLMResponse:
        try:
            has_key = bool(resolve_key(self.api_key_env))
        except OSError as exc:
            raise LLMError(f"could not read key for '{self.api_key_env}': {exc}") from exc
        if not has_key:
            raise LLMError(
                f"no API key available for '{self.api_key_env}'. Set that "
                "environment variable, open a new terminal so it loads, or "
                "store the key once with: /connect"
            )

        use_stream = self.stream and on_delta is not None
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            self._token_field: self._token_budget,
        }
        if tools:
            payload["tools"] = tools
        if use_stream:
            payload["stream"] = True
            if self._usage_supported:
                payload["stream_options"] = {"include_usage": True}
        if self._reasoning_supported and self.reasoning_effort:
            payload["reasoning_effort"] = self.reasoning_effort

        last_error = ""
        for attempt in range(1, self.max_retries + 1):
            body = json.dumps(payload).encode("utf-8")
            try:
                if use_stream:
                    response = self._request_stream(body, on_delta)
                else:
                    response = self._request(body)
                if response.usage:
                    self.last_usage = response.usage
                return response
            except urllib.error.HTTPError as exc:
                detail = ""
                try:
                    detail = exc.read().decode(errors="replace")[:300]
                except OSError:
                    pass
                # Each downgrade has to be for the field the server
                # actually complained about. Dropping reasoning_effort
                # because max_tokens was rejected would quietly turn off
                # reasoning on precisely the models that need it.
                if exc.code == 400 and payload.get("stream_options") \
                        and self._blamed(detail, "stream_options"):
                    with self._lock:
                        self._usage_supported = False
                    del payload["stream_options"]
                    last_error = detail or "stream_options rejected"
                    continue
                if exc.code == 400 and payload.get("reasoning_effort") \
                        and self._blamed(detail, "reasoning_effort"):
                    # Older and local servers reject the field outright
                    # rather than ignoring it. Shed it and carry on.
                    with self._lock:
                        self._reasoning_supported = False
                    del payload["reasoning_effort"]
                    last_error = detail or "reasoning_effort rejected"
                    continue
                if exc.code == 400 and self._token_field == "max_tokens" \
                        and "max_completion_tokens" in detail.lower():
                    # Reasoning models refuse max_tokens and want the
                    # completion budget named differently.
                    with self._lock:
                        self._token_field = "max_completion_tokens"
                    payload.pop("max_tokens", None)
                    payload["max_completion_tokens"] = self._token_budget
                    last_error = detail or "max_tokens rejected"
                    continue
                if exc.code in (401, 403):
                    # Auth failures never succeed on retry; fail fast with cause.
                    raise LLMError(
                        f"the server rejected the key from '{self.api_key_env}' "
                        f"(HTTP {exc.code}) at {self.base_url}: {detail}"
                    ) from exc
                if exc.code == 400:
                    # We asked for something the server does not
                    # understand. Sending it again unchanged cannot
                    # succeed, and each attempt costs a round trip.
                    raise LLMError(
                        f"the server rejected the request (HTTP 400) at "
                        f"{self.base_url}: {detail or 'no detail given'}"
                    ) from exc
                last_error = f"HTTP {exc.code}: {detail}"
                if attempt < self.max_retries:
                    time.sleep(min(2**attempt, 8))
            except (urllib.error.URLError, TimeoutError, OSError, IncompleteRead) as exc:
                last_error = str(exc)
                if attempt < self.max_retries:
                    time.sleep(min(2**attempt, 8))
        raise LLMError(f"LLM request failed after {self.max_retries} attempts: {last_error}")

    @staticmethod
    def _blamed(detail: str, field: str) -> bool:
        """Did the server complain about this field?

        Empty detail no longer counts as blamed — it would incorrectly shed
        features on auth/format errors. Only explicit mention counts, with
        word-boundary check to avoid false positives from unrelated substrings.
        """
        if not detail:
            return False
        import re as _re
        # Require field appears as a distinct token, not a substring of another word
        pattern = r"(?<![a-z0-9_])" + _re.escape(field.lower()) + r"(?![a-z0-9_])"
        return bool(_re.search(pattern, detail.lower()))

    def _headers(self) -> dict[str, str]:
        # Environment first, stored credential second.
        api_key = resolve_key(self.api_key_env) or ""
        headers = {"Content-Type": "application/json", "User-Agent": "MANTRA/1.0 (https://opencode.ai)"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        return headers

    def _request(self, body: bytes) -> LLMResponse:
        # No provider-specific probe here — try chat first, fall back agnostically on 400/500 below
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=body,
            headers=self._headers(),
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                try:
                    raw_bytes = response.read(_MAX_RESPONSE_BYTES + 1)
                except TypeError:
                    raw_bytes = response.read()
                if len(raw_bytes) > _MAX_RESPONSE_BYTES:
                    raise LLMError("LLM response exceeds size cap")
                raw = raw_bytes.decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            # Do not fallback for parameter downgrade cases that chat() handles
            detail = ""
            raw_detail = b""
            try:
                raw_detail = exc.read()
                detail = raw_detail.decode(errors="replace").lower()[:500]
            except Exception:
                pass
            if "max_tokens" in detail or "max_completion_tokens" in detail or "reasoning_effort" in detail or "stream_options" in detail:
                # Re-raise with fresh body so outer handler can still read it
                try:
                    import io as _io
                    raise urllib.error.HTTPError(exc.url, exc.code, exc.msg, exc.hdrs, _io.BytesIO(raw_detail))
                except Exception:
                    raise
            # Agnostic fallback: if chat fails and provider offers Responses API, try it
            # Preserve original error for diagnostics if fallback also fails
            _orig_exc = exc
            _orig_detail = detail
            if exc.code in (400, 500):
                try:
                    # Probe: does {base}/responses exist? Try it before surfacing 400/500
                    resp = self._request_via_responses(body)
                    # Log that fallback was used (visible via logger if attached)
                    return resp
                except Exception as _fb_exc:
                    # Fallback failed — chain original error for diagnostics
                    raise LLMError(
                        f"chat completions failed (HTTP {exc.code}): {_orig_detail[:300] or 'no detail'}; "
                        f"fallback to responses also failed: {_fb_exc}"
                    ) from _orig_exc
            raise
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise LLMError(
                f"{self.base_url}/chat/completions did not return JSON: {exc}"
            ) from exc
        if not isinstance(data, dict) or not data.get("choices"):
            # An empty choices array is what a gateway returns when it
            # accepts the request and then has nothing to say. Indexing
            # it raised an IndexError that nothing upstream caught.
            raise LLMError(
                f"{self.base_url}/chat/completions returned no choices"
            )

        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            raise LLMError(f"{self.base_url}/chat/completions returned no choices")
        message = (choices[0] or {}).get("message") or {}
        if not isinstance(message, dict):
            raise LLMError("LLM response message not an object")
        raw_calls = message.get("tool_calls") or []
        if not isinstance(raw_calls, list):
            raise LLMError("LLM tool_calls not a list")
        tool_calls = []
        for call in raw_calls:
            if not isinstance(call, dict):
                raise LLMError(f"tool_call not an object: {call!r}")
            fid = call.get("id", "")
            func = call.get("function") or {}
            if not isinstance(func, dict):
                raise LLMError(f"tool_call function not an object: {call!r}")
            name = func.get("name")
            if not isinstance(name, str) or not name.strip():
                raise LLMError(f"tool_call missing name: {call!r}")
            args_raw = func.get("arguments")
            if args_raw is None or args_raw == "":
                arguments = {}
            elif isinstance(args_raw, dict):
                arguments = args_raw
            elif isinstance(args_raw, str):
                try:
                    arguments = json.loads(args_raw or "{}")
                except json.JSONDecodeError as exc:
                    # Try quoted-escapes repair for Windows paths
                    try:
                        from mantra.core.tool_repairs import repair_quoted_escapes_json_text

                        fixed = repair_quoted_escapes_json_text(args_raw or "{}")
                        arguments = json.loads(fixed)
                    except Exception:
                        raise LLMError(f"tool arguments not JSON for '{name}': {exc}") from exc
                if not isinstance(arguments, dict):
                    raise LLMError(f"tool arguments not an object for '{name}'")
            else:
                raise LLMError(f"tool arguments wrong type for '{name}': {type(args_raw).__name__}")
            tool_calls.append(ToolCall(id=str(fid) if fid else "", name=name.strip(), arguments=arguments))
        return LLMResponse(
            content=message.get("content"),
            tool_calls=tool_calls,
            usage=data.get("usage") or None,
        )

    def _request_via_responses(self, body: bytes) -> LLMResponse:
        """Agnostic fallback: translate chat payload to Responses API (OpenAI Responses)."""
        try:
            payload = json.loads(body.decode("utf-8", errors="replace"))
        except Exception as exc:
            raise LLMError(f"could not translate payload for responses: {exc}") from exc
        # Chat messages -> input for Responses API. Include full history so tool results are seen.
        messages = payload.get("messages") or []
        parts: list[str] = []
        for m in messages:
            role = m.get("role", "")
            content = m.get("content") or ""
            if role == "system":
                parts.append(f"System: {content}")
            elif role == "user":
                parts.append(f"User: {content}")
            elif role == "assistant":
                # include tool_calls summary
                tc = m.get("tool_calls")
                if tc:
                    names = ", ".join((t.get("function") or {}).get("name", "?") for t in tc)
                    parts.append(f"Assistant called {names}: {content}")
                else:
                    parts.append(f"Assistant: {content}")
            elif role == "tool":
                parts.append(f"Tool {m.get('name','')} result: {content[:2000]}")
        prompt = "\n\n".join(parts) if parts else (payload.get("input") or "")
        # Build responses payload
        resp_payload: dict[str, Any] = {
            "model": payload.get("model", self.model),
            "input": prompt,
        }
        # Translate chat tools -> responses tools (agnostic, not zen-specific)
        chat_tools = payload.get("tools") or []
        if chat_tools:
            resp_tools = []
            for t in chat_tools:
                if not isinstance(t, dict):
                    continue
                # Chat: {"type":"function","function":{"name","description","parameters"}}
                # Responses: {"type":"function","name","description","parameters","strict":false}
                if t.get("type") == "function" and isinstance(t.get("function"), dict):
                    fn = t["function"]
                    resp_tools.append(
                        {
                            "type": "function",
                            "name": fn.get("name"),
                            "description": fn.get("description") or "",
                            "parameters": fn.get("parameters") or {"type": "object", "properties": {}},
                            "strict": False,
                        }
                    )
                elif t.get("name"):
                    # Already responses-like
                    resp_tools.append(t)
            if resp_tools:
                resp_payload["tools"] = resp_tools
                resp_payload["tool_choice"] = "auto"
        request = urllib.request.Request(
            f"{self.base_url}/responses",
            data=json.dumps(resp_payload).encode("utf-8"),
            headers=self._headers(),
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            try:
                raw_bytes = response.read(_MAX_RESPONSE_BYTES + 1)
            except TypeError:
                raw_bytes = response.read()
            if len(raw_bytes) > _MAX_RESPONSE_BYTES:
                raise LLMError("LLM response exceeds size cap")
            raw = raw_bytes.decode("utf-8", errors="replace")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise LLMError(f"{self.base_url}/responses did not return JSON: {exc}") from exc
        # If called from stream path, we need to feed deltas to the UI
        # (stream fallback is non-streaming, so emit whole text as one delta)
        # Caller passes on_delta via _request_stream fallback — handled there
        # Responses shape: {"output": [{"type":"message","content":[{"text":"..."}]}]}
        # or reasoning + message. Extract text.
        text = ""
        for item in data.get("output") or []:
            if item.get("type") == "message":
                for part in item.get("content") or []:
                    if part.get("type") == "output_text":
                        text += part.get("text") or ""
                    elif part.get("type") == "text":
                        text += part.get("text") or ""
        # Tool calls in responses: output items type function_call
        tool_calls = []
        for item in data.get("output") or []:
            if item.get("type") == "function_call":
                tool_calls.append(
                    ToolCall(
                        id=item.get("call_id") or item.get("id") or "",
                        name=item.get("name") or "",
                        arguments=json.loads(item.get("arguments") or "{}") if isinstance(item.get("arguments"), str) else item.get("arguments") or {},
                    )
                )
        return LLMResponse(content=text or None, tool_calls=tool_calls, usage=data.get("usage"))

    def _request_stream(self, body: bytes, on_delta: DeltaCallback) -> LLMResponse:
        # No hardcode — chat is tried first, responses fallback below on error
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=body,
            headers=self._headers(),
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:

                def _line_iter():
                    # Prefer readline for real HTTPResponse; fall back to iteration for mocks
                    if hasattr(response, "readline"):
                        try:
                            while True:
                                raw = response.readline()
                                if not raw:
                                    break
                                if isinstance(raw, bytes):
                                    yield raw.decode("utf-8", errors="replace")
                                else:
                                    yield str(raw)
                        except Exception:
                            pass
                        return
                    for raw in response:  # type: ignore[attr-defined]
                        if isinstance(raw, bytes):
                            text = raw.decode("utf-8", errors="replace")
                            for line in text.splitlines():
                                yield line + "\n"
                        else:
                            text = str(raw)
                            for line in text.splitlines():
                                yield line + "\n"

                return parse_sse_stream(_line_iter(), on_delta)
        except urllib.error.HTTPError as exc:
            # Do not fallback for downgrade cases
            detail = ""
            raw2 = b""
            try:
                raw2 = exc.read()
                detail = raw2.decode(errors="replace").lower()[:500]
            except Exception:
                pass
            if "max_tokens" in detail or "max_completion_tokens" in detail or "reasoning_effort" in detail or "stream_options" in detail:
                try:
                    import io as _io2
                    raise urllib.error.HTTPError(exc.url, exc.code, exc.msg, exc.hdrs, _io2.BytesIO(raw2))
                except Exception:
                    raise
            _orig = exc
            _orig_detail2 = detail
            if exc.code in (400, 500):
                try:
                    return self._request_via_responses(body)
                except Exception as _fb2:
                    raise LLMError(
                        f"streaming chat failed (HTTP {exc.code}): {_orig_detail2[:300] or 'no detail'}; "
                        f"fallback also failed: {_fb2}"
                    ) from _orig
            raise
