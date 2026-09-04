---
name: mine-sessions
description: Mine recent session traces and console logs, reduce cross-session patterns into weighted candidates, and route durable knowledge, repeated workflows, failure trends, and operational candidates without copying secrets.
version: 2.0.0
user-invocable: true
---

# Mine Sessions

## Use When

Use at checkpoints, during periodic maintenance, after a major phase, or before
deciding whether a repeated workflow deserves a skill.

## Procedure

1. Generate a bounded report over a stated time window and session limit from the
   session store (`~/.mantra/sessions/`) and console logs (`logs/*.jsonl`).
2. Inspect summaries, tokens/totals, tool frequencies, repeated tool sequences,
   failures, and log trends.
3. Collect raw candidates from every session, then run the **Reduce** phase below
   to produce a cross-session weighted candidate set.
4. Where a turn ran a workflow (`/workflow`), note workflow names and step
   outcomes using the saved session and workflow file (`~/.mantra/workflows.json`).
5. Route durable facts to memory, style lessons to the repo conventions, repeated
   workflows to /workflow or a new skill through create-skill, repeated denies to
   rules review, and recurring failures to the known-failure registry.
6. Run the relevant verification and baseline checks after any follow-up change.

## Verification

Confirm that the report was written to the requested destination. Treat a failed
output write as a failure, not a successful stdout fallback. Verify that
cross-session candidates are weighted by actual occurrence count, not by how
recently they appeared. A pattern seen once yesterday is not more important than a
pattern seen ten times last week.

## Reduce

Sessions are independent observations of the same behavior. The same pattern
appearing across multiple sessions is a systemic signal - but only if you can see
it across sessions. Raw per-session reports bury cross-session patterns under
session-specific noise.

### Step 1 - Validate

- Confirm each session's store record is complete and readable. A truncated or
  corrupted session cannot contribute findings - log it as skipped.
- Verify that timestamps fall within the stated time window.
- Drop sessions with no actionable content (empty turns, pure setup, cancelled
  before work began).

### Step 2 - Normalize

- Strip session-specific identifiers (session ids, timestamps, paths that vary per
  session) to expose the underlying pattern.
- Normalize tool names, error messages, and workflow labels to canonical forms.
- Convert temporal references to relative offsets (turn number, phase position).

### Step 3 - Group

- Collapse the same pattern observed in multiple sessions into one candidate.
- Weight each group by occurrence count: a failure in 8 of 10 sessions is
  systemic; one in 1 of 10 may be environmental.
- Keep session metadata attached to each group so the candidate can be traced back
  to source.

### Step 4 - Surface Structure

- **Recurrence**: Patterns appearing across many sessions are high-priority
  candidates for skill creation, memory, or debugging.
- **Contradiction**: Where one session reports a pattern as active and another
  shows it was fixed, flag the contradiction - an incomplete fix or an
  environment-dependent pattern.
- **Isolation**: Patterns found in only one session may be noise, early signals,
  or a session-specific configuration difference. Tag as single-occurrence.
- **Rejection log**: any session that could not be mined, with the reason.

### Step 5 - Deliver

1. **Weighted candidate set** - each candidate tagged with occurrence count,
   session span, and confidence tier (systemic / emerging / isolated).
2. **Contradiction report** - any pattern with conflicting status across sessions.
3. **Rejection log** - sessions skipped and why.

## Boundaries

Never mine credentials, raw secrets, or private command arguments. One occurrence
is not enough evidence to create a skill or change a rule.