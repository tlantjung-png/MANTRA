"""Orchestrator with injected deps; hooks for context, abort, approval."""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from mantra.core.context import ContextManager
from mantra.core.events import EventBus
from mantra.core.exceptions import AbortError, LLMError, SandboxError, ToolError
from mantra.core.tool_repairs import repair_arguments, validate_arguments
from mantra.interfaces.evaluator import EvaluationResult, Evaluator
from mantra.interfaces.llm_client import LLMClient
from mantra.interfaces.logger import Logger
from mantra.interfaces.sandbox import Sandbox
from mantra.interfaces.tool import Tool

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

        try:
            self.sandbox.setup(task)
            while steps < self.max_steps:
                if self.aborted:
                    stopped_reason = "aborted"
                    break
                steps += 1

                response = self.llm.chat(
                    context.messages, tools=tool_schemas, on_delta=self.on_delta
                )
                # Response must be LLMResponse-like.
                if response is None or not hasattr(response, "is_final"):
                    raise LLMError(f"LLM returned invalid response: {type(response).__name__}")
                self._absorb_usage(response, metrics)

                if response.is_final:
                    raw = response.content
                    content_str = raw if isinstance(raw, str) else (str(raw) if raw is not None else "")
                    if not content_str.strip() and not (getattr(response, "tool_calls", None) or []):
                        # Empty final — treat as error if model repeatedly empty
                        if steps >= self.max_steps:
                            stopped_reason = "error"
                            final_message = "mantra error: model returned empty final"
                            break
                        continue
                    stopped_reason = "final"
                    # Normalize final_message to string for downstream consumers
                    final_message = content_str if isinstance(raw, str) else (str(raw) if raw is not None else "")
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
                    # Intent-normalized loop breaker (12hex SHA256 of tool|primary)
                    # Uses full primary argument plus length to avoid collisions
                    # from truncation. Bounded registry prunes oldest entries.
                    try:
                        args = call.arguments if isinstance(call.arguments, dict) else {}
                        primary = ""
                        if call.name in ("read_file", "write_file", "edit_file", "list_dir"):
                            primary = str(args.get("path", ""))
                        elif call.name == "run_command":
                            primary = str(args.get("command", ""))
                        elif call.name == "search_code":
                            primary = str(args.get("query", ""))
                        elif call.name == "find_file":
                            primary = str(args.get("pattern", ""))
                        elif call.name == "web_fetch":
                            primary = str(args.get("url", ""))
                        elif call.name == "shell_output":
                            primary = str(args.get("task_id", ""))
                        elif call.name == "kill_shell":
                            primary = str(args.get("task_id") or args.get("pid") or args.get("port") or "")
                        else:
                            primary = json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)
                        import hashlib

                        # Include length so same-prefix different-length args differ
                        intent_input = f"{call.name}|{len(primary)}|{primary}"
                        intent_sig = hashlib.sha256(intent_input.encode("utf-8")).hexdigest()[:12]
                        exact_sig = hashlib.sha256(json.dumps(call.arguments, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8", errors="replace")).hexdigest()[:12] if call.arguments else "noargs"
                        key = f"{call.name}|{intent_sig}"
                        cnt = recent_calls.get(key, 0) + 1
                        recent_calls[key] = cnt
                        recent_calls[f"{key}|exact:{exact_sig}"] = recent_calls.get(f"{key}|exact:{exact_sig}", 0) + 1
                        # Bound registry to prevent unbounded growth
                        if len(recent_calls) > 500:
                            # Remove oldest 100 entries (dict preserves insertion order)
                            oldest_keys = list(recent_calls.keys())[:100]
                            for _k in oldest_keys:
                                recent_calls.pop(_k, None)
                    except Exception:
                        try:
                            key = f"{call.name}:{json.dumps(call.arguments, sort_keys=True, ensure_ascii=False, default=str)}"
                        except Exception:
                            key = f"{call.name}:{call.arguments}"
                        cnt = recent_calls.get(key, 0) + 1
                        recent_calls[key] = cnt
                    if cnt >= 3:
                        observation = f"STOP RETRYING: you already called {call.name} with same intent {cnt} times (intent {intent_sig}). Use the previous result. If you need a different result, change the primary argument (path/query/command) not the same one."
                        metrics["tool_errors"] += 1
                    elif cnt == 2:
                        observation = f"ERROR: you already called {call.name} {call.arguments} — result is already in history above. Do not repeat. Use it or try a different file (e.g. README.md, pyproject.toml)."
                        metrics["tool_errors"] += 1
                    else:
                        observation = self._dispatch_tool(task_id, steps, call, metrics)
                    context.append(
                        {
                            "role": "tool",
                            "tool_call_id": cid,
                            "name": call.name,
                            "content": observation,
                        }
                    )
                if stopped_reason == "aborted":
                    break
            else:
                stopped_reason = "max_steps"
        except AbortError:
            aborted = True
            stopped_reason = "aborted"
            final_message = "(interrupted)"
        except (LLMError, SandboxError, ToolError) as exc:
            stopped_reason = "error"
            final_message = f"mantra error: {exc}"
            try:
                self._emit("run_error", {"task_id": task_id, "error": str(exc)})
            except Exception:
                pass
        except Exception as exc:  # noqa: BLE001 - ensure RunResult even on unexpected crash
            stopped_reason = "error"
            final_message = f"mantra error: {exc}"
            try:
                self._emit("run_error", {"task_id": task_id, "error": str(exc)})
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
        self.logger.log("run_result", _result_payload(result))
        return result

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
        tool = self.tools.get(call.name)
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
                self.logger.log("tool_input_repaired", {"tool": call.name, "notes": notes})
            else:
                # Repair failed or no change — return model-readable retry
                self.logger.log("tool_input_invalid", {"tool": call.name, "issues": issues})
                return (
                    f"ERROR: invalid arguments for '{call.name}': {'; '.join(issues)}. "
                    f"Expected {schema.get('properties', {}) if schema else 'valid args'}. "
                    f"Fix and retry the same tool call."
                )
        self._emit(
            "tool_call",
            {"task_id": task_id, "step": step, "tool": call.name, "args": args},
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
        self._emit("tool_result", result_payload)
        return observation

    def _seed_context(self, context: ContextManager, task: dict[str, Any]) -> None:
        """Seed first turn or append to existing history, respecting budget."""
        rendered = self._render_task(task)
        if not context.messages:
            context.seed(self.system_prompt, rendered)
        else:
            # Check budget before append to avoid immediate truncation
            if len(rendered) > context.max_chars:
                rendered = rendered[: max(1000, int(context.max_chars * 0.8))] + "\n... [truncated — task too large]"
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
            except Exception:
                try:
                    # Object with attributes (e.g., OpenAI Usage)
                    usage = {k: getattr(usage_raw, k) for k in dir(usage_raw) if not k.startswith("_") and not callable(getattr(usage_raw, k, None))}
                    if not usage:
                        raise ValueError("empty usage object")
                except Exception:
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
