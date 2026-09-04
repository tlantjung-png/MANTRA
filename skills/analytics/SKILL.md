---
name: analytics
description: Debug console and agent-loop issues by reducing the repo's JSONL event logs and per-session signals into one correlated, prioritized report of bugs, failures, anomalies, per-tool metrics, and token usage. Use to find where a feature is failing or to audit health.
version: 2.0.0
user-invocable: true
---

# Analytics (Deep Debugging)

## Use When

When something is misbehaving and you need to find where: a failing tool, a slow
operation, a recurring warning, or a broad health audit. Use the console's own
signals - `/cost` for token usage, `/tools` for the surface, per-turn footers for
step/context sizes - plus the log files, and correlate the evidence into one
report.

## Data Sources

- `logs/mantra-run.jsonl` and `logs/mantra-console.jsonl` record session events,
  tool calls, inference, and token usage (JSONL, one event per line).
- `~/.mantra/sessions/*.json` hold saved conversations (one per resumed session)
  with totals and message history.

## Procedure

1. Collect the raw signals: read the relevant log files and any saved session
   files for the window in question.
2. Group events by tool, model, or phase; note failures, repeated tool calls,
   stops (`stopped_reason`), and token totals per turn (the per-turn footer shows
   `I/O` and `CTX`; `/cost` shows session totals).
3. Correlate across events: a tool that fails repeatedly plus the surrounding
   session context plus the failure line in the log are one finding seen through
   several lenses - group them.
4. Normalize and reduce: strip session-specific noise, weight recurring patterns
   by occurrence count, and rank by evidence breadth.
5. Deliver a prioritized finding set with exact tool names, counts, paths, and the
   number of independent events confirming each finding. Do not invent a failure
   the logs do not show.

## Verification

A finding is confirmed only from correlated evidence across multiple log events.
A single-category finding is suspect until confirmed by a second event. Report
exact counts and contexts.

## Boundaries

Log analysis is read-only. It does not fix anything, restart MANTRA, call
providers, or mutate configuration. Use its evidence to localize the bug, then
route the fix through the appropriate skill (debug, diagnostics, or a focused
regression).