---
name: instruction-audit
description: Audit always-loaded instruction assets - the base system prompt, workspace memory, AGENTS.md instructions, the known-failure registry, and command rules - for token weight, staleness, contradiction, and attention dilution.
version: 1.0.0
user-invocable: true
---

# Instruction Audit

## Prerequisites

- No external helpers are required; use word-count heuristics for token weight.

## Use When

Use when injected instructions feel ignored, after several rules accumulated,
before large prompt-asset changes, or periodically as maintenance.

## Procedure

1. Enumerate what every session actually loads: the base system prompt
   (`DEFAULT_SYSTEM_PROMPT`), the workspace memory (`<workspace>/.mantra/memory.md`),
   instructions files (AGENTS.md, CLAUDE.md, `.mantra-instructions.md`), the
   known-failure registry (`knowledge/known-failures.md`), and the command rules
   (`rules/commands.rules` plus `~/.mantra/rules/commands.rules`).
2. Measure weight: estimate per-file token cost (roughly 4 chars/token) and report
   the total always-loaded budget; growth here taxes every turn.
3. Scan for contradiction: memory entries versus current code, known-failure rules
   versus current behavior, rules versus reality. Any count or claim that source
   no longer supports is documentation drift - and inside instruction assets it is
   loaded as truth.
4. Apply the attention lesson: injected instructions compete with per-turn repo
   content, so keep the standing goal and todo list pointer-sized; a rule nobody
   attends to costs tokens without changing behavior.
5. Apply the prune test to every rule: if removing it would not cause a concrete
   mistake, propose removal. Historical narration moves to memory or the
   CHANGELOG.
6. Route changes through the owning flow - update-memory for memory entries,
   known-failures for failure classes, rules edits for approvals - and verify with
   the relevant focused suites.

## Verification

Report per-file weights before/after, each contradiction found with exact paths
and lines, and each removal with the evidence that it was safe.

## Boundaries

Instruction assets are policy: changes beyond pruning verified-stale content need
an operator decision. Do not merge distinct rules into vague prose to save tokens;
a short wrong rule is worse than a precise long one.