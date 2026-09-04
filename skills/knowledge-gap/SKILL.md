---
name: knowledge-gap
description: Find documentation gaps between shipped code and docs (missing/stale/hard-to-find/needs-triage) with evidence.
version: 1.0.0
user-invocable: true
---

# Knowledge Gap

## Use When
Docs lag behind the code. Need to compare the actual change history vs customer/internal docs and surface missing/stale/hard-to-find/needs-triage with evidence.

## Procedure
1. Collect evidence: `git log --oneline --since="last tag"` or a stated window +
   `git diff --stat`, plus `CHANGELOG.md`/README when present. Collect docs
   inventory: `docs/**/*.md`, `skills/**/SKILL.md`, and `AGENTS.md` /
   `<workspace>/.mantra/memory.md` when present.
2. Classify each shipped change vs docs: `missing` (no doc), `stale` (doc exists
   but outdated), `hard-to-find` (exists but undiscoverable), `needs-triage`
   (uncertain).
3. Write the report to `<workspace>/.mantra/knowledge-gaps.md` (or the repo
   `docs/` when auditing the repo itself) with a table: Finding | Severity |
   Evidence (file:line, commit) | Checked Pages | Ready Prompt.
4. For each finding, generate a copy-ready prompt for the agent: `Update {doc} to
   reflect {change} in {evidence} - keep style of the surrounding docs`. Optionally
   attach to `<workspace>/.mantra/memory.md`.
5. Verify: at least one evidence-backed finding per shipped change, no
   hallucinated files, prompts are self-contained.

## Verification
Gaps reference real git commits and real doc paths. The report exists and is not
empty when drift exists.

## Boundaries
Read-only analysis only. Do not auto-edit docs without approval. Keep prompts
within token budget.