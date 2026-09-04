#!/usr/bin/env python3
"""MANTRA statusline.

JSON on stdin (workspace.current_dir / cwd), one plain line on stdout.
Counts pending briefs (<24h) + vault freshness.
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

def get_json_field(obj, names):
    for n in names:
        cur = obj
        for seg in n.split("."):
            if not isinstance(cur, dict) or seg not in cur:
                cur = None
                break
            cur = cur[seg]
        if cur not in (None, ""):
            return str(cur)
    return None

def main():
    raw = sys.stdin.read()
    cwd = None
    # Unparseable or empty input is indistinguishable from no payload:
    # fall back to the current directory.
    try:
        if raw.strip():
            j = json.loads(raw)
            cwd = get_json_field(j, ["workspace.current_dir", "cwd", "folder"])
    except Exception:
        pass
    if not cwd:
        try:
            cwd = os.getcwd()
        except Exception:
            cwd = "."

    parts = []

    # Pending briefs (last 24h)
    for base in [".mantra/orchestrate", "orchestrate"]:
        mailbox = Path(cwd) / base
        if mailbox.is_dir():
            pending = 0
            cut = datetime.now() - timedelta(days=1)
            for d in mailbox.iterdir():
                if d.is_dir():
                    for f in d.glob("*.md"):
                        try:
                            if datetime.fromtimestamp(f.stat().st_mtime) >= cut:
                                pending += 1
                        except Exception:
                            pass
            if pending > 0:
                parts.append(f"briefs:{pending}")
            # First existing mailbox wins, even when it has no pending briefs.
            break

    # Vault freshness: only the nearest ancestor with a chain is reported.
    probe = Path(cwd).resolve()
    for parent in [probe] + list(probe.parents):
        chain = parent / ".mantra" / "vault" / "chain.json"
        chain_path = chain if chain.exists() else None
        if chain_path and chain_path.exists():
            try:
                age_h = int((datetime.now() - datetime.fromtimestamp(chain_path.stat().st_mtime)).total_seconds() // 3600)
                # The "!" suffix marks a chain older than 72h (display only).
                label = f"vault:{age_h}h" if age_h <= 72 else f"vault:{age_h}h!"
                parts.append(label)
            except Exception:
                pass
            break

    if parts:
        sys.stdout.write(" | ".join(parts))

if __name__ == "__main__":
    main()
