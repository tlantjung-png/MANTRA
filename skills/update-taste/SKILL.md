---
name: update-taste
description: Maintain concise durable coding and documentation conventions in the repo (knowledge/known-failures.md and AGENTS.md) without inventing one-off preferences.
version: 1.0.0
user-invocable: true
---

# Update Taste

## Use When

Use when recurring naming, formatting, architecture, testing, repository, or
environment conventions emerge at a meaningful checkpoint.

## Procedure

1. Inspect the current code, recent changes, and project rules (AGENTS.md,
   CLAUDE.md, `.mantra-instructions.md` if present) to see what conventions are
   already documented.
2. Keep only stable evidence-backed conventions that would prevent a future
   mistake.
3. Organize rules under language and framework, naming and structure, formatting,
   preferred patterns, and things to avoid.
4. Remove conflicts, preserve still-valid history, and record the rule in the
   appropriate durable place: conventions the agent must follow belong in the
   workspace memory (`<workspace>/.mantra/memory.md`) or an AGENTS.md; recurring
   failure classes belong in the known-failure registry
   (`knowledge/known-failures.md`).
5. Keep the file short enough to be used as an active rule set.
6. Record why the convention exists (the mistake it prevents) so a future rewrite
   can tell a rule from a one-off preference.

## Verification

Every rule must be evidenced by the repository or an explicit durable decision.
Apply the prune test: if removing a line would not cause a mistake, remove it.

## Boundaries

Do not record project state, detailed API documentation, or a one-off request as a
permanent style rule. Avoid trivial mid-task edits.