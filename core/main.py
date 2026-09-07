"""Headless runner: loads config and task, runs loop, grades result."""

from __future__ import annotations

import argparse
import json
import os
import sys

from core.config import load_config
from core.agent.loop import DEFAULT_SYSTEM_PROMPT, AgentLoop
from core.agent.events import EventBus
from core.agent.exceptions import ConfigError
from core.agent.knowledge import assemble_system_prompt
from core.registry import build_evaluator, build_llm, build_logger, build_sandbox, build_tools
from core.term import force_utf8_output

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _resolve_data_path(*parts: str) -> str:
    """Locate a bundled data file in a source tree or an installed wheel."""
    candidates = [
        os.path.join(PROJECT_ROOT, *parts),
        os.path.join(os.path.dirname(PROJECT_ROOT), *parts),
        os.path.join(sys.prefix, *parts),
    ]
    for cand in candidates:
        if os.path.isfile(cand):
            return cand
    return candidates[0]


class _HeadlessApprover:
    """Non-interactive approval policy for unattended runs.

    Nobody is at the terminal to answer a prompt, so each configured
    mode maps to its closest non-interactive form: plan refuses every
    mutation, as it does interactively; default and auto allow ordinary
    mutations but refuse destructive ones (a prompt cannot be answered);
    yolo allows everything, as it does interactively.
    """

    def __init__(self, mode: str) -> None:
        self.mode = mode

    def check(self, tool: str, arguments: dict) -> bool:
        from core.agent.approvals import MUTATING_TOOLS, classify

        if self.mode == "yolo":
            return True
        if self.mode == "plan" and tool in MUTATING_TOOLS:
            return False
        risk, _detail = classify(tool, arguments)
        if risk == "safe":
            return True
        if self.mode in ("default", "auto") and risk == "mutating":
            return True
        # destructive under default/auto: nothing can confirm it here
        return False


def _resolve_path(path: str) -> str:
    """Resolve input paths against cwd then project root.

    Relative logging paths are instead anchored to the project root in
    main() so the same config works from any working directory.
    """
    if os.path.exists(path):
        return path
    candidate = os.path.join(PROJECT_ROOT, path)
    if os.path.exists(candidate):
        return candidate
    return path  # let the normal error path report it


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mantra", description="MANTRA coding-agent harness")
    parser.add_argument("--config", required=True, help="Path to config file (resolved against cwd then project root; relative log paths inside anchor to project root)")
    parser.add_argument("--task", required=True, help="Path to task JSON file (resolved against cwd then project root)")
    args = parser.parse_args(argv)
    # UTF-8 streams keep event output and verdicts encodable even when the
    # run is piped through a narrow console or redirected to a file.
    force_utf8_output()

    try:
        config = load_config(_resolve_path(args.config))
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    try:
        task_path = _resolve_path(args.task)
        with open(task_path, "r", encoding="utf-8") as handle:
            task = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"error: cannot read task file: {exc}", file=sys.stderr)
        return 2
    if not isinstance(task, dict):
        print("error: task file must contain a JSON object", file=sys.stderr)
        return 2

    # Anchor relative log path to project root.
    log_path = config["logging"].get("path")
    if log_path and not os.path.isabs(log_path):
        config["logging"]["path"] = os.path.join(PROJECT_ROOT, log_path)
    stmt = task.get("problem_statement")
    if not isinstance(stmt, str) or not stmt.strip():
        print("error: task file must contain 'problem_statement'", file=sys.stderr)
        return 2

    try:
        llm = build_llm(config["llm"])
        sandbox = build_sandbox(config["sandbox"])
        tools = build_tools(config["tools"])
        evaluator = build_evaluator(config["evaluator"])
        logger = build_logger(config["logging"])
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    events = EventBus()
    events.subscribe(lambda name, payload: print(f"[{name}] {_brief(payload)}"))

    approval_mode = config.get("approvals", "default")
    print(f"[approvals] headless policy: {approval_mode} "
          "(plan refuses mutations; default/auto refuse destructive commands "
          "and interpreter one-liners; yolo allows all)")
    loop = AgentLoop(
        llm=llm,
        sandbox=sandbox,
        tools=tools,
        evaluator=evaluator,
        logger=logger,
        events=events,
        system_prompt=assemble_system_prompt(
            config.get("system_prompt") or DEFAULT_SYSTEM_PROMPT,
            known_failures_path=_resolve_data_path("knowledge", "known-failures.md"),
        ),
        max_steps=config.get("max_steps", 30),
        approver=_HeadlessApprover(approval_mode),
    )
    try:
        result = loop.run(task)
    finally:
        logger.close()  # flush and release the append handle

    verdict = "PASS" if result.passed else "FAIL"
    print(
        f"\n{verdict} task={result.task_id} steps={result.steps_used} "
        f"reason={result.stopped_reason} elapsed={result.elapsed_seconds:.1f}s"
    )
    if result.evaluation_detail:
        print(result.evaluation_detail)
    return 0 if result.passed else 1


def _brief(payload: dict) -> str:
    keys = ("tool", "step", "task_id", "passed", "stopped_reason")
    parts = [f"{k}={payload[k]}" for k in keys if k in payload]
    return " ".join(parts)


if __name__ == "__main__":
    sys.exit(main())
