"""Next-step suggestions shown after a finished task.

A rule-based scanner over the conversation tail: the finished turn's user
prompt, assistant output, tool calls, and error lines suggest 1-4 follow-up
actions. Pure and deterministic - no LLM round-trip, no latency, no cost.

Rules are scored, not scanned in order: each rule collects evidence from
three zones - prompt, reply, tools - weighted by recency of relevance
(reply > tools > prompt: the reply states what was actually done, tools
prove it happened, the prompt only says what was asked). Ties break by
rule order, so output is deterministic.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class Suggestion:
    label: str          # chip text as painted, e.g. "run the tests"
    command: str        # exact prompt submitted when accepted


@dataclass(frozen=True)
class _Rule:
    pattern: re.Pattern
    label: str
    command: str
    weight: float = 1.0  # static importance bump for unusually strong signals


_RULES: list[_Rule] = [
    # Hard evidence of breakage outranks everything else.
    _Rule(re.compile(r"\bfail(?:ed|ure|ing|s)\b|\berror\b|\btraceback\b|\bexception\b", re.I),
          "diagnose the failure", "diagnose the failure and propose a fix", weight=1.5),
    _Rule(re.compile(r"\b(?:file|files)\b.*\b(?:edit|edits|write|written|creat|modifi)", re.I | re.S),
          "run the tests", "run the test suite and summarize failures"),
    # Edit-first ordering ("edit the files") triggers the same follow-up.
    _Rule(re.compile(r"\b(?:edit|edits|write|wrote|creat|modifi)\w*\b.*\bfiles?\b", re.I | re.S),
          "run the tests", "run the test suite and summarize failures"),
    _Rule(re.compile(r"\bcommit\b", re.I),
          "commit the changes", "commit the changes"),
    _Rule(re.compile(r"\bdiff\b|\bchanged?\b|\bmodif\w*\b", re.I | re.S),
          "show a summary of the changes", "show a summary of the changes made"),
    _Rule(re.compile(r"\btest", re.I),
          "add more test coverage", "add test coverage for the recent changes"),
    _Rule(re.compile(r"\bdocs?\b|\bdocument|\bdocstring|\bREADME", re.I),
          "update the docs", "update the documentation for the recent changes"),
    # Reply-aware: a green test report ("all N tests pass(ed)") or a
    # finished edit task is the classic commit-me moment - strong signal,
    # lives in the freshest zone.
    _Rule(re.compile(r"\b\d+ .{0,12}test.{0,20}\bpass|\ball .{0,20}tests? .{0,20}pass", re.I | re.S),
          "commit the changes", "commit the changes", weight=1.4),
    _Rule(re.compile(r"\bimplement\w*\b|\badded\b|\bupdated\b|\bedited\b|\brefactor\w*\b", re.I),
          "commit the changes", "commit the changes"),
]

# Evidence zones, most recent first. A match in an earlier (fresher)
# zone scores higher; matching in several zones accumulates.
_ZONE_WEIGHTS = (("reply", 3.0), ("tools", 2.0), ("prompt", 1.0))


def suggestions_for(prompt: str, output: str, tool_text: str, had_error: bool,
                    max_items: int = 3) -> list[Suggestion]:
    """Derive follow-up chips from the finished turn.

    ``prompt`` is the user's message, ``output`` the assistant's visible
    reply, ``tool_text`` concatenated tool-call summaries (file paths,
    command lines), ``had_error`` whether the turn surfaced an error.

    The most relevant chip comes first: rules are ranked by recency-
    weighted evidence (reply hits beat tool hits beat prompt hits), with
    the static rule weight as a tiebreaker and rule order as the final
    deterministic tiebreaker.
    """
    zones = {"prompt": prompt or "", "reply": output or "", "tools": tool_text or ""}
    scored: list[tuple[float, int, str, str]] = []  # (score, rule_idx, label, command)
    for idx, rule in enumerate(_RULES):
        score = 0.0
        matched = False
        for zone, zone_weight in _ZONE_WEIGHTS:
            if rule.pattern.search(zones[zone]):
                matched = True
                score += zone_weight
        if matched:
            scored.append((score * rule.weight, idx, rule.label, rule.command))

    # Errors always lead: a broken state is more urgent than any
    # follow-up work the text might suggest.
    out: list[Suggestion] = []
    seen: set[str] = set()

    def add(label: str, command: str) -> None:
        if label not in seen and len(out) < max(1, max_items):
            seen.add(label)
            out.append(Suggestion(label=label, command=command))

    if had_error:
        add("diagnose the failure", "diagnose the failure and propose a fix")

    # Highest score first; ties fall back to rule order (stable sort on
    # the index keeps this deterministic).
    scored.sort(key=lambda item: (-item[0], item[1]))
    for _score, _idx, label, command in scored:
        add(label, command)

    # Nothing matched but there is evidence of work (tool calls or an
    # error): a generic opener keeps the row useful. Pure conversation
    # (greetings, questions) earns no chips.
    combined = f"{prompt}\n{output}\n{tool_text}"
    if not out and (tool_text.strip() or had_error) and combined.strip():
        add("show a summary of the changes", "show a summary of the changes made")
    return out
