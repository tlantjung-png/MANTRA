---
name: vault
description: Check whether a workspace has an external integrity verifier and report its scope; this MANTRA build does not ship vault tooling.
version: 1.1.0
user-invocable: false
---

# External Integrity Check

## Use When

Use only when the user explicitly asks about an existing integrity chain or
verifier in their workspace. MANTRA does not create or verify such a chain.

## Procedure

1. Inspect the workspace for an existing chain and its documented verifier.
2. If a verifier is installed, read its instructions and run it only within
   the user's requested scope. Report what files it covers and what it cannot
   establish.
3. If no verifier exists, state that integrity cannot be verified through
   this skill; do not infer a clean result from Git status or file hashes.

## Boundaries

Do not invent a vault command, chain format, or successful verification.
