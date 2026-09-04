---
name: deploy-parity
description: Copy changed assets (skills, rules, scripts, knowledge) to a target directory or backup backup-first, with SHA-256 parity proof and a recorded rollback path.
version: 1.0.0
user-invocable: true
---

# Deploy Parity

## Prerequisites

- An explicit operator directive for this deploy; deployment mutates target state.
- A baseline check run before any file moves.

## Use When

Use when changed repo assets (skills, rules, scripts, knowledge files) must reach
another location - a personal `~/.mantra` tree, a backup, or a checked-out
installation - so the other copy picks them up.

## Procedure

1. Baseline first: run the project's check (e.g. `pytest -q` or the repo's
   configured suite); non-zero exit means unhealthy - stop.
2. Backup first: create a timestamped backup of every file the deploy will touch,
   including a manifest with per-file hashes. A failed backup aborts the deploy.
3. Compute the change set and SHA-256 both sides; never deploy files whose
   repository state you have not verified (parse check for scripts, frontmatter
   check for skills).
4. Copy, then prove parity: re-hash both sides and require checked = matched =
   expected with mismatches = 0 and missing = 0 before reporting success.
5. Validate behavior: run the focused checks for deployed files against the
   deployed copies, not only the repository tree (e.g. `skills.load_all()` parses
   every copied SKILL.md).
6. Record the rollback path: the backup directory name and manifest in the report.

## Verification

The parity manifest (checked/matched/mismatched/missing counts) and the
focused-check results are the completion evidence; a deploy without both is
unverified.

## Boundaries

Requires explicit operator approval; it is never part of an unrequested cleanup.
Never restore over unrelated operator work, and treat a parity mismatch as a
failed deploy even when files copied.