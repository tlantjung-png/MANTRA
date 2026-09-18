"""Next-step suggestions shown after a finished task.

A rule-based scanner over the conversation tail: the finished turn's user
prompt, assistant output, tool calls, and error lines suggest 1-3 follow-up
actions. Pure and deterministic - no LLM round-trip, no latency, no cost.

Chips stay tied to the conversation: the subject of the operator's own
prompt (stopwords stripped) seeds a "continue with <subject>" chip on
every row, and when no rule matches, subject-based conversational chips
fill in so every agent reply earns a row. The /suggestions toggle is the
only off switch.

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

# Words that never carry the subject of a request. Content words outside
# this set are what the chips echo back.
_STOPWORDS = frozenset(
    """a an and are as at be been but by can could did do does for from had
    has have help how i if in into is it its me my not of on or our please
    should so some tell that the their them then there these they this to
    us was we were what when where which who why will with would you your
    about want need make just really very thing things
    hi hello hey thanks thank ok okay bye welcome greet greetings
    done cool nice great good sure yup yes no yeah nah
    here there now then""".split()
)

# Request scaffolding that prefixes the actual subject. Stripped once,
# anchored at the start, so "why is the build failing" yields "build
# failing" as the topic rather than a lecture-shaped chip.
_SCAFFOLD_RE = re.compile(
    r"^(?:please\s+|can you\s+|could you\s+|would you\s+|i want to\s+|"
    r"i need to\s+|help me\s+|how (?:do|can) i\s+|why (?:is|are|does|do)\s+|"
    r"what(?:'s| is| are)\s+|lets?\s+|let us\s+)",
    re.I,
)


def topic_of(prompt: str) -> str:
    """The conversation's subject, in the operator's own words.

    Politeness and question scaffolding are dropped, content words kept,
    and the phrase capped so a chip never wraps. Empty when the prompt
    carries no content words at all (greetings, thanks) - callers fall
    back to a generic row in that case.
    """
    text = re.sub(r"\s+", " ", (prompt or "")).strip()
    if not text:
        return ""
    # One pass would miss stacked scaffolding ("please can you"), so
    # strip repeatedly until the head stops changing.
    while True:
        stripped = _SCAFFOLD_RE.sub("", text, count=1)
        if stripped == text:
            break
        text = stripped
    words = [
        w for w in re.findall(r"[A-Za-z0-9_][\w.\\/\-]*", text)
        if w.lower() not in _STOPWORDS
    ]
    if not words:
        return ""
    return " ".join(words[:6])[:48].rstrip()


def suggestions_for(prompt: str, output: str, tool_text: str, had_error: bool,
                    max_items: int = 3,
                    recent_topics: list[str] | None = None) -> list[Suggestion]:
    """Derive follow-up chips from the finished turn.

    ``prompt`` is the user's message, ``output`` the assistant's visible
    reply, ``tool_text`` concatenated tool-call summaries (file paths,
    command lines), ``had_error`` whether the turn surfaced an error.
    ``recent_topics`` lists subjects of earlier turns, newest first: the
    row can then jump back to an older thread, and a content-free turn
    inherits the previous subject instead of going generic.

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

    # Tie the row to the conversation: the operator's own subject
    # reserves a slot, so a row of rule chips is never all-generic. A
    # content-free turn ("thanks", "ok") inherits the previous subject,
    # so the row keeps pointing at the thread actually in progress.
    topics = [t for t in (recent_topics or []) if t]
    topic = topic_of(prompt) or (topics[0] if topics else "")
    rule_cap = max(1, max_items) - 1 if topic else max(1, max_items)

    if had_error:
        add("diagnose the failure", "diagnose the failure and propose a fix")

    # Highest score first; ties fall back to rule order (stable sort on
    # the index keeps this deterministic).
    scored.sort(key=lambda item: (-item[0], item[1]))
    for _score, _idx, label, command in scored:
        if len(out) >= rule_cap:
            break
        add(label, command)

    if topic and all(topic not in s.command for s in out):
        add(f"continue with {topic}", f"continue working on: {topic}")

    # An older thread resurfaces as a jump-back chip: switching subjects
    # between turns is exactly when "return to the other thing" saves a
    # scroll-back through the transcript.
    older = [t for t in topics if t != topic]
    if older and len(out) < max(1, max_items) and all(older[0] not in s.command for s in out):
        add(f"return to {older[0]}", f"go back to: {older[0]}")

    # The row never vanishes: a reply no rule recognised falls back to
    # chips drawn from the conversation. With a usable subject the
    # continuation chip above is joined by a detail chip; a content-free
    # prompt (greetings, thanks) gets the generic conversational pair.
    if not out:
        add("what are the next steps?", "what are the next steps?")
        add("summarize this conversation", "summarize this conversation so far in a short paragraph")
    elif topic and not scored:
        add("explain that in more detail", "explain your last answer in more detail")
    return out
