---
name: diff-review
description: Inspect a working-tree diff hunk by hunk and selectively accept or reject changes using the console's /diff and file tools without mutating unread content.
version: 1.0.0
user-invocable: true
---

# Diff Review

## Use When

Use for mixed changes, selective cleanup, discarded hunks, or a request to review
the working-tree diff in detail.

## Procedure

1. Confirm the repository, current status (`git status`), and whether the request
   is review-only.
2. Read the complete diff (`/diff` shows uncommitted changes; `git diff` for the
   raw form) and its surrounding source before making a decision.
3. Enumerate hunks: the console renders diffs as paged old/new panes with a
   per-file chip; page through them with an empty Enter.
4. Classify each hunk as intended, unrelated, unsafe, or requiring clarification.
5. Apply only explicitly authorized hunk decisions: restore an unwanted file with
   `git checkout -- <path>` after confirming, or edit the file to revert the hunk -
   always after reading it (the edit tools enforce read-before-edit).
6. Re-list the diff and verify that only intended changes remain.

## Verification

Confirm the selected hunks, the final diff, working-tree state, encoding, line
endings, and new-file behavior.

## Boundaries

Never reject unread hunks or manually recreate a rejected edit. A review-only
request must not mutate files.