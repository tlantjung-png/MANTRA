"""Orchestrator with injected deps; hooks for context, abort, approval."""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from core.agent.context import ContextManager
from core.agent.events import EventBus
from core.agent.approvals import _redact_sensitive
from core.agent.exceptions import AbortError, LLMError, SandboxError, ToolError
from core.agent.repairs import repair_arguments, validate_arguments
from core.types import EvaluationResult, Evaluator
from core.types import LLMClient
from core.types import Logger
from core.types import Sandbox
from core.types import Tool

DEFAULT_SYSTEM_PROMPT = (
    "You are a senior engineer and universal solver. Deliver correct, minimal, verified results for any task — "
    "coding, analysis, research, writing. Never hallucinate APIs, facts, or syntax; if unsure, say \"I don't know.\" "
    "Be direct, no fluff. Use Environment for workspace context — answer without tools when possible. "
    "For complex work: explore, plan, act, verify. Batch tools in one turn (list_dir + read_file), "
    "never repeat the same call, read before edit, confirm with tests before finishing."
)


@dataclass
class RunResult:
    """Result of one run: verdict, steps, timing, metrics."""

    task_id: str
    passed: bool
    evaluation_detail: str
    steps_used: int
    stopped_reason: str  # "final" | "max_steps" | "error" | "aborted"
    final_message: str | None = None
    metrics: dict[str, float] = field(default_factory=dict)
    elapsed_seconds: float = 0.0


