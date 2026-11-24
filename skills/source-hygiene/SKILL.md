---
name: source-hygiene
description: Scan documents for claims without supporting evidence (unsourced superlatives, unverifiable absolutes, statistics without citation, proxy authority, placeholders, broken local references), verify each finding, and either replace it with a verified fact or mark it needs-evidence.
version: 1.0.0
user-invocable: true
---

# Source Hygiene

## Use When

Use when a document asserts things it does not back up: marketing-heavy
phrasing, numbers without a source, appeals to unnamed authorities,
placeholders, or references to files that do not exist. Works across docs,
READMEs, skill files, changelogs, and any prose that should be
evidence-backed.

## Procedure

1. Run the `claim_audit` tool on the target files (default: `README.MD`,
   `docs/**/*.md`, and `skills/**/SKILL.md`). The tool returns line-numbered
   findings with stable rule ids, advisory only.
2. Treat every finding as a suspicion, not a verdict. Before calling a
   claim unsupported, probe it:
   - For a broken local reference, confirm the path really does not exist.
   - For a statistic or superlative, look for the source in the workspace
     memory/fact ledger, the codebase, or the cited report; a claim with a
     real source is confirmed and needs only a citation added.
   - Label each finding CONFIRMED (probe failed), SUSPECTED (ambiguous),
     or UNVERIFIED (could not check).
3. For CONFIRMED claims, rewrite the sentence so it states only what the
   evidence supports, keeping the surrounding style (see the `humanize`
   skill when the rewrite needs a natural voice). Never invent a
   replacement fact, source, or number.
4. When no verified replacement exists, keep the text unchanged and mark
   the finding `needs evidence` in the report.
5. Write the report to `<workspace>/.mantra/source-hygiene.md` (or the repo
   `docs/` when auditing the repo itself) with a table: Finding | Evidence |
   Status | Action.
6. Re-run `claim_audit` and confirm the remaining findings are either
   resolved or intentional (documented in the report).

## Verification

Every claim labeled unsupported has a failed probe; no fact, source, or
number was invented; the report exists and lists each finding's status.
The re-run shows no new CONFIRMED findings.

## Boundaries

Read-and-report first: fixes happen only for CONFIRMED findings and only
when a verified replacement exists. Do not touch operational ledgers
(memory, known-failure registry, sessions). Do not use detector scores or
ratings as a definition of success.
