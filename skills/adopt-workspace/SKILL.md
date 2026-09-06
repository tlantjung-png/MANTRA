---
name: adopt-workspace
description: Adopt a new project into MANTRA - seed its workspace .mantra state directory with a memory file and known-failure registry so every session has somewhere to read and write from day one.
version: 1.0.0
user-invocable: true
---

# Adopt Workspace

## Prerequisites

- The target project path exists and the operator wants it adopted.
- The workspace is reachable from the current session (its root is the sandbox).

## Use When

Use when starting work in a new repository/folder that should accumulate its own
project state (memory, known failures) instead of staying stateless.

## Procedure

1. Ensure the state directory exists: `<workspace>/.mantra/` (the console already
   uses it for `memory.md` and autosaved state; create it if missing).
2. Seed the ledger: write `<workspace>/.mantra/memory.md` with short sections -
   Goal (one line), Constraints, Progress (first dated bullet: adopted), Next
   Steps, Critical Context. Real content only; an empty stub is worse than none.
3. Seed the registry: if the repo's `knowledge/known-failures.md` exists, use its
   format as the template; keep only the header/format, not the incident history.
   Record workspace-specific failure classes there or in the workspace memory.
4. Optional integrity chain: this repo does not ship vault tooling (integrity
   chains were deliberately left behind - see docs/ADOPTION.md); skip this step
   unless the target project already owns a chain and an external verifier.
5. Record the adoption: add a Progress bullet to the memory ledger.

## Verification

`<workspace>/.mantra/memory.md` exists and parses with the five sections present,
and any workspace-specific known-failure entry follows the repo registry's format.

## Boundaries

Never copy API keys, credentials, or another project's incident history into the
new project. Adoption creates structure; it does not import operator state.