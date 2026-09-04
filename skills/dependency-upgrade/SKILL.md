---
name: dependency-upgrade
description: Upgrade one dependency while researching compatibility, handling breaking changes, preserving scope, and proving the result with project checks.
version: 1.0.0
user-invocable: true
---

# Dependency Upgrade

## Use When

Use for a package, library, module, runtime, or tool version change.

## Procedure

1. Confirm the current and requested versions, source of truth, supported runtime, and lockfile policy.
2. Read the dependency's change history and identify breaking behavior, removed APIs, and security implications.
3. Update only the requested dependency and its required metadata.
4. Fix compatibility fallout, then run the full project checks and relevant integration tests.
5. Summarize behavior changes and any manual deployment step.

## Verification

Compilation or parsing alone is insufficient. Require the project's tests and diagnostics, and report unavailable external or live checks.

## Boundaries

Do not install missing tooling without approval. Major upgrades and live-money changes require explicit risk discussion.
