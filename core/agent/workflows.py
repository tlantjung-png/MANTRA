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
# Inter-process write lock: exclusive create is the arbiter.
_LOCK_STALE = 5.0  # a lock this old belonged to a dead process
_LOCK_WAIT = 0.5  # how long to wait for the lock before giving up


def workflows_path() -> Path:
    """Path to the workflows file; MANTRA_WORKFLOWS relocates it for tests."""
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
        for attempt in range(100):
            suffix = f".corrupt-{stamp}" if attempt == 0 else f".corrupt-{stamp}-{attempt}"
            backup = target.with_suffix(target.suffix + suffix)
            if not backup.exists():
                shutil.copy2(target, backup)
                return
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
    except (OSError, ValueError):
        # ValueError covers JSONDecodeError and the UnicodeDecodeError a
        # non-UTF-8 file raises; both mean "unusable workflows file".
        _quarantine(target)
        return _empty()
    if not isinstance(data, dict) or not isinstance(data.get("workflows"), dict):
        _quarantine(target)
        return _empty()
    return data


def _break_stale(lock_path: Path) -> bool:
    """Remove a lock whose holder is gone. True when removed.

    The mtime is checked twice so a freshly created lock is never deleted.
    """
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


def _save_all(data: dict[str, Any]) -> bool:
    """Persist the workflows document under an inter-process lock."""
    target = workflows_path()
    # File lock for inter-process safety — atomic exclusive create is arbiter.
    lock_path = target.with_suffix(target.suffix + ".lock")
    if lock_path.exists():
        _break_stale(lock_path)
    acquired = False
    lock_fd = None
    start = time.monotonic()
    while time.monotonic() - start < _LOCK_WAIT:
        try:
            lock_fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
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
    if not acquired:
        # Another process holds the lock: skip the write rather than race
        # it. The caller reports the failure and can retry.
        return False
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(target.parent, 0o700)
        except OSError:
            pass
        content = json.dumps(data, ensure_ascii=False, indent=2)
        # Unique temp name: no fixed path for a planted symlink, no shared
        # file for two writers to interleave into.
        import tempfile

        fd_tmp, tmp_name = tempfile.mkstemp(
            dir=str(target.parent), prefix=target.name + ".", suffix=".tmp"
        )
        try:
            with os.fdopen(fd_tmp, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(content)
        except OSError:
            # fdopen itself failed: the raw descriptor must be closed or
            # it leaks.
            try:
                os.close(fd_tmp)
            except OSError:
                pass
            raise
        try:
            try:
                os.chmod(tmp_name, 0o600)
            except OSError:
                pass
            os.replace(tmp_name, target)
        except OSError:
            # No direct-write fallback: a non-atomic write to the target
            # path could follow a planted symlink. Report the failure;
            # the caller surfaces it and the previous file is untouched.
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            return False
    except OSError:
        return False
    finally:
        if acquired and lock_fd is not None:
            try:
                os.close(lock_fd)
            except OSError:
                pass
            try:
                lock_path.unlink(missing_ok=True)
            except OSError:
                pass
    return True


def get(name: str) -> dict[str, Any] | None:
    """One launchable workflow by name, or None."""
    found = load_all()["workflows"].get(slug(name))
    if not isinstance(found, dict):
        return None
    steps = found.get("steps")
    if not isinstance(steps, list) or not steps:
        return None
    return found


def list_workflows() -> list[dict[str, Any]]:
    """Workflows with steps, alphabetically, with their step counts."""
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
