---
name: recall
description: Retrieve targeted knowledge from workspace memory, the known-failure registry, session records, and the skill catalog. Use at task start, before decisions, after long sessions, or when a subagent left durable context.
version: 1.0.0
user-invocable: true
---

# Recall

## Use When

Use at the start of a task that may depend on prior lessons, before a decision
that earlier work touched, after a long session, or when a subagent should
surface durable context it left behind.

## Procedure

1. Read the sources directly with the file and search tools - `search_code` for
   keywords or grep-style questions, `read_file` for exact context.
2. Scope the search to a source: workspace memory
   (`<workspace>/.mantra/memory.md`), the known-failure registry
   (`knowledge/known-failures.md`), saved sessions (`~/.mantra/sessions/`), the
   skill catalog (`/skills show <name>` or `skills/**/SKILL.md`), or the session
   JSONL logs.
3. To isolate a class of knowledge, filter by kind: failure, decision, fact, note,
   or procedure (memory sections, KF entries, transcript roles).
4. Read the returned source and section for each hit, then open the original file
   when the snippet is not enough.
5. Apply the recalled context to the current work, patching existing memory rather
   than duplicating it.
6. If a subagent needs to leave context for a later session, append it to the
   workspace memory with a date.

## The sources

There is no separate derived index: the ledgers ARE the store. Memory entries and
KF entries are the typed knowledge units; session transcripts and logs are the
history. Read them directly - keyword search over the real files beats any
precomputed digest for accuracy.

After a compaction, pair this skill with context-handoff: recall supplies evidence
pointers from the ledgers, the continuation brief supplies intent and next
actions. Neither alone rebuilds the working state.

## Verification

Confirm the recall result comes from a real source and section, and that the
snippet and type are accurate. Do not treat a keyword hit as proof of intent; open
the source before relying on it.

## Boundaries

Recall is deterministic and keyword-based, not semantic or embedding-based. It
does not establish facts by itself; it points at evidence. Never store secrets in
memory. Do not use a single hit as enough evidence to change a durable rule.