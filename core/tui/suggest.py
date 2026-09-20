"""Next-step suggestions shown after a finished task.

A rule-based scanner over the finished turn's *evidence*: the operator's
prompt, the assistant's reply, the tool calls that actually ran, the files
they changed, and any error the turn surfaced. Pure and deterministic - no
LLM round-trip, no latency, no cost.

Suggestions are ranked in three tiers, so the top of the row always
describes the work that actually happened:

1. **Evidence** - a failure leads; an edited file earns "run the tests for
   <file>"; a verified change earns "commit the N changed files". These
   name the real artifacts, which is what makes a row feel like it belongs
   to this conversation rather than to any conversation.
2. **Subject** - the operator's own words ("fix the login redirect bug")
   reserve a slot, so the row always points back at what was asked.
3. **Keywords** - the classic follow-ups (docs, coverage, a summary of the
   changes) fill whatever is left.

Ties break by tier then by insertion order, so output is deterministic.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class Suggestion:
    label: str          # row text as painted, e.g. "run the tests for app.py"
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
# this set are what the suggestions echo back.
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
# failing" as the subject rather than a lecture-shaped suggestion.
_SCAFFOLD_RE = re.compile(
    r"^(?:please\s+|can you\s+|could you\s+|would you\s+|i want to\s+|"
    r"i need to\s+|help me\s+|how (?:do|can) i\s+|why (?:is|are|does|do)\s+|"
    r"what(?:'s| is| are)\s+|lets?\s+|let us\s+)",
    re.I,
)

# A prompt that opens with one of these asks for understanding, so the
# subject row offers to go deeper rather than to keep working.
_QUESTION_RE = re.compile(
    r"^(?:why|what|how|when|where|which|who|is|are|does|do|did|can|could|should|explain)\b",
    re.I,
)

# Path-shaped tokens: a separator plus an extension, or a bare filename
# with a known code/data extension. Tool observations quote both.
_PATH_RE = re.compile(
    r"[A-Za-z0-9_.@\-]+(?:[/\\][A-Za-z0-9_.\-]+)+"
    r"|\b[\w\-]+\.[A-Za-z0-9]{1,6}\b"
)
# Extensions that mark a token as a file worth naming back.
_CODE_SUFFIXES = frozenset(
    """py js jsx ts tsx json md txt yaml yml toml ini cfg html css scss
    c h cc cpp hpp cs java go rs rb php sh ps1 bat sql csv xml""".split()
)
_EDIT_TOOLS = ("write_file", "edit_file")
_TEST_RE = re.compile(r"\bpytest\b|\bunittest\b|\bnpm test\b|\bjest\b|\bvitest\b|\bcargo test\b", re.I)
_FAIL_RE = re.compile(r"\bfail(?:ed|ure|ing|s)?\b|\berror\b|\btraceback\b|\bexception\b", re.I)
_PASS_RE = re.compile(
    r"\b\d+\s+(?:passed|passing)\b|\ball\s+\d*\s*tests?\s+pass|\b0\s+failed\b|\bOK\b", re.I
)


def topic_of(prompt: str) -> str:
    """The conversation's subject, in the operator's own words.

    Politeness and question scaffolding are dropped, content words kept,
    and the phrase capped so a row never wraps. Empty when the prompt
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


def _files_in(text: str) -> list[str]:
    """Path-shaped tokens in ``text``, first-seen order, deduped."""
    out: list[str] = []
    seen: set[str] = set()
    for token in _PATH_RE.findall(text or ""):
        clean = token.strip(".,;:()[]{}'\"`")
        suffix = clean.rsplit(".", 1)[-1].lower() if "." in clean else ""
        # A path with a separator is a file whatever its extension; a bare
        # token needs a code/data extension so "e.g." and "v1.2" are not
        # mistaken for filenames.
        if "/" not in clean and "\\" not in clean and suffix not in _CODE_SUFFIXES:
            continue
        key = clean.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(clean)
    return out


def _short_name(path: str) -> str:
    """The part of a path a human recognises: its basename."""
    return re.split(r"[/\\]", path)[-1] or path


def _evidence(
    prompt: str, reply: str, tool_text: str, changed_files: list[str], recent_topics: list[str]
) -> dict:
    """What the turn actually did, read off the three zones plus the ledger."""
    tools = tool_text or ""
    reply = reply or ""
    ran_tests = bool(_TEST_RE.search(tools) or _TEST_RE.search(reply))
    edited = bool(
        changed_files
        or any(name in tools for name in _EDIT_TOOLS)
        or re.search(r"\b(?:edit|wrote|created|updated|modified)\b.{0,40}\bfile", reply, re.I | re.S)
    )
    failed = bool(
        _FAIL_RE.search(tools)
        or _FAIL_RE.search(reply)
        or re.search(r"exit_code:\s*[1-9]", tools)
    )
    passed = bool(_PASS_RE.search(reply) or _PASS_RE.search(tools))
    # Files: the ledger's changed set is the strongest evidence, then the
    # paths the tool observations quote, then the ones the reply names.
    files: list[str] = []
    for path in list(changed_files) + _files_in(tools) + _files_in(reply):
        if path not in files:
            files.append(path)
    # A content-free prompt ("thanks", "ok") inherits the previous
    # subject, so the row keeps pointing at the thread in progress.
    subject = topic_of(prompt) or (recent_topics[0] if recent_topics else "")
    return {
        "files": files,
        "ran_tests": ran_tests,
        "edited": edited,
        "failed": failed,
        "passed": passed,
        "subject": subject,
        "asks_why": bool(_QUESTION_RE.match(_SCAFFOLD_RE.sub("", (prompt or "").strip(), count=1))),
    }


