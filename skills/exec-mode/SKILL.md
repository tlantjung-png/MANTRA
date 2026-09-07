---
name: exec-mode
description: Recommend whether a task should run in a regular session, as a MANTRA workflow, or as a goal, using a deterministic signal table, and log the decision to the workspace memory.
version: 1.0.0
user-invocable: true
---

# Exec Mode

## Use When

Use at the start of a task when the execution shape matters: parallel fan-out, an
open-ended objective, or a quick single deliverable. The operator remains the
decision maker; this skill only recommends.

## Procedure

1. Read the task text, or the task file when one exists.
2. Apply the signal table below to pick a mode, and state the matched signals.
3. Apply the recommended mode: `/workflow` for a structured multi-step sequence,
   `/goal` for an open-ended objective, or a regular session for a quick single
   deliverable.
4. When the operator overrides the recommendation, keep your reasons and note the
   override.
5. Log the decision to the workspace memory (`<workspace>/.mantra/memory.md`):
   mode, matched signals, confidence, and override if any.

## Signal Table

A workflow is indicated by defined multi-step structure, parallel fan-out,
repeatable steps, and verify chains. A goal is indicated by an open-ended
objective such as fix all, make everything pass, or remediate all findings. A
regular session is indicated by a single deliverable, a quick task, an explanation
or comparison, a small diff, or ambiguity. Precedence is workflow, then goal, then
regular; no signal or a tie means the regular default with a low-confidence flag.

## Cost Lane

For long-running loops, keep token economics in mind: `/cost` shows session usage,
and the per-turn footer shows I/O per turn. Reserve expensive frontier lanes for
genuinely hard reasoning, use cheap lanes for bounded mechanical work, and
sanity-check the cost-per-accepted-change ratio before recommending an automation
loop.

## Verification

The recommendation must name a mode, a confidence score, and the matched signals.
The decision log entry must exist in memory after the choice.

## Boundaries

Recommendation only. Workflows launch via `/workflow`; goals via `/goal`; a
one-shot headless run via `mantra-headless --config <cfg> --task <file>`
with the console's `--config`/`--task` interface. Never auto-launch a workflow or
goal without the operator running it.