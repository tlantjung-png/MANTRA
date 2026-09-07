"""User settings: endpoints, active pick, skill prefs."""

from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path
from typing import Any, Iterable

from core.agent.keys import warn_insecure_transport

_VERSION = 1

# Tests point this elsewhere; nobody else should need to.
_OVERRIDE_ENV = "MANTRA_SETTINGS"

DEFAULT_FILE = {
    "version": _VERSION,
    "endpoints": {},
    "active": {"endpoint": "", "model": "", "reasoning_effort": None},
    "skills": {
        # Whether the router may attach a skill/bundle to a plain prompt. Kept
        # here rather than in the config because it is a preference the
        # operator switches once, and config.json is an input, not a
        # scratchpad - nothing in MANTRA writes back to it.
        "auto": True,
        "auto_bundle": True,
    },
}


def path() -> Path:
    """Where the settings live. Honours MANTRA_SETTINGS for tests."""
    return settings_path()


def settings_path() -> Path:
    """Public name for the file the user may edit by hand."""
    override = os.environ.get(_OVERRIDE_ENV)
    if override and override.strip():
        return Path(override.strip())
    return Path.home() / ".mantra" / "config.json"


# Set when the file could not be parsed. Nothing writes over a document
# it failed to understand until that document has been copied aside.
_last_error: str | None = None

# A lock this old belonged to a process that is gone.
_LOCK_STALE_SECONDS = 5.0
_LOCK_WAIT = 0.5


def last_error() -> str | None:
    """Why the settings file could not be read, or None when it could.

    The console reads this once at startup, because continuing silently
    on defaults looks exactly like "you have no endpoints" and sends the
    operator off to re-enter everything they already had.
    """
    return _last_error


def _read() -> dict[str, Any]:
    """The raw document. Records a parse failure instead of hiding it."""
    global _last_error
    file = path()
    if not file.is_file():
        _last_error = None
        return {}
    try:
        data = json.loads(file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, ValueError) as exc:
        # ValueError covers JSONDecodeError and the UnicodeDecodeError of
        # a file saved in an unexpected encoding. A file the user is
        # editing by hand will be broken sometimes. Starting empty is
        # fine; silently overwriting what we could not read is not, so
        # the failure is remembered for _write.
        _last_error = f"{file} could not be parsed: {exc}"
        return {}
    except OSError as exc:
        _last_error = f"{file} could not be read: {exc}"
        return {}
    _last_error = None
    # Non-dict documents count as empty; only parse/IO failures set _last_error.
    return data if isinstance(data, dict) else {}


def _quarantine(file: Path, reason: str) -> str | None:
    """Copy an unreadable file aside before anything replaces it.

    Returns the backup path, or None when there was nothing to save. The
    backup name is unique, so a file that stays broken never grows a
    pile of same-named copies: each surviving write quarantines the
    previous attempt's backup too.
    """
    if not file.is_file():
        return None
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = file.with_suffix(file.suffix + f".corrupt-{stamp}")
    # Second-resolution stamp collides within the same second; make the
    # name unique instead of silently overwriting the last backup.
    counter = 1
    while backup.exists():
        backup = file.with_suffix(file.suffix + f".corrupt-{stamp}-{counter}")
        counter += 1
        if counter > 100:
            return None
    try:
        shutil.copy2(file, backup)
        return str(backup)
    except OSError:
        # Nothing more can be done; the caller still has to decide.
        return None


def _break_stale_lock(lock_path: Path) -> bool:
    """Try to break a stale lock atomically. Returns True if removed."""
    try:
        stat = lock_path.stat()
        age = time.time() - stat.st_mtime
        if age < _LOCK_STALE_SECONDS:
            return False
        # Compare-and-remove: re-stat before unlink so a fresh lock is never deleted.
        try:
            stat2 = lock_path.stat()
            if stat2.st_mtime != stat.st_mtime:
                return False
            lock_path.unlink(missing_ok=True)
            return True
        except FileNotFoundError:
            return True
    except OSError:
        return False