def suggestions_for(
    prompt: str,
    output: str,
    tool_text: str,
    had_error: bool,
    max_items: int = 3,
    recent_topics: list[str] | None = None,
    changed_files: list[str] | None = None,
) -> list[Suggestion]:
    """Derive follow-up rows from the finished turn.

    ``prompt`` is the operator's message, ``output`` the assistant's visible
    reply, ``tool_text`` the tool observations, ``had_error`` whether the
    turn surfaced an error, ``recent_topics`` subjects of earlier turns
    (newest first), and ``changed_files`` the workspace paths this turn
    actually wrote - the strongest evidence available, straight from the
    session's change ledger.

    Rows come back highest-evidence first: what the turn did, then what the
    operator asked about, then the generic follow-ups that fill the row.
    """
    limit = max(1, max_items)
    evidence = _evidence(prompt, output, tool_text, changed_files or [], recent_topics or [])
    files = evidence["files"]
    subject = evidence["subject"]
    # One file is named; several collapse into a count so a row never wraps.
    named = _short_name(files[0]) if files else ""
    others = len(files) - 1

    out: list[Suggestion] = []
    seen: set[str] = set()
    scores: list[float] = []

    def add(label: str, command: str, weight: float) -> None:
        key = label.lower()
        if key in seen or len(out) >= limit:
            return
        seen.add(key)
        out.append(Suggestion(label=label, command=command))
        scores.append(weight)

    # ── tier 1: evidence - what the turn actually did ──────────────
    if evidence["failed"] or had_error:
        if named:
            add(f"diagnose the failure in {named}",
                f"diagnose the failure and propose a fix for {named}", 10.0)
        add("diagnose the failure", "diagnose the failure and propose a fix", 9.5)

    if evidence["edited"]:
        if evidence["failed"] or (evidence["ran_tests"] and not evidence["passed"]):
            if named:
                add(f"run the tests for {named}",
                    f"run the tests for {named} and report what fails", 8.0)
            add("run the test suite", "run the test suite and summarize failures", 7.0)
        elif evidence["passed"] or evidence["ran_tests"]:
            if len(files) > 1:
                add(f"commit the {len(files)} changed files",
                    f"review the diff and commit the {len(files)} changed files", 7.5)
            elif named:
                add(f"commit {named}", f"review the diff and commit {named}", 7.5)
            add("commit the changes", "review the diff and commit the changes", 6.0)
        else:
            if named:
                add(f"run the tests for {named}",
                    f"run the tests for {named} and report the result", 6.5)
            add("run the test suite", "run the test suite and summarize failures", 5.0)
        if others > 0:
            add(f"review the {others} other changed file{'s' if others != 1 else ''}",
                f"review the changes in the {others} other files this turn touched", 4.0)
    elif evidence["passed"] and not evidence["failed"]:
        # A green test report is the commit moment even when the turn's
        # own edits are not visible here.
        add("commit the changes", "review the diff and commit the changes", 5.5)

    # ── tier 2: the subject, in the operator's own words ───────────
    # Weighted below any keyword hit on purpose: the subject guarantees a
    # slot in the row, it does not outrank a follow-up the turn actually
    # earned.
    if subject:
        if evidence["asks_why"] or evidence["failed"] or had_error:
            add(f"explain {subject} in more detail",
                f"explain {subject} in more detail", 0.9)
        else:
            add(f"continue with {subject}", f"continue working on: {subject}", 0.9)

    # ── tier 3: the classic keyword follow-ups, to fill the row ────
    zones = {"prompt": prompt or "", "reply": output or "", "tools": tool_text or ""}
    keyword: list[tuple[float, int, str, str]] = []
    for idx, rule in enumerate(_RULES):
        score = 0.0
        for zone, zone_weight in _ZONE_WEIGHTS:
            if rule.pattern.search(zones[zone]):
                score += zone_weight
        if score:
            keyword.append((score * rule.weight, idx, rule.label, rule.command))
    keyword.sort(key=lambda item: (-item[0], item[1]))

    # The subject reserves a slot: without it a row of generic follow-ups
    # answers a request nobody made.
    subject_rows = sum(
        1 for label, _cmd in [(s.label, s.command) for s in out]
        if subject and subject in label
    )
    keyword_cap = limit - (1 if subject and not subject_rows else 0)
    for _score, _idx, label, command in keyword:
        if len(out) >= keyword_cap:
            break
        add(label, command, _score)

    # An older thread resurfaces when the subject just changed.
    topics = [t for t in (recent_topics or []) if t]
    older = [t for t in topics if t != subject]
    if older and len(out) < limit:
        add(f"return to {older[0]}", f"go back to: {older[0]}", 1.0)

    # The row never vanishes: a turn no rule recognised still earns rows
    # drawn from the conversation rather than from a canned list.
    if not out:
        add("what are the next steps?", "what are the next steps?", 0.5)
        if len(out) < limit:
            add("summarize this conversation",
                "summarize this conversation so far in a short paragraph", 0.4)

    order = sorted(range(len(out)), key=lambda i: (-scores[i], i))
    return [out[i] for i in order][:limit]
