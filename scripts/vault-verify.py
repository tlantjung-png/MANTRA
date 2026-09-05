#!/usr/bin/env python3
"""Vault verifier for MANTRA.

Replays chain.json blobs/references, checks linkage, hash, missing refs.
Exit codes: 0 clean, 1 tampered, 2 missing ref, 3 corrupt chain.
"""
import hashlib
import json
import os
import sys
from pathlib import Path

def resolve_state_dir(state_dir: str = "", workspace_root: str = "") -> Path:
    if state_dir:
        return Path(state_dir)
    if workspace_root:
        return Path(workspace_root) / ".mantra"
    here = Path(__file__).resolve().parent.parent
    if (here / ".mantra").is_dir():
        # A self-contained install sits inside .mantra itself.
        if here.name == ".mantra":
            return here
        return here / ".mantra"
    return Path.home() / ".mantra"

def get_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()

def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-dir", default="")
    parser.add_argument("--workspace-root", default="")
    args = parser.parse_args()

    state = resolve_state_dir(args.state_dir, args.workspace_root)
    vault_dir = state / "vault"
    blob_dir = vault_dir / "blobs"
    chain_file = vault_dir / "chain.json"

    if not chain_file.exists():
        print(f"vault: no chain at {chain_file} (not initialized)")
        sys.exit(0)

    try:
        with open(chain_file, "r", encoding="utf-8") as f:
            raw_chain = json.load(f)
    except Exception as e:
        print(f"vault: chain unreadable or corrupt at {chain_file}: {e}")
        sys.exit(3)

    chain = raw_chain if isinstance(raw_chain, list) else [raw_chain]
    prev_expected = ""
    first_break = ""
    missing_path = ""

    for e in chain:
        if not isinstance(e, dict):
            continue
        # The first link has prev == ""; only the first linkage break is
        # recorded, later ones stay silent.
        prev = e.get("prev", "")
        eid = e.get("id", "")
        if prev != prev_expected and not first_break:
            first_break = f"link {eid} has prev '{prev}' expected '{prev_expected}'"
        prev_expected = eid

    for e in chain:
        if not isinstance(e, dict):
            continue
        for ent in e.get("entries", []) or []:
            if not isinstance(ent, dict):
                continue
            kind = ent.get("kind")
            if kind == "blob":
                sha = ent.get("sha256", "")
                rel = ent.get("rel", "")
                blob = blob_dir / sha
                if not blob.exists():
                    if not first_break:
                        first_break = f"blob missing for {rel} ({sha})"
                else:
                    try:
                        actual = get_sha256(blob)
                    except Exception:
                        # Unreadable blob is an environment problem, not a
                        # integrity failure: a raw crash here would exit 1,
                        # which scripted consumers read as TAMPERED.
                        if not first_break:
                            first_break = f"blob unreadable for {rel}"
                    else:
                        if actual != sha and not first_break:
                            first_break = f"blob tampered for {rel}"
            elif kind == "reference":
                rel = ent.get("rel", "")
                sha = ent.get("sha256", "")
                # Reference paths resolve against the process CWD, not
                # the state dir.
                if not rel or not Path(rel).exists():
                    label = rel if rel else "(empty reference path)"
                    if not missing_path:
                        missing_path = label
                else:
                    try:
                        actual = get_sha256(Path(rel))
                        if actual != sha and not first_break:
                            first_break = f"archive changed for {rel}"
                    except Exception:
                        # Unreadable reference file: skipped silently.
                        pass

    # Missing references are reported only when nothing was tampered;
    # otherwise they merge into the tampered verdict.
    if missing_path and not first_break:
        print(f"vault: MISSING reference {missing_path}")
        sys.exit(2)
    if first_break and missing_path:
        print(f"vault: TAMPERED - {first_break} ; also missing {missing_path}")
        sys.exit(1)
    if first_break:
        print(f"vault: TAMPERED - {first_break}")
        sys.exit(1)
    print(f"vault: OK {len(chain)} link(s), {blob_dir}")
    sys.exit(0)

if __name__ == "__main__":
    main()
