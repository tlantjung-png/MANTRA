---
name: changelog
description: Write user-facing release notes from verified repository history, clearly separating current, planned, changed, fixed, and removed behavior.
version: 1.0.0
user-invocable: true
---

# Changelog

## Use When

Use for release notes, changelog entries, version summaries, or user-facing change communication.

## Procedure

1. Establish the release boundary from actual history and the current diff.
2. Read the changed behavior and identify user impact, operational impact, breaking changes, and removed features.
3. Group notes under the repository's existing headings and use behavior-focused language.
4. Mark historical facts as historical and planned work as planned.
5. Ask before publishing a user-facing release record when the request does not explicitly authorize it.

## Verification

Every note must be traceable to a commit, source file, or verified operational result. Do not claim a live or tested behavior that was not checked.

## Boundaries

Do not invent metrics, compatibility, pricing, or support claims. Invisible refactors belong only when they affect operators or are part of the repository's established format.