def _write(data: dict[str, Any]) -> bool:
    """Persist the document atomically. Returns False when the write failed."""
    file = path()
    try:
        file.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False
    try:
        os.chmod(file.parent, 0o700)
    except OSError:
        pass
    if _last_error is not None:
        _quarantine(file, _last_error)
    data["version"] = _VERSION  # Force the current schema version on every write.
    content = json.dumps(data, indent=2, sort_keys=True) + "\n"
    # File lock for inter-process safety (best effort).
    # Use atomic exclusive create as the sole arbiter; stale check is
    # opportunistic and its removal is verified to avoid deleting a
    # freshly created lock from another process.
    lock_path = file.with_suffix(file.suffix + ".lock")
    # Opportunistically break a stale lock before trying; if break fails,
    # the subsequent open will simply wait.
    if lock_path.exists():
        _break_stale_lock(lock_path)
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
            # Opportunistically retry stale break if lock looks old
            try:
                if time.time() - lock_path.stat().st_mtime >= _LOCK_STALE_SECONDS:
                    _break_stale_lock(lock_path)
            except OSError:
                pass
        except OSError:
            break
    if not acquired:
        # Another process holds the lock: skip the write rather than race it.
        return False
    # Unique temp name: no fixed path for a planted symlink to hijack and
    # no shared file for two writers to interleave into.
    import tempfile

    try:
        fd, tmp_name = tempfile.mkstemp(dir=str(file.parent), prefix=file.name + ".", suffix=".tmp")
    except OSError:
        # Temporary-file creation failed outside the guarded region below;
        # release the lock and report the failure per the bool contract.
        if acquired and lock_fd is not None:
            try:
                os.close(lock_fd)
            except OSError:
                pass
            try:
                lock_path.unlink(missing_ok=True)
            except OSError:
                pass
        return False
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        tmp.replace(file)
        return True
    except OSError:
        # No direct-write fallback: a non-atomic write to the target path
        # could follow a planted symlink, exactly the hazard the unique
        # temp name exists to avoid. Report the failure instead — the
        # caller decides whether to retry, and the previous document
        # (if any) is untouched.
        try:
            tmp.unlink()
        except OSError:
            pass
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


def load() -> dict[str, Any]:
    """The whole settings document, with the expected shape guaranteed."""
    data = _read()
    out = {
        "version": _VERSION,
        "endpoints": {},
        "active": dict(DEFAULT_FILE["active"]),
        "skills": dict(DEFAULT_FILE["skills"]),
    }
    skills = data.get("skills")
    if isinstance(skills, dict):
        # Copy only known preference keys; unknown stored keys are dropped.
        out["skills"].update({key: skills.get(key, value) for key, value in DEFAULT_FILE["skills"].items()})
    endpoints = data.get("endpoints")
    if isinstance(endpoints, dict):
        for name, entry in endpoints.items():
            if isinstance(entry, dict) and entry.get("base_url"):
                # Every lookup lower-cases (get_endpoint/models_for/
                # remove_endpoint) and add_endpoint stores lower-case, so
                # a hand-edited mixed-case name must be normalized here or
                # it becomes unreachable.
                out["endpoints"][str(name).strip().lower()] = _clean_endpoint(entry)
    active = data.get("active")
    if isinstance(active, dict):
        out["active"].update({k: active.get(k, v) for k, v in DEFAULT_FILE["active"].items()})
    return out


def _clean_endpoint(entry: dict[str, Any]) -> dict[str, Any]:
    base_url = str(entry.get("base_url", "")).strip().rstrip("/")
    return {
        "base_url": base_url,
        "api_key_env": str(entry.get("api_key_env", "")).strip(),
        "models": [str(m) for m in entry.get("models") or [] if str(m).strip()],
        "note": str(entry.get("note", "")).strip(),
    }


# ---- endpoints -----------------------------------------------------------


def endpoints() -> dict[str, dict[str, Any]]:
    return load()["endpoints"]


def get_endpoint(name: str) -> dict[str, Any] | None:
    # Endpoints are stored lowercased, so lookups normalize too.
    return endpoints().get((name or "").strip().lower())


