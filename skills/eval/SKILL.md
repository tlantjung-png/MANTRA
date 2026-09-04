---
name: eval
description: Measure agent-loop quality with the repo's test suite and session logs - validate gates, classify blast radius, and report cost per accepted change.
version: 1.0.0
user-invocable: true
---

# Eval

## Prerequisites

- The repo's tests must be runnable (`pytest`) for gate validation; session logs
  (`logs/*.jsonl` and `~/.mantra/sessions/`) must exist for telemetry work.

## Use When

Use after harness or prompt changes, before trusting a verification gate, when
loops seem wasteful, or when a recurring task should become a measurable
regression fixture.

## Procedure

1. Fixtures from history: mine a stated time window of session logs and saved
   sessions to turn real working and broken behavior into test fixtures; review
   each fixture's expected outcome before promoting it.
2. Trajectory review: review a session's path from its saved transcript; treat
   exact loops, redundant reads, and context inflation as cost findings even when
   the final answer was correct.
3. Gate validation: before relying on any verification gate (a test, a check, a
   script), prove it is meaningful - it must pass in a clean fixture workspace and
   fail in a deliberately broken one.
4. Blast-radius classification: sort a proposed change into its undo-cost lane
   (tiny, contained, wide) from the actual file set it touches; deterministic
   checks carry the verdict, not opinions.
5. Economics: report token cost per accepted change after loop work (`/cost` plus
   the per-turn footers) so automation pays for itself visibly.
6. Route confirmed regressions into the known-failure registry through the
   known-failures skill; route systemic trajectory waste into instruction or
   prompt changes through update-taste.

## Verification

Report per-item results with exact counts, the window used, and any unavailable
telemetry labeled unverified. A gate without a proven failure mode stays
unvalidated regardless of green output.

## Boundaries

Evals measure; they never auto-promote a harness change. Do not tune prompts or
rules to pass an eval without locking the underlying regression. Do not fabricate
baseline numbers when telemetry is absent - record the gap instead.