"""Workflows: named ordered prompt sequences."""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

_VERSION = 1
_OVERRIDE_ENV = "MANTRA_WORKFLOWS"

_MAX_STEPS = 50
_MAX_STEP_CHARS = 4000
_LOCK_STALE = 5.0
_LOCK_WAIT = 0.5


def workflows_path() -> Path:
    """Where workflows live. Honours MANTRA_WORKFLOWS for tests."""
    override = os.environ.get(_OVERRIDE_ENV)
    if override and override.strip():
        return Path(override.strip())
    return Path.home() / ".mantra" / "workflows.json"


def slug(name: str) -> str:
    """A name safe to type and to use as a filename.

    Spaces are collapsed rather than rejected, so `/workflow create my
    flow` produces `my-flow` instead of an error - and `/workflow launch
    my flow` finds it again.
    """
    return re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")


def _empty() -> dict[str, Any]:
    return {"version": _VERSION, "workflows": {}}


def _quarantine(target: Path) -> None:
    """Copy corrupt file aside before overwriting."""
    try:
        import shutil
        import time

        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup = target.with_suffix(target.suffix + f".corrupt-{stamp}")
        shutil.copy2(target, backup)
    except OSError:
        pass


def load_all() -> dict[str, Any]:
    """The whole file. A missing or corrupt file reads as empty.

    Corrupt is deliberately not an exception here: a hand-edited file
    with a stray comma should cost the operator their last edit, not
    every workflow they have.
    """
    target = workflows_path()
    if not target.is_file():
        return _empty()
    try:
        with open(target, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        _quarantine(target)
        return _empty()
    if not isinstance(data, dict) or not isinstance(data.get("workflows"), dict):
        return _empty()
    return data


def _break_stale(lock_path: Path) -> bool:
    try:
        stat = lock_path.stat()
        age = time.time() - stat.st_mtime
        if age < _LOCK_STALE:
            return False
        # Verify mtime unchanged to avoid deleting a freshly created lock
        try:
            stat2 = lock_path.stat()
            if stat2.st_mtime != stat.st_mtime:
                return False
        except OSError:
            return False
        lock_path.unlink(missing_ok=True)
        return True
    except OSError:
        return False
    return False


def _save_all(data: dict[str, Any]) -> bool:
    target = workflows_path()
    # File lock for inter-process safety — atomic exclusive create is arbiter.
    lock_path = target.with_suffix(target.suffix + ".lock")
    if lock_path.exists():
        _break_stale(lock_path)
    acquired = False
    fd = None
    start = time.monotonic()
    while time.monotonic() - start < _LOCK_WAIT:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            acquired = True
            break
        except FileExistsError:
            time.sleep(0.02)
            try:
                if time.time() - lock_path.stat().st_mtime >= _LOCK_STALE:
                    _break_stale(lock_path)
            except OSError:
                pass
        except OSError:
            break
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(target.parent, 0o700)
        except OSError:
            pass
        content = json.dumps(data, ensure_ascii=False, indent=2)
        tmp = target.with_suffix(target.suffix + ".tmp")
        try:
            with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(content)
            try:
                os.chmod(tmp, 0o600)
            except OSError:
                pass
            tmp.replace(target)
        except OSError:
            with open(target, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(content)
            try:
                os.chmod(target, 0o600)
            except OSError:
                pass
    except OSError:
        return False
    finally:
        if acquired and fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
            try:
                lock_path.unlink(missing_ok=True)
            except OSError:
                pass
    return True


def get(name: str) -> dict[str, Any] | None:
    """One workflow by name, or None."""
    found = load_all()["workflows"].get(slug(name))
    if not isinstance(found, dict):
        return None
    steps = found.get("steps")
    if not isinstance(steps, list):
        return None
    return found


def list_workflows() -> list[dict[str, Any]]:
    """Every workflow, alphabetically, with its step count."""
    out = []
    for key, value in load_all()["workflows"].items():
        if not isinstance(value, dict):
            continue
        steps = value.get("steps")
        # Skipped unless it really has steps, matching get(): an entry
        # that cannot be shown or launched must not be listed, or the
        # operator picks it from the list and is told it does not exist.
        if not isinstance(steps, list) or not steps:
            continue
        out.append(
            {
                "name": value.get("name") or key,
                "created_at": value.get("created_at") or "",
                "steps": [str(step) for step in steps],
            }
        )
    out.sort(key=lambda item: item["name"])
    return out


def create(name: str, steps: list[str]) -> tuple[bool, str]:
    """Add or replace a workflow. Returns (ok, message)."""
    key = slug(name)
    if not key:
        return False, "a workflow needs a name"
    clean = [str(step).strip() for step in steps if str(step).strip()]
    if not clean:
        return False, "a workflow needs at least one step"
    if len(clean) > _MAX_STEPS:
        return False, f"too many steps (limit {_MAX_STEPS})"
    too_long = [step for step in clean if len(step) > _MAX_STEP_CHARS]
    if too_long:
        return False, f"a step is longer than {_MAX_STEP_CHARS} characters"

    data = load_all()
    existed = key in data["workflows"]
    data["workflows"][key] = {
        "name": key,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "steps": clean,
    }
    if not _save_all(data):
        return False, "could not write the workflow file"
    count = len(clean)
    label = f"{count} step" if count == 1 else f"{count} steps"
    verb = "updated" if existed else "created"
    return True, f"{verb} '{key}' ({label})"


def delete(name: str) -> bool:
    key = slug(name)
    data = load_all()
    if key not in data["workflows"]:
        return False
    del data["workflows"][key]
    return _save_all(data)
