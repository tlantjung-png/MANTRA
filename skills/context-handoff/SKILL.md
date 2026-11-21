---
name: context-handoff
description: Preserve task context across compaction by recording verified state in workspace memory or a continuation brief, then reconstructing it before resuming.
version: 1.0.0
user-invocable: true
---

# Context Handoff

## Use When

Use when the session is approaching its context cap, before deliberately ending a
long phase, after any `/compact`, or when resuming work whose reasoning lives only
in a transcript.

## Procedure

1. Watch the signals: the console shows `CTX <tokens>` in the per-turn footer and
   the `/cost` dashboard. A steady climb toward the window cap means a compaction
   is coming.
2. Before compaction, record durable facts in the workspace memory ledger
   (`<workspace>/.mantra/memory.md`) and write the remaining actions in a
   continuation brief. Do not rely on a chat-only checklist to survive a new
   session.
3. Write a continuation brief before a known stop or `/compact`: goal in one line,
   what is done with evidence pointers (files, commits, test names), exact next
   actions, open risks, and the paths that own the state. Keep it in
   `<workspace>/.mantra/` (e.g. `continuation.md`) so a later session can find it.
4. Rebuild after compaction: re-read the memory ledger, the continuation brief,
   and the session record; re-read source files before editing them (the read
   ledger is stale by definition across a compaction boundary).
5. Distrust surviving artifacts of the pre-compaction turn: claims remembered
   "from earlier" must be re-verified against source before use.
6. Resume work only when reconstruction is verified: restate the current subgoal
   and its evidence pointers before the first post-compaction action.

## Verification

Confirm every item needed to resume exists on disk (memory entries and brief)
rather than in transcript memory. Confirm the first post-compaction edits
follow the read-before-edit rule (the file tools enforce it and reject edits
without a prior read).

## Boundaries

Offloading never means duplicating: patch existing durable entries instead of
appending copies. The host owns compaction itself; this skill manages what
survives it. Never store secrets in briefs or notes.
