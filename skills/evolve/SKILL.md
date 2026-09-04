---
name: evolve
description: Run a closed task-aware harness-evolution loop that diagnoses a task against pass/fail success criteria, proposes a harness change and model lane, and locks regressions into the known-failure registry. Use when a task repeatedly fails, when a harness needs tuning, or when a failure should become a durable regression.
version: 1.0.0
user-invocable: true
---

# Evolve

## Prerequisites

- A project task file (a JSON task with a problem statement) or a saved session transcript; without them only the manual Diagnose/Propose/Lock actions apply.

## Use When

Use when a defined task fails repeatedly, when the harness (prompts, tools, model lane) needs tuning, or when a diagnosed failure should be locked as a durable regression so the harness gets harder to break.

## Procedure

1. The auto pass applies when a project provides a task file and an execution trace (a saved session record); it refreshes the diagnosis without a model call. Re-run the diagnosis manually when the trace is stale.
2. Confirm or create a task file with a task statement and a success-criteria checklist of pass-or-fail lines.
3. Diagnose manually: run the task with the headless entrypoint or in the session, record the per-criterion verdict from the actual output, and score improve-or-regress against the previous run.
4. Propose a recommended harness change (prompt, rules, tools) and a model lane (cheap, mid, or frontier) for the task.
5. Apply the proposed change through the normal verify flow (run the relevant tests/checks).
6. Lock the diagnosed failure as a known-failure entry in `knowledge/known-failures.md` (and a workspace note), and scaffold a regression test.
7. Author the regression-test body and confirm the gate passes.

## Verification

Diagnose must be reproducible from the actual run output. Lock must produce a valid known-failure entry and a regression stub. Durable registry changes remain explicit because they alter project policy.

## Boundaries

Diagnose is evidence-based, not a semantic judge. Lock only scaffolds and registers; the model writes the real test body. Per-turn intra-session model routing is out of scope; the model lane is a per-task recommendation surfaced as the task config's model input.
