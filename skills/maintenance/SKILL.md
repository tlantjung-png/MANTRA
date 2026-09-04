---
name: maintenance
description: Run the periodic health pass - test suite, git state, known-failure probing, session log review - and write one dated report.
version: 1.0.0
user-invocable: true
---

# Maintenance

## Prerequisites

- A checkout of the repo is present; checks run against the working tree.
- Suitable for unattended scheduling (pair with the `unattended` skill when
  registering a cron/headless wrapper).

## Use When

Use on a periodic cadence, before long absences, or whenever health drift is
suspected between full audits.

## Procedure

1. Baseline: run the project's test suite (`pytest -q` or the repo's configured
   check) and `git status`/`git diff --check`; stop on failure.
2. Integrity: run `python scripts/vault-verify.py` against the state directory to
   confirm the `.mantra/vault` chain is clean and fresh.
3. Registry: probe the known-failure classes in `knowledge/known-failures.md`
   (see the known-failures skill); record each as CONFIRMED / NOT REPRODUCED /
   UNVERIFIED.
4. Sessions/logs: scan the session store (`~/.mantra/sessions/`) and the console
   logs (`logs/*.jsonl`) for repeated failures, stalls, or errors worth noting
   (see the analytics skill for the deeper pass).
5. Write everything to `<workspace>/.mantra/logs/maintenance-<yyyy-MM-dd>.log`
   (or the repo `logs/` directory) with a one-line verdict: PASS, PASS-with-notes,
   or FAIL plus the failing step.

## Verification

The dated report file exists with all sections populated and a verdict; any FAIL
names the exact failing check and its output.

## Boundaries

Read-and-report only - no repairs, no deletes, no config changes. Repairs route
through their own skills (known-failures, diagnostics, debug).