---
name: pr
description: Manage the full pull-request lifecycle through the repository's approved GitHub integration, including review comments, CI, synchronization, stacked PRs, and explicit merge approval.
version: 1.1.0
user-invocable: true
---

# Pull Request

## Use When

Use for creating or updating a pull request, fetching review comments, resolving CI, syncing branches, landing a stack of dependent PRs, or merging.

## Procedure

1. Inspect status, recent history, remote tracking, base branch, and the complete change set.
2. Create or update the pull request with a truthful summary, verification record, and risk notes.
3. Fetch review comments and inspect the exact hunks they concern.
4. Fix accepted issues through the appropriate debug, test, or diff-review workflow, then rerun checks.
5. Merge only after explicit approval and green required checks.

## Stacked Pull Requests

When landing a chain of dependent PRs (each based on the one below):

1. Require the native stack feature; hard-stop rather than reproducing stack semantics by merging and retargeting PRs one at a time.
2. Fetch live PR metadata and exact head OIDs; establish the bottom-to-top order from the live base references, not branch names or an earlier report.
3. Link same-author chains additively in bottom-to-top order; if authors differ or an existing stack conflicts, ask before mutating GitHub state. Never dissolve, reorder, or rebuild an existing stack automatically.
4. Preflight the merge range: every selected PR open, non-draft, in order, and compliant with review and check requirements — a ready top layer does not prove its dependencies are ready.
5. Merge the whole stack, or an explicitly bounded prefix, through the stack API (`gh stack merge --yes --merge`). Do not bypass merge requirements or fall back to per-PR merges.
6. Wait for every selected PR to report MERGED; a queued request is not a completed landing.
7. After any history rewrite, re-fetch live heads and re-audit unresolved review threads, approvals, mergeability, and checks; never use raw `--force`.
8. Delete branches only in a separate final pass, after each PR reports MERGED and no open PR still uses the branch as its base.

## Verification

Before a PR is opened or merged, confirm the intended commits, base comparison, CI status, review resolution, and working-tree state.

## Boundaries

Do not use ad hoc network scripts, force-push shared branches, resolve comments without judgment, or merge without explicit authorization.
