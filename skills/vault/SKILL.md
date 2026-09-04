---
name: vault
description: Verify the append-only integrity history of project state (the .mantra/vault chain) at checkpoints, after refinements, or before declaring done, using the repo's read-only verifier.
version: 1.0.0
user-invocable: true
---

# Vault

## Use When

Use at a meaningful checkpoint, after a refinement completion, before or after a
backup, or before declaring a task done that depends on durable state.

## Procedure

1. Locate the chain: a project's integrity chain lives at `<workspace>/.mantra/vault/`
   with `chain.json` and `blobs/`. The repo ships `scripts/vault-verify.py` to
   check it.
2. Verify the chain before declaring done: run
   `python scripts/vault-verify.py --workspace-root <workspace>` (or `--state-dir
   <dir>` for a standalone state directory). It replays `chain.json`, checks
   linkage, hashes, and missing references.
3. Confirm linkage, stored content hashes, and that referenced blobs exist and
   match. Drift from current live files is separate from chain tampering and is
   reported, not failed.
4. When verification fails, identify the exact break and treat the state as
   untrusted. Recover through a backup, a trusted checkpoint, or version control;
   the vault does not repair.

## Notes

- Verification is read-only. Initializing or checkpointing a chain is out of
  scope for this repo's tooling; the `.mantra/vault` directory is the machine-local
  store and version control remains the portability mechanism.

## Verification

Run the repo's verifier and confirm a clean result. A tampered blob or changed
archive must be reported by name.

## Boundaries

The vault detects and reports integrity problems; it never repairs them. It proves
integrity for the recorded scope only, not provenance for arbitrary files.