class AgentLoop:
    """Run one task: provision, loop, evaluate, cleanup."""

    def __init__(
        self,
        llm: LLMClient,
        sandbox: Sandbox,
        tools: list[Tool],
        evaluator: Evaluator,
        logger: Logger,
        events: EventBus | None = None,
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
        max_steps: int = 30,
        on_delta: Any = None,
        context: ContextManager | None = None,
        abort: threading.Event | None = None,
        approver: Any = None,
        on_tool_result: Any = None,
    ) -> None:
        self.llm = llm
        self.sandbox = sandbox
        self.tools = {t.name: t for t in tools}
        self.evaluator = evaluator
        self.logger = logger
        self.events = events or EventBus()
        self.system_prompt = system_prompt
        self.max_steps = max_steps
        self.on_delta = on_delta
        self.context = context
        self.abort = abort
        self.approver = approver
        # Receives (tool_name, observation, step) right after each tool
        # executes, so a UI can show what the agent actually saw (command
        # output, file contents) without putting that content into event
        # payloads or the run log.
        self.on_tool_result = on_tool_result

    @property
    def aborted(self) -> bool:
        return bool(self.abort and self.abort.is_set())

    def run(self, task: dict[str, Any]) -> RunResult:
        task_id = str(task.get("task_id", "unnamed"))
        started = time.monotonic()
        context = self.context or ContextManager()
        self.context = context
        self._seed_context(context, task)
        # Share abort signal so sandbox exec can be interrupted.
        try:
            setattr(self.sandbox, "abort", self.abort)
        except Exception:
            pass

        tool_schemas = [t.schema() for t in self.tools.values()]

        self._emit("run_start", {"task_id": task_id})
        stopped_reason = "max_steps"
        final_message: str | None = None
        steps = 0
        aborted = False
        metrics: dict[str, float] = {"tool_errors": 0, "denied": 0}
        recent_calls: dict[str, int] = {}
        # Keys whose most recent real execution returned an error. A single
        # identical retry after a transient failure is legitimate (a network
        # blip, a flaky probe); the no-op block must not punish it, so the
        # block only fires when the previous result for the key was usable.
        recent_failed: set[str] = set()
        # A single empty final is often transient — a reasoning model can
        # spend its whole output budget on reasoning and emit nothing, or a
        # provider can drop a completion. Nudging once or twice usually
        # recovers it. Bounded retries keep the anti-burn guarantee: a
        # model that is *persistently* empty still fails fast instead of
        # exhausting max_steps with identical context.
        empty_finals = 0
        max_empty_finals = 2
        # A response cut off mid-tool-call (the output budget ran out while
        # the model was still writing a tool call's JSON, so the stream ends
        # with an unparseable half) raises from the client. Like an empty
        # final it is usually transient, so nudge and retry a bounded number
        # of times; only give up with actionable advice.
        truncated_calls = 0
        max_truncated_calls = 2

        try:
            self.sandbox.setup(task)
            while steps < self.max_steps:
                if self.aborted:
                    stopped_reason = "aborted"
                    break
                steps += 1

                try:
                    response = self.llm.chat(
                        context.messages, tools=tool_schemas, on_delta=self.on_delta
                    )
                except LLMError as exc:
                    message = str(exc)
                    # llm.py raises this with the stable prefix above; a
                    # coincidental substring in a provider message must
                    # not trigger the retry path. A structured flag on the
                    # exception, when a client sets one, also counts.
                    if not message.startswith(
                        "the response ended mid-tool-call"
                    ) and not getattr(exc, "retryable_truncation", False):
                        raise
                    truncated_calls += 1
                    if truncated_calls > max_truncated_calls:
                        # Surface the tool name, not the raw JSON decode
                        # error - the operator needs the cause (budget),
                        # not the truncated fragment.
                        import re as _re

                        m = _re.search(r"mid-tool-call \(([^)]*)\):", message)
                        tool = m.group(1) if m else ""
                        raise LLMError(
                            "the model response was cut off mid-tool-call"
                            + (f" ({tool})" if tool else "")
                            + " three times: the output budget was exhausted while "
                            "writing the tool call. Raise max_tokens or lower "
                            "reasoning_effort in the llm config, then retry."
                        ) from exc
                    # Transient: nudge once and let the model re-issue the
                    # call. Retrying with identical context would reproduce
                    # the same cut, so the nudge is what makes it recover.
                    context.append(
                        {
                            "role": "user",
                            "content": "(Your previous response was cut off mid-tool-call - "
                            "the output budget probably ran out while writing the tool call. "
                            "Re-issue the tool call in full now, or answer directly if the "
                            "previous tool results already suffice.)",
                        }
                    )
                    continue
                # Response must be LLMResponse-like.
                if response is None or not hasattr(response, "is_final"):
                    raise LLMError(f"LLM returned invalid response: {type(response).__name__}")
                self._absorb_usage(response, metrics)

                if response.is_final:
                    raw = response.content
                    content_str = raw if isinstance(raw, str) else (str(raw) if raw is not None else "")
                    if not content_str.strip() and not (getattr(response, "tool_calls", None) or []):
                        # Empty final with nothing to say. Retrying with the
                        # identical context would reproduce the same empty
                        # reply, so change the context (a nudge) and retry a
                        # bounded number of times before giving up.
                        empty_finals += 1
                        if empty_finals <= max_empty_finals:
                            context.append(
                                {
                                    "role": "user",
                                    "content": "(Your previous response was empty. "
                                    "Please provide your final answer now, even if brief.)",
                                }
                            )
                            continue
                        stopped_reason = "error"
                        final_message = "mantra error: model returned empty final"
                        break
                    stopped_reason = "final"
                    final_message = content_str
                    content = content_str
                    context.append({"role": "assistant", "content": content})
                    break

                # Deduplicate tool call IDs per turn without mutating original response
                seen_ids: set[str] = set()
                dedup_calls: list[tuple[Any, str]] = []
                for c in response.tool_calls or []:
                    orig = getattr(c, "id", "") or ""
                    cid = orig
                    counter = 0
                    if not cid:
                        # Some gateways omit call ids entirely. An empty id
                        # must never reach the API: providers that require a
                        # non-empty tool_call id reject the whole request.
                        # Synthesize a stable placeholder instead.
                        while not cid or cid in seen_ids:
                            cid = f"call_{counter}"
                            counter += 1
                    while cid in seen_ids:
                        counter += 1
                        cid = f"{orig}_{counter}" if orig else f"call_{counter}"
                    seen_ids.add(cid)
                    dedup_calls.append((c, cid))
                # Serialize tool arguments safely.
                tool_calls_payload = []
                for call, cid in dedup_calls:
                    try:
                        args_json = json.dumps(call.arguments)
                    except (TypeError, ValueError) as exc:
                        raise LLMError(f"tool arguments not serializable for '{call.name}': {exc}") from exc
                    tool_calls_payload.append(
                        {
                            "id": cid,
                            "type": "function",
                            "function": {
                                "name": call.name,
                                "arguments": args_json,
                            },
                        }
                    )
                # Record assistant turn once per reply.
                content = response.content if isinstance(response.content, str) else (str(response.content) if response.content else "")
                context.append(
                    {
                        "role": "assistant",
                        "content": content,
                        "tool_calls": tool_calls_payload,
                    }
                )

                for idx, (call, cid) in enumerate(dedup_calls):
                    if self.aborted:
                        stopped_reason = "aborted"
                        for rem_call, rem_cid in dedup_calls[idx:]:
                            context.append(
                                {
                                    "role": "tool",
                                    "tool_call_id": rem_cid,
                                    "name": rem_call.name,
                                    "content": "ERROR: interrupted by operator",
                                }
                            )
                        break
                    # Every call in the batch must end up with a tool message,
                    # whatever happens inside the tool. An abort raised from
                    # within a tool (the sandbox checks the abort signal
                    # mid-execution) used to escape the loop before the
                    # synthetic fill ran, leaving the just-appended assistant
                    # tool_calls without answering tool messages — and the
                    # next model request rejected by the provider until the
                    # conversation was cleared. The fill below runs first,
                    # then the abort propagates via the stopped_reason.
                    try:
                        self._process_tool_call(
                            task_id, steps, call, cid, context, metrics, recent_calls, recent_failed
                        )
                    except AbortError:
                        for rem_call, rem_cid in dedup_calls[idx:]:
                            context.append(
                                {
                                    "role": "tool",
                                    "tool_call_id": rem_cid,
                                    "name": rem_call.name,
                                    "content": "ERROR: interrupted by operator",
                                }
                            )
                        stopped_reason = "aborted"
                        break
                if stopped_reason == "aborted":
                    break

        except AbortError:
            aborted = True
            stopped_reason = "aborted"
            final_message = "(interrupted)"
        except (LLMError, SandboxError, ToolError) as exc:
            stopped_reason = "error"
            detail = _safe_error_text(exc)
            final_message = f"mantra error: {detail}"
            try:
                self._emit("run_error", {"task_id": task_id, "error": detail})
            except Exception:
                pass
        except Exception as exc:  # noqa: BLE001 - ensure RunResult even on unexpected crash
            stopped_reason = "error"
            detail = _safe_error_text(exc)
            final_message = f"mantra error: {detail}"
            try:
                self._emit("run_error", {"task_id": task_id, "error": detail})
            except Exception:
                pass
        finally:
            if aborted or stopped_reason == "aborted":
                evaluation = EvaluationResult(
                    passed=False, detail="interrupted before completion"
                )
            else:
                try:
                    evaluation = self.evaluator.evaluate(self.sandbox, task)
                except Exception as exc:  # noqa: BLE001 - verdict must exist
                    evaluation = EvaluationResult(
                        passed=False, detail=f"evaluator crashed: {exc}"
                    )
            try:
                self.sandbox.cleanup()
            except Exception:  # noqa: BLE001 - cleanup must not mask results
                pass

        elapsed = time.monotonic() - started
        result = RunResult(
            task_id=task_id,
            passed=evaluation.passed and stopped_reason not in ("error", "aborted"),
            evaluation_detail=evaluation.detail,
            steps_used=steps,
            stopped_reason=stopped_reason,
            final_message=final_message,
            metrics=dict(metrics),
            elapsed_seconds=elapsed,
        )
        self._emit("run_end", _result_payload(result))
        # The work is done and the result exists; a logger failure (disk
        # full, permissions, an implementation bug) must not turn a
        # completed run into an exception for the caller. Same contract
        # as _emit: log best-effort, never break run().
        try:
            self.logger.log("run_result", _result_payload(result))
        except Exception:
            pass
        return result

    def _process_tool_call(
        self,
        task_id: str,
        step: int,
        call,
        cid: str,
        context: ContextManager,
        metrics: dict[str, float],
        recent_calls: dict[str, int],
        recent_failed: set[str],
    ) -> None:
        """Run the loop breaker, dispatch one tool call, append its result.

        The key is the tool plus a canonical form of its arguments, so a
        windowed re-read (different offset/limit) is never mistaken for a
        repeat; a successful write/edit clears the read counters for that
        path and the run-command counters, so the natural verify step —
        re-running the same test command after a change — executes again
        while true no-op repetition stays blocked. A key whose previous
        execution *failed* keeps one identical retry: blocking it would
        punish recovering from a transient error. A second consecutive
        failure trips the counter and the call is refused, so the retry
        allowance stays bounded.
        """
        import hashlib

        written_path = ""
        try:
            args = call.arguments if isinstance(call.arguments, dict) else {}
            if call.name == "run_command":
                key = f"run_command|{str(args.get('command', '')).strip()}"
            elif call.name in ("read_file", "list_dir"):
                rest = {k: v for k, v in args.items() if k != "path"}
                rest_json = json.dumps(rest, sort_keys=True, ensure_ascii=False, default=str)
                key = (
                    f"{call.name}|{args.get('path', '')}|"
                    f"{hashlib.sha256(rest_json.encode('utf-8', errors='replace')).hexdigest()[:12]}"
                )
            elif call.name in ("write_file", "edit_file"):
                payload = json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)
                key = (
                    f"{call.name}|{args.get('path', '')}|"
                    f"{hashlib.sha256(payload.encode('utf-8', errors='replace')).hexdigest()[:12]}"
                )
                written_path = str(args.get("path", ""))
            else:
                payload = json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)
                key = f"{call.name}|{payload}" if len(payload) < 300 else (
                    f"{call.name}|{hashlib.sha256(payload.encode('utf-8', errors='replace')).hexdigest()[:12]}"
                )
            cnt = recent_calls.get(key, 0) + 1
            # Re-insert so the key just counted moves to the end
            # of the eviction order and can never be evicted by
            # its own increment.
            recent_calls.pop(key, None)
            recent_calls[key] = cnt
            # Bound registry to prevent unbounded growth
            if len(recent_calls) > 500:
                # Remove oldest 100 entries (dict preserves insertion order)
                oldest_keys = list(recent_calls.keys())[:100]
                for _k in oldest_keys:
                    recent_calls.pop(_k, None)
                    recent_failed.discard(_k)
        except (TypeError, ValueError):
            # Key serialization failed; fall back to the unhashed form.
            try:
                key = f"{call.name}:{json.dumps(call.arguments, sort_keys=True, ensure_ascii=False, default=str)}"
            except (TypeError, ValueError):
                key = f"{call.name}:{call.arguments}"
            cnt = recent_calls.get(key, 0) + 1
            recent_calls[key] = cnt
        # A failed execution leaves one identical retry open; a succeeded
        # (or previously recovered) call closes it again.
        prior_failed = key in recent_failed
        if cnt >= 3:
            observation = f"STOP RETRYING: you already called {call.name} with the same arguments {cnt} times. Use the previous result. If you need a different result, change the arguments (a different path, offset, or command)."
            metrics["tool_errors"] += 1
        elif cnt == 2 and not prior_failed:
            observation = f"ERROR: you already called {call.name} {call.arguments} — the result is already in history above. Do not repeat. Use it or try a different file (e.g. README.md, pyproject.toml)."
            metrics["tool_errors"] += 1
        else:
            observation = self._dispatch_tool(task_id, step, call, metrics)
            if str(observation).startswith("ERROR"):
                recent_failed.add(key)
            else:
                recent_failed.discard(key)
            if (
                written_path
                and isinstance(observation, str)
                and observation.startswith("OK")
            ):
                # A successful write/edit invalidates every earlier counter:
                # the workspace state the previous results describe no longer
                # exists, so a repeated read of the path or a re-run of the
                # same verification command must execute again. Only this
                # write's own counter survives.
                for done_key in list(recent_calls.keys()):
                    if done_key != key:
                        recent_calls.pop(done_key, None)
                        recent_failed.discard(done_key)
        context.append(
            {
                "role": "tool",
                "tool_call_id": cid,
                "name": call.name,
                "content": observation,
            }
        )

    def _dispatch_tool(
        self, task_id: str, step: int, call, metrics: dict[str, float]
    ) -> str:
        """Approve and execute one tool; failures become observations."""
        try:
            if self.approver is not None and not self.approver.check(call.name, call.arguments):
                metrics["denied"] += 1
                self._emit(
                    "tool_denied",
                    {"task_id": task_id, "step": step, "tool": call.name},
                )
                return (
                    f"ERROR: the operator denied '{call.name}'. Do not retry it; "
                    "explain what you would have done and ask, or use another approach."
                )
        except Exception as exc:  # noqa: BLE001 - approver must not crash run
            metrics["tool_errors"] += 1
            return f"ERROR: approval check failed for '{call.name}': {exc}"
        return self._execute_tool(task_id, step, call, metrics)

    def _execute_tool(
        self, task_id: str, step: int, call, metrics: dict[str, float]
    ) -> str:
        """Dispatch one tool call; every failure becomes an observation."""
        # Registry aliases (e.g. webfetch -> web_fetch) are normalised at
        # build time, so look up the canonical form here or an aliased call
        # would be reported as an unknown tool.
        name = call.name
        tool = self.tools.get(name)
        if tool is None:
            canonical = str(name or "").strip().lower().replace("-", "_")
            if canonical == "webfetch":
                canonical = "web_fetch"
            tool = self.tools.get(canonical)
        if tool is None:
            metrics["tool_errors"] += 1
            return f"ERROR: unknown tool '{call.name}'"
        # Validate-then-repair (Command Code harness engineering)
        args = dict(call.arguments) if isinstance(call.arguments, dict) else {}
        schema = getattr(tool, "parameters", None)
        issues = validate_arguments(args, schema)
        if issues:
            repaired, notes = repair_arguments(call.name, args, schema)
            re_issues = validate_arguments(repaired, schema)
            if not re_issues and repaired != args:
                # Repair succeeded — use repaired args and surface note
                call.arguments = repaired  # type: ignore[attr-defined]
                args = repaired
                self._emit(
                    "tool_repaired",
                    {"task_id": task_id, "step": step, "tool": call.name, "notes": notes, "issues": issues},
                )
                # Log best-effort, never break run() — same contract as _emit.
                try:
                    self.logger.log("tool_input_repaired", {"tool": call.name, "notes": notes})
                except Exception:
                    pass
            else:
                # Repair failed or no change — return model-readable retry
                try:
                    self.logger.log("tool_input_invalid", {"tool": call.name, "issues": issues})
                except Exception:
                    pass
                return (
                    f"ERROR: invalid arguments for '{call.name}': {'; '.join(issues)}. "
                    f"Expected {schema.get('properties', {}) if schema else 'valid args'}. "
                    f"Fix and retry the same tool call."
                )
        # Event/log payloads must not carry raw secrets: file contents and
        # command text can embed credentials, so arguments are redacted and
        # truncated exactly like the pre-tool-use audit log.
        redacted: dict[str, Any] = {}
        for key_, value in args.items():
            if isinstance(value, str):
                value = _redact_sensitive(value)
                if len(value) > 300:
                    value = value[:297] + "..."
            redacted[key_] = value
        self._emit(
            "tool_call",
            {"task_id": task_id, "step": step, "tool": call.name, "args": redacted},
        )
        started = time.monotonic()
        try:
            observation = tool.execute(self.sandbox, **args)
        except AbortError:
            raise
        except TypeError as exc:
            observation = f"ERROR: bad arguments for '{call.name}': {exc}"
        except Exception as exc:  # noqa: BLE001 - surface to the agent
            observation = f"ERROR: tool '{call.name}' failed: {exc}"
        if str(observation).startswith("ERROR"):
            metrics["tool_errors"] += 1
        # Include edit result so UI can show diffs.
        result_payload: dict = {
            "task_id": task_id,
            "step": step,
            "tool": call.name,
            "seconds": round(time.monotonic() - started, 3),
            "ok": not str(observation).startswith("ERROR"),
        }
        if call.name in ("edit_file", "write_file"):
            result_payload["result"] = observation
        # Give the UI the raw observation for display-only tools (command
        # output, file reads). Kept off the event payload and the run log
        # so output size and any embedded secrets stay between the agent
        # and the operator's screen.
        if self.on_tool_result is not None:
            try:
                self.on_tool_result(call.name, observation, step)
            except Exception:
                pass
        self._emit("tool_result", result_payload)
        return observation

    def _seed_context(self, context: ContextManager, task: dict[str, Any]) -> None:
        """Seed first turn or refresh the pinned prompt for an ongoing one.

        The system prompt is rebuilt every turn (goals, attached skills,
        environment facts and memory change mid-session, and a resumed
        session carries a stale one), so when history already exists the
        pinned first message is replaced in place instead of ignored.
        """
        rendered = self._render_task(task)
        # Check budget on both branches: a fresh seed bypasses append-time
        # truncation (seeded history is below the eviction floor), so an
        # oversized task would otherwise be sent to the model in full.
        if len(rendered) > context.max_chars:
            rendered = rendered[: max(1000, int(context.max_chars * 0.8))] + "\n... [truncated — task too large]"
        if not context.messages:
            context.seed(self.system_prompt, rendered)
        else:
            if context.messages[0].get("role") == "system":
                context.messages[0] = {"role": "system", "content": self.system_prompt}
                context.resync()
            context.append({"role": "user", "content": rendered})

    def _absorb_usage(self, response, metrics: dict[str, float]) -> None:
        usage_raw = getattr(response, "usage", None)
        # Normalize to dict: handle dict, object with __dict__, or attrs
        usage: dict[str, Any] | None = None
        if isinstance(usage_raw, dict):
            usage = usage_raw
        elif usage_raw is not None:
            try:
                # Try dict conversion, then attr fallback
                usage = dict(usage_raw)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                try:
                    # Object with attributes (e.g., OpenAI Usage)
                    usage = {k: getattr(usage_raw, k) for k in dir(usage_raw) if not k.startswith("_") and not callable(getattr(usage_raw, k, None))}
                    if not usage:
                        raise ValueError("empty usage object")
                except (TypeError, AttributeError, ValueError):
                    # Provider shapes we do not recognize; usage is optional.
                    metrics["usage_unknown"] = metrics.get("usage_unknown", 0) + 1
                    return
            if not isinstance(usage, dict) or not usage:
                metrics["usage_unknown"] = metrics.get("usage_unknown", 0) + 1
                return
        if usage is None:
            metrics["usage_unknown"] = metrics.get("usage_unknown", 0) + 1
            return
        def _to_int(v: Any) -> int | None:
            if isinstance(v, (int, float)):
                iv = int(v)
                return iv if iv >= 0 else None
            if isinstance(v, str) and v.strip().lstrip("-").isdigit():
                try:
                    iv = int(v.strip())
                    return iv if iv >= 0 else None
                except ValueError:
                    return None
            return None
        prompt = _to_int(usage.get("prompt_tokens"))
        if prompt is None:
            # Some providers use input_tokens / promptTokens
            prompt = _to_int(usage.get("input_tokens") or usage.get("promptTokens"))
        completion = _to_int(usage.get("completion_tokens"))
        if completion is None:
            completion = _to_int(usage.get("output_tokens") or usage.get("completionTokens") or usage.get("outputTokens"))
        if prompt is not None:
            metrics["tokens_in"] = metrics.get("tokens_in", 0) + prompt
        if completion is not None:
            metrics["tokens_out"] = metrics.get("tokens_out", 0) + completion
        # Prompt caching: providers report cached_tokens at various paths.
        cached = None
        details = usage.get("prompt_tokens_details")
        if isinstance(details, dict):
            cached = _to_int(details.get("cached_tokens"))
            if cached is None:
                cached = _to_int(details.get("cache_hit_tokens") or details.get("cached_prompt_tokens"))
        if cached is None:
            # Top-level variants
            for k in ("cached_tokens", "prompt_cache_hit_tokens", "cache_hit_tokens",
                      "cache_read_input_tokens", "cached_prompt_tokens", "cache_hit_input_tokens"):
                if k in usage:
                    cached = _to_int(usage.get(k))
                    if cached is not None:
                        break
        # Alternative nesting: usage.details or usage.cached
        if cached is None and isinstance(usage.get("details"), dict):
            cached = _to_int(usage["details"].get("cached_tokens"))
        if cached is not None:
            metrics["cache_hit"] = metrics.get("cache_hit", 0) + cached
        # Fallback when provider gives no usage at all: estimate from context size
        # so /cost and CACHE always show something useful.
        if prompt is None and completion is None:
            # Mark as estimated so display can note it if desired
            metrics["usage_estimated"] = metrics.get("usage_estimated", 0) + 1

    def _render_task(self, task: dict[str, Any]) -> str:
        raw_statement = task.get("problem_statement")
        statement = str(raw_statement).strip() if raw_statement is not None else ""
        parts = [statement] if statement else []
        repo = task.get("repo_url")
        if repo:
            repo_str = str(repo).strip()
            if repo_str:
                parts.append(f"Repository: {repo_str} @ {task.get('base_commit', 'HEAD')}")
        return "\n\n".join(parts)

    def _emit(self, event: str, payload: dict[str, Any]) -> None:
        try:
            self.events.emit(event, payload)
        except Exception:
            pass
        try:
            self.logger.log(event, payload)
        except Exception:
            pass


def _safe_error_text(exc: Exception) -> str:
    """Bounded, redacted exception text for operator-visible surfaces."""
    return _redact_sensitive(str(exc))[:500]


def _result_payload(result: RunResult) -> dict[str, Any]:
    return {
        "task_id": result.task_id,
        "passed": result.passed,
        "stopped_reason": result.stopped_reason,
        "steps_used": result.steps_used,
        "elapsed_seconds": round(result.elapsed_seconds, 3),
        "evaluation_detail": result.evaluation_detail,
        "metrics": result.metrics,
    }
