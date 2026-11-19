"""Deterministic prose pattern audit for MANTRA's writing skill.

Finds the common tells of machine-sounding prose with plain regexes: no
model call, no network, no dependency, and no detector score. Findings are
advisory input for the ``humanize`` skill, not a gate.

The pattern taxonomy follows Wikipedia's "Signs of AI writing"; the rules
here are original, deliberately narrow so ordinary prose is not nagged.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any

from core.types import Sandbox, Tool

_MAX_FILE_BYTES = 500_000
_MAX_TEXT_CHARS = 20_000
_MAX_FINDINGS = 50
_DEFAULT_FINDINGS = 20
_SNIPPET_CHARS = 100
_MAX_OUTPUT_CHARS = 8_000
_WEAK_THRESHOLD = 3


@dataclass(frozen=True)
class Finding:
    rule: str
    title: str
    line: int
    paragraph: int
    snippet: str
    hint: str
    severity: str


# Strong tells: report on sight. Each rule is (id, title, regex, hint).
_STRONG_RULES: tuple[tuple[str, str, re.Pattern[str], str], ...] = (
    (
        "P01",
        "not-X-but-Y",
        re.compile(r"\bnot\s+(?:just|only|mainly|simply)\b[^.!?\n]{1,100}?\b(?:but|it's|it is)\b", re.I),
        "state the point directly",
    ),
    (
        "P03",
        "saying that sounds deep",
        re.compile(r"\b(?:at its core|what really matters is|at the end of the day|the bottom line is)\b", re.I),
        "replace the saying with the specific claim",
    ),
    (
        "P04",
        "staged run-up",
        re.compile(r"^\s*(?:here'?s the thing|honestly\?|let'?s dive in|let me tell you|first things first|now,? here'?s)\b", re.I),
        "drop the run-up and state the point",
    ),
    (
        "P05",
        "arguing with no one",
        re.compile(r"\b(?:i'?m not saying|this isn'?t (?:mainly|really) about|a tempting approach would be)\b", re.I),
        "remove the unraised objection; keep the real claim",
    ),
    (
        "P06",
        "forced abstract triad",
        re.compile(
            r"\b[a-z]{5,}(?:ion|ity|ance|ence|ment), [a-z]{5,}(?:ion|ity|ance|ence|ment), "
            r"and [a-z]{5,}(?:ion|ity|ance|ence|ment|s)\b",
            re.I,
        ),
        "use the number of items the meaning needs",
    ),
    (
        "P12",
        "overused AI word",
        re.compile(
            r"\b(?:delve|testament|showcas\w*|pivotal|tapestry|underscore\w*|"
            r"multifaceted|intricate|seamless|paradigm[\s-]shift|game[\s-]changer)\b",
            re.I,
        ),
        "use plain words",
    ),
    (
        "P13",
        "inflated significance",
        re.compile(
            r"\b(?:a new era|the future looks bright|continues to thrive|"
            r"despite (?:all )?challenges|marking a pivotal moment)\b",
            re.I,
        ),
        "keep the fact and drop the significance",
    ),
    (
        "P20",
        "decorative heading",
        re.compile(r"^\s*#{1,6}\s+.*[\U0001F300-\U0001FAFF\u2190-\u21FF\u2600-\u27BF]|\s[\U0001F300-\U0001FAFF\u2190-\u21FF\u2600-\u27BF]\s+\w", re.I),
        "remove emoji and arrows; name what the section holds",
    ),
    (
        "P22",
        "chatbot residue",
        re.compile(
            r"^\s*(?:great question!|sure[,!]|certainly[,!]|of course[,!]|i hope this helps|"
            r"happy to help|let me know if)(?=\s|$)|\bi hope this helps\b",
            re.I,
        ),
        "remove the wrapper and keep the content",
    ),
)

# Weak tells: only reported when at least three land in one paragraph.
_WEAK_RULES: tuple[tuple[str, str, re.Pattern[str], str], ...] = (
    (
        "P08",
        "dashes as universal connector",
        re.compile(r"\u2014"),
        "use periods, commas, colons, or parentheses",
    ),
    (
        "P09",
        "stacked qualifiers",
        re.compile(r"\b(?:could|may|might)\s+\w+\s+(?:possibly|potentially)\b|\b(?:potentially|possibly)\s+\w+\s+(?:could|may|might)\b", re.I),
        "keep only qualifiers the source supports",
    ),
    (
        "P21",
        "curly quotation marks",
        re.compile(r"[\u201c\u201d]"),
        "use straight quotes",
    ),
)

_DISABLE_RE = re.compile(r"prose-?audit-disable\s+([Pp]\d+)", re.I)
_DISABLE_ALL_RE = re.compile(r"prose-?audit-disable-all", re.I)
_CLOSER_RE = re.compile(
    r"^(?:that'?s the (?:real )?(?:win|point)|let that sink in|no \w+\.?\s+no \w+|"
    r"this shows the importance|in conclusion|to summarize|that is the takeaway)\b",
    re.I,
)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
_SENTENCE_START_RE = re.compile(r"([A-Z][a-z]{2,})")
_BOLD_RE = re.compile(r"\*\*[^*\n]{1,60}\*\*")
_HYPHEN_PAIR_RE = re.compile(r"\b[a-z]{2,}-[a-z]{2,}\b", re.I)
_PASSIVE_RE = re.compile(r"\b(?:is|are|was|were)\s+[a-z]+ed\b", re.I)
_FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})")


def _masked_lines(text: str) -> list[str]:
    """Blank frontmatter, fenced code, and inline code; keep line numbers."""
    lines = text.splitlines()
    out: list[str] = []
    in_frontmatter = bool(lines) and lines[0].strip() == "---"
    in_fence = False
    for line in lines:
        if in_frontmatter:
            out.append("")
            if line.strip() in ("---", "..."):
                in_frontmatter = False
            continue
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            out.append("")
            continue
        if in_fence:
            out.append("")
            continue
        out.append(re.sub(r"`[^`\n]*`", lambda m: " " * len(m.group()), line))
    return out


def _paragraphs(masked: list[str]) -> list[list[int]]:
    """Line indices grouped into blank-line-separated paragraphs."""
    groups: list[list[int]] = []
    current: list[int] = []
    for index, line in enumerate(masked):
        if line.strip() == "":
            if current:
                groups.append(current)
                current = []
        else:
            current.append(index)
    if current:
        groups.append(current)
    return groups


def _snippet(line: str, match: re.Match[str] | None = None) -> str:
    raw = (match.group(0) if match is not None else line).strip()
    if not raw:
        raw = line.strip()
    if len(raw) > _SNIPPET_CHARS:
        raw = raw[:_SNIPPET_CHARS].rstrip() + "..."
    return raw.replace("\t", " ").replace("\r", " ")


def _suppressed(line: str, rule: str) -> bool:
    if _DISABLE_ALL_RE.search(line):
        return True
    match = _DISABLE_RE.search(line)
    return bool(match and match.group(1).upper() == rule)


def audit_text(text: str, max_findings: int = _DEFAULT_FINDINGS) -> tuple[list[Finding], dict[str, int]]:
    """Return bounded findings and scan statistics for one piece of prose."""
    masked = _masked_lines(text)
    original = text.splitlines()
    groups = _paragraphs(masked)
    para_of: dict[int, int] = {}
    for para, indices in enumerate(groups):
        for index in indices:
            para_of[index] = para

    strong: list[Finding] = []
    weak: list[Finding] = []

    for rule, title, pattern, hint in _STRONG_RULES:
        for index, line in enumerate(masked):
            match = pattern.search(line)
            if match is None or _suppressed(original[index] if index < len(original) else line, rule):
                continue
            strong.append(Finding(rule, title, index + 1, para_of.get(index, 0), _snippet(original[index], match), hint, "strong"))

    for rule, title, pattern, hint in _WEAK_RULES:
        for index, line in enumerate(masked):
            source = original[index] if index < len(original) else line
            if _suppressed(source, rule):
                continue
            # Weak tells are counted, so every occurrence matters: one
            # em dash in a paragraph means nothing, three mean a pattern.
            for match in list(pattern.finditer(line))[:20]:
                weak.append(Finding(rule, title, index + 1, para_of.get(index, 0), _snippet(source, match), hint, "weak"))

    # Custom line/paragraph rules.
    for index, line in enumerate(masked):
        stripped = line.strip()
        if not stripped:
            continue
        para = para_of.get(index)
        standalone = para is not None and len(groups[para]) == 1
        if standalone and _CLOSER_RE.match(stripped) and len(stripped) <= 80 and not _suppressed(original[index], "P02"):
            strong.append(Finding("P02", "one-line closer", index + 1, para, _snippet(original[index]), "cut it or merge it into a fact", "strong"))

    for para, indices in enumerate(groups):
        lines = [masked[i] for i in indices]

        openings: list[str] = []
        for line in lines:
            for sentence in _SENTENCE_SPLIT_RE.split(line):
                match = _SENTENCE_START_RE.match(sentence.strip())
                if match:
                    openings.append(match.group(1))
        for previous, current in zip(openings, openings[1:]):
            if previous.lower() == current.lower() and previous.lower() not in ("the", "this", "that"):
                strong.append(Finding("P07", "repeated sentence openings", indices[0] + 1, para, _snippet(original[indices[0]]), "merge the sentences or change the subject", "strong"))
                break

        bold_count = sum(len(_BOLD_RE.findall(line)) for line in lines)
        if bold_count >= 3 and not any(_suppressed(original[i], "P19") for i in indices):
            strong.append(Finding("P19", "bold as decoration", indices[0] + 1, para, _snippet(original[indices[0]]), "remove the bold; turn the labeled list into prose", "strong"))

        if sum(len(_HYPHEN_PAIR_RE.findall(line)) for line in lines) >= 3 and not any(_suppressed(original[i], "P10") for i in indices):
            weak.append(Finding("P10", "hyphenated pairs everywhere", indices[0] + 1, para, _snippet(original[indices[0]]), "keep hyphens only where the dictionary has them", "weak"))
        if sum(len(_PASSIVE_RE.findall(line)) for line in lines) >= 3 and not any(_suppressed(original[i], "P11") for i in indices):
            weak.append(Finding("P11", "passive voice", indices[0] + 1, para, _snippet(original[indices[0]]), "name the actor when that helps", "weak"))

    # Headings and their first body line are often in different paragraphs,
    # so this rule scans globally rather than within one group.
    for index, line in enumerate(masked):
        if not original[index].lstrip().startswith("#"):
            continue
        heading = re.sub(r"^#+\s*", "", line).strip().lower()
        words = re.findall(r"[a-z]{4,}", heading)[:4]
        first_body = next((i for i in range(index + 1, len(masked)) if masked[i].strip()), None)
        if words and first_body is not None:
            lead = masked[first_body].lower()
            if all(word in lead for word in words) and not _suppressed(original[first_body], "P24"):
                strong.append(
                    Finding(
                        "P24",
                        "heading restated in first sentence",
                        first_body + 1,
                        para_of.get(first_body, 0),
                        _snippet(original[first_body]),
                        "let the heading do the work",
                        "strong",
                    )
                )

    weak_counts: dict[int, int] = {}
    for finding in weak:
        weak_counts[finding.paragraph] = weak_counts.get(finding.paragraph, 0) + 1
    kept_weak = [f for f in weak if weak_counts.get(f.paragraph, 0) >= _WEAK_THRESHOLD]

    findings = sorted(strong + kept_weak, key=lambda f: (f.line, f.rule))
    findings = findings[:max_findings]
    stats = {
        "lines": len(masked),
        "paragraphs": len(groups),
        "strong_rules": len(_STRONG_RULES) + 4,  # plus P02, P07, P19, P24
        "weak_rules": len(_WEAK_RULES) + 2,
        "suppressed_weak": len(weak) - len(kept_weak),
    }
    return findings, stats


def format_report(findings: list[Finding], stats: dict[str, int]) -> str:
    """Render a bounded, deterministic observation string."""
    if not findings:
        return (
            f"prose_audit: no findings across {stats['paragraphs']} paragraph(s) "
            f"({stats['strong_rules']} strong / {stats['weak_rules']} weak rules scanned)"
        )
    lines = [
        f"prose_audit: {len(findings)} finding(s) across {stats['paragraphs']} paragraph(s) "
        f"({stats['strong_rules']} strong / {stats['weak_rules']} weak rules scanned)"
    ]
    for finding in findings:
        lines.append(
            f"[{finding.rule} {finding.title}] L{finding.line}: "
            f"\"{finding.snippet}\" -> {finding.hint}"
        )
    report = "\n".join(lines)
    if len(report) > _MAX_OUTPUT_CHARS:
        report = report[:_MAX_OUTPUT_CHARS].rstrip() + "\n... [truncated]"
    return report


class ProseAuditTool(Tool):
    """Audit prose for machine-writing tells; advisory only."""

    name = "prose_audit"
    description = (
        "Audit a text file or inline prose for the common tells of "
        "machine-sounding writing. Returns bounded, line-numbered findings "
        "with stable rule ids and fix hints. No score, no rewriting."
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Workspace-relative text file to audit"},
            "text": {"type": "string", "description": "Inline prose to audit (alternative to path)"},
            "max_findings": {"type": "integer", "description": "Maximum findings to return (default 20, max 50)"},
        },
    }

    def execute(
        self,
        sandbox: Sandbox,
        path: str = "",
        text: str = "",
        max_findings: int = _DEFAULT_FINDINGS,
    ) -> str:
        # Direct callers (and alternate front ends) can pass any JSON type;
        # never let that raise out of the tool boundary.
        path = "" if path is None else str(path)
        text = "" if text is None else str(text)
        try:
            budget = max(1, min(int(max_findings), _MAX_FINDINGS))
        except (TypeError, ValueError):
            budget = _DEFAULT_FINDINGS

        if bool(path) == bool(text):
            return "ERROR: give exactly one of path or text"

        if path:
            if "\x00" in path or "\n" in path or "\r" in path:
                return "ERROR: path contains invalid characters"
            if len(path) > 1000:
                return "ERROR: path too long"
            root = getattr(sandbox, "root", None)
            if root is None:
                return "ERROR: sandbox has no workspace root"
            full = os.path.join(root, path)
            try:
                real_root = os.path.realpath(root)
                real_full = os.path.realpath(full)
                if not (real_full == real_root or real_full.startswith(real_root + os.sep)):
                    return "ERROR: path escapes workspace"
                if not os.path.isfile(real_full):
                    return f"ERROR: file not found: {path!r}"
                size = os.path.getsize(real_full)
            except OSError as exc:
                return f"ERROR: cannot resolve {path!r}: {exc}"
            if size > _MAX_FILE_BYTES:
                return f"Note: {path!r} is large ({size} bytes, cap {_MAX_FILE_BYTES})."
            try:
                with open(real_full, "rb") as handle:
                    raw = handle.read(_MAX_FILE_BYTES + 1)
            except OSError as exc:
                return f"ERROR: cannot read {path!r}: {exc}"
            text = raw.decode("utf-8", errors="replace")
        elif len(text) > _MAX_TEXT_CHARS:
            text = text[:_MAX_TEXT_CHARS]

        findings, stats = audit_text(text, budget)
        return format_report(findings, stats)