def add_endpoint(
    name: str,
    base_url: str,
    api_key_env: str = "",
    models: Iterable[str] | None = None,
    note: str = "",
) -> None:
    """Add or replace an endpoint. Raises ValueError on a bad entry."""
    name = (name or "").strip().lower()
    if not name:
        raise ValueError("an endpoint needs a name")
    url = (base_url or "").strip()
    if not url.startswith(("http://", "https://")):
        raise ValueError("base_url must start with http:// or https://")
    data = load()
    existing = data["endpoints"].get(name)
    # A key attached to a plain-http endpoint crosses the network in
    # cleartext; warn once per process so local servers stay usable.
    warn_insecure_transport(url, bool(api_key_env or (existing or {}).get("api_key_env", "")))
    merged = _clean_endpoint(
        {
            "base_url": url,
            "api_key_env": api_key_env or (existing or {}).get("api_key_env", ""),
            # Hand-added models survive a re-connect: only replace the
            # list when new ones were actually discovered (None means keep).
            "models": list(models) if models is not None else (existing or {}).get("models", []),
            "note": note or (existing or {}).get("note", ""),
        }
    )
    data["endpoints"][name] = merged
    _write(data)


def remove_endpoint(name: str) -> bool:
    # Stored keys are lowercased; normalize so a mixed-case name does not
    # silently do nothing.
    name = (name or "").strip().lower()
    data = load()
    if name not in data["endpoints"]:
        return False
    del data["endpoints"][name]
    # Removing the active endpoint also blanks the active pick.
    if data["active"].get("endpoint") == name:
        data["active"]["endpoint"] = ""
        data["active"]["model"] = ""
    _write(data)
    return True


def set_models(name: str, models: Iterable[str]) -> None:
    """Record what an endpoint serves, so /model can list it offline."""
    name = (name or "").strip().lower()
    data = load()
    entry = data["endpoints"].get(name)
    if entry is None:
        return
    entry["models"] = [str(m) for m in models if str(m).strip()]
    _write(data)


def models_for(name: str) -> list[str]:
    entry = get_endpoint(name)
    return list(entry.get("models") or []) if entry else []


def endpoint_name_for_url(base_url: str) -> str | None:
    """Which endpoint name, if any, serves this base URL."""
    wanted = (base_url or "").strip().rstrip("/").lower()
    for name, entry in endpoints().items():
        if entry.get("base_url", "").rstrip("/").lower() == wanted:
            return name
    return None


class _Unset:
    """Sentinel so ``set_active(effort=None)`` can mean "off".

    Plain ``None`` cannot carry that meaning, because it is also the
    value the sentinel has to be distinguishable from.
    """

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "<unset>"


_UNSET = _Unset()


# ---- the active pick -----------------------------------------------------


def active() -> dict[str, Any]:
    return load()["active"]


def set_active(
    endpoint: str | None = None,
    model: str | None = None,
    reasoning_effort: Any = _UNSET,
) -> None:
    """Record the current choice. Arguments left as-is when not given."""
    data = load()
    if endpoint is not None:
        data["active"]["endpoint"] = endpoint
    if model is not None:
        data["active"]["model"] = model
    if reasoning_effort is not _UNSET:
        data["active"]["reasoning_effort"] = reasoning_effort
    _write(data)


def skills_prefs() -> dict[str, Any]:
    """The skills preferences the operator has actually set.

    Empty rather than defaulted when they have never touched them, so a
    config.json that sets these is not silently overridden by defaults
    that were merely assumed on their behalf.
    """
    stored = _read().get("skills")
    if not isinstance(stored, dict):
        return {}
    return {
        key: bool(stored[key])
        for key in DEFAULT_FILE["skills"]
        if key in stored
    }


def set_skills_prefs(auto: bool | None = None, auto_bundle: bool | None = None) -> None:
    """Record the skills preferences. Arguments left as-is when not given."""
    data = load()
    if auto is not None:
        data["skills"]["auto"] = bool(auto)
    if auto_bundle is not None:
        data["skills"]["auto_bundle"] = bool(auto_bundle)
    _write(data)


def validate_endpoint(entry: dict[str, Any]) -> str | None:
    """An error message when an endpoint entry is unusable, else None."""
    if not isinstance(entry, dict):
        return "endpoint entry must be an object"
    if not str(entry.get("base_url", "")).strip():
        return "base_url is required"
    url = str(entry["base_url"]).strip().lower()
    if not url.startswith(("http://", "https://")):
        return "base_url must start with http:// or https://"
    warn_insecure_transport(url, bool(str(entry.get("api_key_env", "")).strip()))
    return None
