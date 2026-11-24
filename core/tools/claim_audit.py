"""Deterministic claim audit: find unsupported assertions in text.

Sibling of ``prose_audit`` for the ``source-hygiene`` skill: plain
regexes, no model call, no network, no dependency, and no score. Findings
are advisory input for a writer, not a gate.

Rules are deliberately narrow so ordinary prose is not nagged. Evaluative
words are flagged only when they read as claims with no evidence: bare
superlatives, unverifiable absolutes, statistics without a citation,
proxy authority, placeholders, and broken local references. A rule can be
suppressed on a line with a comment, the same way ``prose_audit`` rules
are suppressed. The broken-reference rule only runs when a workspace root
is available, so inline-text audits stay dependency-free.
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
        "C01",
        "unsourced superlative",
        re.compile(
            r"\b(?:the best|top-rated|best-in-class|world-class|second to none|"
            r"unmatched|unrivaled|unrivalled|market-leading|industry-leading|"
            r"leading-edge|number one)\b",
            re.I,
        ),
        "name the basis or drop the superlative",
    ),
    (
        "C02",
        "unverifiable absolute",
        re.compile(
            r"\b(?:guaranteed|undoubtedly|without a doubt|without question|"
            r"always works|never fails|works every time|perfectly)\b",
            re.I,
        ),
        "state the specific condition or the measured result",
    ),
    (
        "C04",
        "vanity claim",
        re.compile(
            r"\b(?:revolutionary|groundbreaking|unprecedented|game-?changing|"
            r"industry-first|first-ever|effortless|flawless|miracle|magical)\b",
            re.I,
        ),
        "keep the fact and drop the claim",
    ),
    (
        "C05",
        "proxy authority",
        re.compile(
            r"\b(?:studies show|research shows|research suggests|experts say|"
            r"scientists say|many say|some say|people say|"
            r"it is widely known|widely believed|as everyone knows|as you know)\b",
            re.I,
        ),
        "name the source or cut the appeal",
    ),
    (
        "C06",
        "placeholder content",
        re.compile(
            r"(?i:\blorem ipsum\b|\bcoming soon\b|\bTBD\b|\bFIXME\b|"
            r"\bplaceholder\b|\bchange me\b)|\bTODO\b(?!\s+(?:ADD|DONE)\b)"
        ),
        "write the real content or remove the placeholder",
    ),
)

# Weak tells: only reported when at least three land in one paragraph.
_WEAK_RULES: tuple[tuple[str, str, re.Pattern[str], str], ...] = (
    (
        "W01",
        "vague magnitude",
        re.compile(
            r"\b(?:significantly|far|much|way|huge|big|vastly|twice)\s+"
            r"(?:better|faster|stronger|improved|higher|lower|more|less|superior)\b",
            re.I,
        ),
        "give the measured quantity or the comparison",
    ),
    (
        "W02",
        "unsupported typicality",
        re.compile(
            r"\b(?:industry standard|standard practice|best practice|"
            r"commonly used|widely used|popular choice|generally accepted)\b",
            re.I,
        ),
        "cite the practice or name who uses it",
    ),
    (
        "W03",
        "appeal to common knowledge",
        re.compile(
            r"\b(?:needless to say|of course|obviously|clearly|plainly)\b",
            re.I,
        ),
        "keep the statement and drop the appeal",
    ),
)

_CITATION_RE = re.compile(
    r"(?:according to|per\s+\w+|source|report|study|benchmark|survey|"
    r"\bdata\b|figure|table|see\s|ref\.|\[\s*\d+\s*\])",
    re.I,
)
_STATISTIC_RE = re.compile(
    r"\d+(?:\.\d+)?\s*(?:%|x|\u00d7)(?![a-z0-9])|\d+\s*times\b",
    re.I,
)

_DISABLE_RE = re.compile(r"claim-audit-disable\s+([CW]\d+)", re.I)
_DISABLE_ALL_RE = re.compile(r"claim-audit-disable-all", re.I)
_FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})")

_LINK_PATH_RE = re.compile(r"\]\(\s*([^)#\s]+)")
_FILE_LIKE_RE = re.compile(r"\.\w{1,5}$")
_NON_PATH_RE = re.compile(r"(?:://|^#|^\s*$|[*?{}<>|\"'])")
_LEADING_DOT_RE = re.compile(r"^\./")


def _masked_lines(text: str) -> list[str]:
    """Blank frontmatter, fenced code, and inline code; keep line numbers."""
    lines = text.splitlines()
    out: list[str] = []
    in_frontmatter = bool(lines) and lines[0].strip() == "---"
    in_fence = False
    for line in lines:
        if in_frontmatter:
            out.append("")
            if line is not lines[0] and line.strip() in ("---", "..."):
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


def _candidate_paths(line: str) -> list[str]:
    """Markdown link targets from a line, deduplicated in order.

    Backtick tokens are not candidates: they often name commands, module
    invocations, or build outputs rather than documents, so flagging
    them as broken references produces noise. A markdown link is an
    explicit reference and is the useful signal.
    """
    seen: list[str] = []
    for match in _LINK_PATH_RE.finditer(line) or []:
        candidate = match.group(1)
        if candidate and not _NON_PATH_RE.search(candidate) and _FILE_LIKE_RE.search(candidate):
            seen.append(candidate)
    out: list[str] = []
    for candidate in seen:
        if candidate not in out:
            out.append(candidate)
    return out


def _resolve_relative(root: str, candidate: str) -> str:
    candidate = _LEADING_DOT_RE.sub("", candidate)
    candidate = candidate.split("#", 1)[0]
    return os.path.join(root, candidate)


def audit_text(
    text: str,
    max_findings: int = _DEFAULT_FINDINGS,
    root: str | None = None,
) -> tuple[list[Finding], dict[str, int]]:
    """Return bounded findings and scan statistics for one claim audit."""
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
            for match in list(pattern.finditer(line))[:20]:
                weak.append(Finding(rule, title, index + 1, para_of.get(index, 0), _snippet(source, match), hint, "weak"))

    # Statistics without a citation: sentence-scoped.
    for index, line in enumerate(masked):
        source = original[index] if index < len(original) else line
        if _suppressed(source, "C03"):
            continue
        matches = list(_STATISTIC_RE.finditer(line))
        if not matches:
            continue
        if _CITATION_RE.search(line) is not None:
            continue
        for match in matches[:5]:
            strong.append(Finding("C03", "unsourced statistic", index + 1, para_of.get(index, 0), _snippet(source, match), "give the source of the number", "strong"))

    # Broken local references: only when a workspace root is available.
    path_rules = 0
    if root is not None:
        path_rules = 1
        for index, line in enumerate(masked):
            source = original[index] if index < len(original) else line
            if _suppressed(source, "C07"):
                continue
            for candidate in _candidate_paths(source):
                full = _resolve_relative(root, candidate)
                try:
                    missing = not os.path.exists(full)
                except OSError:
                    missing = True
                if missing:
                    strong.append(Finding("C07", "broken local reference", index + 1, para_of.get(index, 0), _snippet(source), "fix the path or remove the reference", "strong"))
                    break  # one finding per line

    weak_counts: dict[int, int] = {}
    for finding in weak:
        weak_counts[finding.paragraph] = weak_counts.get(finding.paragraph, 0) + 1
    kept_weak = [f for f in weak if weak_counts.get(f.paragraph, 0) >= _WEAK_THRESHOLD]

    findings = sorted(strong + kept_weak, key=lambda f: (f.line, f.rule))
    findings = findings[:max_findings]
    stats = {
        "lines": len(masked),
        "paragraphs": len(groups),
        "strong_rules": len(_STRONG_RULES) + 2,  # plus C03, C07
        "weak_rules": len(_WEAK_RULES),
        "path_rules": path_rules,
        "suppressed_weak": len(weak) - len(kept_weak),
    }
    return findings, stats


def format_report(findings: list[Finding], stats: dict[str, int]) -> str:
    """Render a bounded, deterministic observation string."""
    path_note = f" ({stats['path_rules']} path rule)" if stats["path_rules"] else " (no workspace root: broken-reference rule skipped)"
    if not findings:
        return (
            f"claim_audit: no findings across {stats['paragraphs']} paragraph(s) "
            f"({stats['strong_rules']} strong / {stats['weak_rules']} weak rules scanned)"
            f"{path_note}"
        )
    lines = [
        f"claim_audit: {len(findings)} finding(s) across {stats['paragraphs']} paragraph(s) "
        f"({stats['strong_rules']} strong / {stats['weak_rules']} weak rules scanned)"
        f"{path_note}"
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


class ClaimAuditTool(Tool):
    """Audit text for unsupported claims; advisory only."""

    name = "claim_audit"
    description = (
        "Audit a text file or inline prose for claims without supporting "
        "evidence: unsourced superlatives, unverifiable absolutes, "
        "statistics without citation, proxy authority, placeholders, and "
        "broken local references. Returns bounded, line-numbered findings "
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
        path = "" if path is None else str(path)
        text = "" if text is None else str(text)
        try:
            budget = max(1, min(int(max_findings), _MAX_FINDINGS))
        except (TypeError, ValueError):
            budget = _DEFAULT_FINDINGS

        if bool(path) == bool(text):
            return "ERROR: give exactly one of path or text"

        root = None
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

        findings, stats = audit_text(text, budget, root=root)
        return format_report(findings, stats)
