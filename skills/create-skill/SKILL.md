---
name: create-skill
description: Create and register a new workflow skill with valid metadata, a focused procedure, catalog and routing entries, and verification coverage.
version: 1.0.0
user-invocable: true
disable-model-invocation: true
---

# Create Skill

## Use When

Use when a repeatable workflow deserves a new skill directory and a user has approved its scope.

## Procedure

1. Confirm the skill name, trigger description, scope, side effects, expected inputs, outputs, and completion check.
2. Keep the description specific because it is the invocation contract.
3. Scaffold one skill file plus shallow scripts and references directories.
4. Add the skill to the catalog and the routing table.
5. Verify through the console: /skills show lists the new skill, the loader parses the frontmatter (matching name, nonempty description), and the catalog (skills/INDEX.md) and routing table agree with the on-disk directory, then reread the final procedure.

## Verification

The folder name and frontmatter name must match, use hyphen-case, remain within the length limit, and contain a nonempty description. The on-disk folder, catalog, and routing table must agree. No scaffolding tool ships with this repository; validation is the manual cross-check above (loader frontmatter parse, /skills show, INDEX and routing agreement).

## Boundaries

File-writing scaffolding is side-effecting and should be manually invoked. Require approval before creating the skill, and do not skip registration.
