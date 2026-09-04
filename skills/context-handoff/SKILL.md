---
name: context-handoff
description: Manage the context budget across compaction - watch capacity signals, offload volatile state to durable files (workspace memory, the standing goal), write a continuation brief before compacting, and rebuild intent afterwards.
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
2. Offload before pressure becomes compaction: record durable state through the
   workspace memory ledger (`<workspace>/.mantra/memory.md`), set or refresh the
   standing goal (`/goal <text>`), and keep the session todo list (`/todo`) as the
   checklist of what remains. Anything needed later must live on disk, not in
   conversation memory.
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

Confirm every item needed to resume exists on disk (memory entries, brief, todo
list) rather than in transcript memory. Confirm the first post-compaction edits
follow the read-before-edit rule (the file tools enforce it and reject edits
without a prior read).

## Boundaries

Offloading never means duplicating: patch existing durable entries instead of
appending copies. The host owns compaction itself; this skill manages what
survives it. Never store secrets in briefs or notes.