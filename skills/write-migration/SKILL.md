---
name: write-migration
description: Design a safe reversible database migration by following existing conventions and assessing data loss, defaults, indexes, locks, downtime, and rollback.
version: 1.0.0
user-invocable: true
---

# Write Migration

## Use When

Use for schema, table, column, index, constraint, backfill, or rollback work.

## Procedure

1. Confirm the database, migration tool, supported version, deployment process, and rollback expectations.
2. Read neighboring migrations and follow their naming, transaction, and safety conventions.
3. Assess existing data, nullability, defaults, indexes, locks, duration, backfill strategy, and downtime.
4. Define forward and rollback behavior, including manual steps for irreversible operations.
5. Run the migration and rollback against an isolated test database when available.

## Verification

Report what ran successfully, what was only reviewed, the expected data effect, and any live deployment risk.

## Boundaries

Do not call a migration safe without a rollback or an explicit irreversible-data decision. Ask before inventing schema contract details.
