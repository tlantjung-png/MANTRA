---
name: commit-message
description: Derive a conventional commit message from the actual staged or working-tree change while keeping unrelated concerns separate.
version: 1.0.0
user-invocable: true
---

# Commit Message

## Use When

Use when a commit message is requested or when a mixed change needs to be split into atomic commit themes.

## Procedure

1. Inspect staged and unstaged changes, recent repository message style, and dependency relationships.
2. Classify the primary change type and summarize its user or maintainer effect.
3. Detect unrelated concerns, generated files, and impossible dependency order.
4. Show the proposed message and wait for approval before committing.
5. Confirm that the final message matches the exact diff and that no unrelated work was staged.

## Verification

The message must describe the actual change, use the repository's conventional style, and avoid claiming tests or behavior not present in the diff.

## Boundaries

Do not commit, amend, push, or force-push unless explicitly requested. Do not combine unrelated changes merely to make a message shorter.
