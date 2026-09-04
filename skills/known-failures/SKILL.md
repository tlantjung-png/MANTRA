---
name: known-failures
description: Maintain and re-probe the known-failure registry (knowledge/known-failures.md, injected into the session prompt) so previously fixed incident classes cannot silently return in sibling paths.
version: 1.0.0
user-invocable: true
---

# Known Failures

## Prerequisites

- Decide which registry is authoritative before probing: the repo's
  `knowledge/known-failures.md` (shipped, injected into every session prompt)
  versus a workspace copy under `<workspace>/.mantra/` for project-local classes.

## Use When

Use before an audit or code review, after fixing a bug, and at meaningful checkpoints.

## Procedure

1. Identify the authoritative registry: the repo's `knowledge/known-failures.md`
   for repository work; a workspace `.mantra/memory.md` note or a workspace-level
   registry only for project-local classes. Never silently substitute one for the
   other.
2. Read the registry and check each entry's format: `## KF-N | short title` with
   `symptom`, `rule`, and `date` fields. Malformed entries are not evidence.
3. Re-probe each entry using its rule. Run the named focused suite, test, or
   command when one resolves on disk; for manual or unavailable probes, perform a
   bounded source/runtime check and label the result **CONFIRMED**,
   **NOT REPRODUCED**, or **UNVERIFIED**.
4. Treat a newly reproduced instance of an existing class as a high-severity
   regression. Report the KF identifier, exact path, input/evidence, and failed
   probe; do not downgrade it to a new generic bug.
5. Add a class only after a reproducible failure has a concrete symptom, the
   behavior rule that prevents it, and a probe (test, command, or manual step).
   Keep entries short and machine-parseable; the registry is injected into the
   prompt, so every line costs context.
6. Keep fixed classes for sibling-path checks, but update their evidence when a
   later probe changes the status. Structural validation alone never proves
   runtime behavior.

## Verification

The registry still parses (every entry has title, symptom, rule, date), the
relevant focused probes pass, and `git diff --check` is clean.

## Boundaries

The registry is evidence, not ceremony. Do not delete a class because it is
fixed, and do not report a new class without a successful probe or source path.