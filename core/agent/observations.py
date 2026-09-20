"""Observation reshaping: dense tool output for the model, raw for the operator.

The agent loop keeps two copies of every tool observation. The raw one goes
to the UI (``on_tool_result``) and to ``/fix``'s failure capture, so the
operator always sees exactly what the command printed. The copy appended to
the model's context passes through :func:`reshape` first.

What reshaping may do depends on what the model will *do* with the output,
so the tools are split in two:

**Telemetry** — ``run_command``, ``shell_output``, ``kill_shell``. Output the
model reads but never matches against. Blank-line runs, consecutive
duplicate lines and decorative separator rules are collapsed, and each
collapse is marked in place, so the model can see that something was elided
rather than silently trusting a shorter answer.

**Verbatim** — everything else, above all ``read_file``. Output the model
edits against: ``edit_file``'s anchor and stale-content checks match this
text, and ``search_code``/``git_diff`` results are quoted back into later
calls. Nothing here is reordered, deduplicated or re-spaced; only the
character cap applies, and it keeps both the head and the tail with a marked
elision between them.

Both kinds keep the leading ``ERROR`` / ``exit_code:`` / ``Note:`` header
verbatim, because the loop's control flow keys off it: a leading ``ERROR``
is what marks a failed call for repeat-blocking, and ``exit_code:`` is what
``/fix`` reads.

Reshaping runs after redaction, never before: it must not be able to re-join
a credential the redactor split across lines.
"""

from __future__ import annotations

import re

from core.term import _ANSI_RE

# Default ceiling for one observation in context. Observations are the
# largest single contributor to a turn's prompt and the context budget is
# counted in characters, so the cap is expressed in characters too.
DEFAULT_MAX_CHARS = 12_000

# Tools whose output is telemetry: safe to collapse, because nothing later
# matches against it.
TELEMETRY_TOOLS = frozenset({"run_command", "shell_output", "kill_shell"})

# Elision markers. Each states what was removed and how much, so a model
# reading a shortened observation knows it is reading a shortened one.
_CHARS_MARKER = "... [{n} characters elided] ..."
_LINES_MARKER = "... [{n} identical lines elided] ..."
_BLANK_MARKER = "... [{n} blank lines elided] ..."
_RULE_MARKER = "... [{n} separator lines elided] ..."

_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
# A decorative rule: nothing but box-drawing, dashes, equals, underscores,
# asterisks or spaces, and long enough to be furniture rather than content.
# Short ones ("---", "+++", "@@") are diff syntax and always stay.
_RULE_RE = re.compile(r"^[\s\u2500-\u257f\u2550*_=-]{20,}$")

# Header lines the loop's own logic reads. Everything up to and including
# the last of these is copied through untouched.
_HEADER_RE = re.compile(r"^(?:ERROR\b|exit_code:\s*\d+|Note:)")


def _clean(text: str) -> str:
    """Drop escapes, control bytes and CR, and expand tabs.

    Only ever applied to telemetry: a verbatim observation is compared
    against the file or command it came from, so normalising it would put
    the model's view out of step with the workspace.
    """
    text = _ANSI_RE.sub("", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\t", "    ")
    return _CTRL_RE.sub("", text)


def _split_header(lines: list[str]) -> tuple[list[str], list[str]]:
    """Leading header lines the loop depends on, and the body after them."""
    cut = 0
    for index, line in enumerate(lines):
        if _HEADER_RE.match(line):
            cut = index + 1
        elif line.strip():
            break  # the first real content line ends the header
    return lines[:cut], lines[cut:]


def _collapse(lines: list[str]) -> list[str]:
    """Collapse blank runs, consecutive duplicates and separator rules."""
    out: list[str] = []
    index = 0
    total = len(lines)
    while index < total:
        line = lines[index]
        if not line.strip():
            run = index
            while run < total and not lines[run].strip():
                run += 1
            count = run - index
            out.append(line if count == 1 else _BLANK_MARKER.format(n=count))
            index = run
            continue
        if _RULE_RE.match(line):
            run = index
            while run < total and _RULE_RE.match(lines[run]):
                run += 1
            count = run - index
            out.append(line if count == 1 else _RULE_MARKER.format(n=count))
            index = run
            continue
        run = index
        while run < total and lines[run] == line:
            run += 1
        count = run - index
        out.append(line if count == 1 else _LINES_MARKER.format(n=count - 1))
        index = run
    return out


def _cap(lines: list[str], max_chars: int, tail_biased: bool) -> list[str]:
    """Keep head and tail within the budget, marking what fell out.

    A tail-biased observation keeps 35% head and 65% tail, because a
    command's verdict and a log's last lines are what the model acts on; a
    verbatim observation keeps the inverse, because the start of a file is
    what was asked for.
    """
    total = sum(len(ln) + 1 for ln in lines)
    if total <= max_chars:
        return lines
    marker_cost = len(_CHARS_MARKER.format(n=total)) + 1
    head_share = 0.35 if tail_biased else 0.65
    head_budget = max(1, int(max_chars * head_share) - marker_cost // 2)
    tail_budget = max(1, max_chars - head_budget - marker_cost // 2)

    head: list[str] = []
    used = 0
    for line in lines:
        cost = len(line) + 1
        if head and used + cost > head_budget:
            break
        head.append(line)
        used += cost
    tail: list[str] = []
    used = 0
    for line in reversed(lines):
        cost = len(line) + 1
        if tail and used + cost > tail_budget:
            break
        tail.append(line)
        used += cost
    tail.reverse()
    dropped = sum(len(ln) + 1 for ln in lines[len(head): len(lines) - len(tail)])
    if dropped <= 0:
        return lines
    return head + [_CHARS_MARKER.format(n=dropped)] + tail


def reshape(tool: str, observation: str, max_chars: int = DEFAULT_MAX_CHARS) -> str:
    """The context copy of one tool observation.

    ``max_chars <= 0`` disables reshaping entirely and returns the input
    unchanged. A non-string or empty observation is returned as given: the
    caller decides what that means, and reshaping must not turn it into an
    empty string the model would read as "no output".
    """
    if not isinstance(observation, str) or not observation:
        return observation
    if max_chars is not None and max_chars <= 0:
        return observation
    if max_chars is None:
        max_chars = DEFAULT_MAX_CHARS

    telemetry = tool in TELEMETRY_TOOLS
    lines = _clean(observation).split("\n") if telemetry else observation.split("\n")
    if len(observation) <= max_chars and not telemetry:
        return observation  # verbatim and already inside the budget: untouched

    header, body = _split_header(lines)
    if telemetry:
        body = _collapse(body)
    budget = max(1, max_chars - sum(len(ln) + 1 for ln in header))
    body = _cap(body, budget, tail_biased=telemetry)
    out = "\n".join(header + body).rstrip("\n")
    return out or observation[:max_chars]


def reshape_observation(
    tool: str, observation: str, max_chars: int, metrics: dict | None = None
) -> str:
    """:func:`reshape` plus the accounting a run needs to prove the saving.

    Records the raw and reshaped sizes into ``metrics`` when one is given,
    so ``/cost`` and the run log can show what reshaping actually removed
    rather than leaving it an article of faith.
    """
    shaped = reshape(tool, observation, max_chars)
    if metrics is not None and isinstance(observation, str):
        metrics["observation_chars_raw"] = metrics.get("observation_chars_raw", 0) + len(observation)
        metrics["observation_chars_context"] = (
            metrics.get("observation_chars_context", 0) + len(shaped)
        )
        metrics["observation_chars_saved"] = (
            metrics.get("observation_chars_saved", 0) + max(0, len(observation) - len(shaped))
        )
    return shaped
