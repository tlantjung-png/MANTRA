"""Key resolution: env first, then store; masked display."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

# Tests point this at a temporary file; nobody else should need to.
_OVERRIDE_ENV = "MANTRA_CREDENTIALS"

_CREDENTIALS_VERSION = 1


def credentials_path() -> Path:
    """Where keys are kept. Honours MANTRA_CREDENTIALS for tests."""
    override = os.environ.get(_OVERRIDE_ENV)
    if override and override.strip():
        return Path(override.strip())
    return Path.home() / ".mantra" / "credentials.json"


def _load() -> dict[str, Any]:
    path = credentials_path()
    if not path.is_file():
        return {}
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        # Quarantine corrupt file before treating as empty (like settings)
        try:
            import shutil
            import time

            stamp = time.strftime("%Y%m%d-%H%M%S")
            backup = path.with_suffix(path.suffix + f".corrupt-{stamp}")
            shutil.copy2(path, backup)
        except OSError:
            pass
        return {}
    return data if isinstance(data, dict) else {}


def _save(data: dict[str, Any]) -> None:
    path = credentials_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    _restrict_dir(path.parent)
    content = json.dumps(data, indent=2, sort_keys=True) + "\n"
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(content, encoding="utf-8")
        _restrict_file(tmp)
        tmp.replace(path)
    except OSError:
        try:
            path.write_text(content, encoding="utf-8")
            _restrict_file(path)
        except OSError:
            pass


_WARNED_INSECURE = False


def _platform_warn_if_insecure() -> None:
    """Warn once per process when file permissions cannot be enforced."""
    global _WARNED_INSECURE
    if os.name != "nt":
        return
    # Do not warn for test overrides (temp files), only for real home path
    # to avoid spamming the test suite.
    if os.environ.get(_OVERRIDE_ENV):
        return
    if _WARNED_INSECURE:
        return
    _WARNED_INSECURE = True
    import warnings
    warnings.warn(
        "stored credentials file permissions are not enforced on this platform; "
        "consider using environment variables for keys on shared machines",
        UserWarning,
        stacklevel=3,
    )


def _restrict_dir(directory: Path) -> None:
    try:
        os.chmod(directory, 0o700)
    except OSError:
        pass
    if os.name == "nt":
        # Best-effort ACL tightening via icacls where available; ignore failures.
        try:
            import subprocess
            import getpass
            user = getpass.getuser()
            subprocess.run(
                ["icacls", str(directory), "/inheritance:r", "/grant:r", f"{user}:(OI)(CI)F"],
                capture_output=True, timeout=5
            )
        except Exception:
            pass


def _restrict_file(path: Path) -> None:
    """Best effort at owner-only access.

    Windows honours the read-only bit but not the POSIX mode bits, so
    this is a meaningful guarantee on POSIX and a hint elsewhere. The
    file is still a plain JSON document either way. On non-POSIX we emit
    a warning so the operator understands the threat model.
    """
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    if os.name == "nt":
        _platform_warn_if_insecure()
        try:
            import subprocess
            import getpass
            user = getpass.getuser()
            subprocess.run(
                ["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:F"],
                capture_output=True, timeout=5
            )
        except Exception:
            pass


def stored_keys() -> dict[str, str]:
    """Every stored key name mapped to its value."""
    data = _load()
    keys = data.get("keys")
    return dict(keys) if isinstance(keys, dict) else {}


def store(name: str, key: str) -> None:
    """Save a key. Empty or whitespace-only names are refused."""
    name = (name or "").strip()
    if not name:
        raise ValueError("a key needs a name")
    data = _load()
    keys = data.get("keys")
    if not isinstance(keys, dict):
        keys = {}
    keys[name] = (key or "").strip()
    data["keys"] = keys
    data["version"] = _CREDENTIALS_VERSION
    _save(data)


def remove(name: str) -> bool:
    """Delete a stored key. True when something was actually removed."""
    data = _load()
    keys = data.get("keys")
    if not isinstance(keys, dict) or name not in keys:
        return False
    del keys[name]
    data["keys"] = keys
    _save(data)
    return True


def resolve(api_key_env: str | None) -> str | None:
    """The key to send, or None when there is none.

    The environment wins, so a variable set for one session overrides a
    stored value without touching the file.
    """
    if not api_key_env:
        return None
    from_env = os.environ.get(api_key_env, "").strip()
    if from_env:
        return from_env
    return stored_keys().get(api_key_env, "").strip() or None


def has_stored(api_key_env: str | None) -> bool:
    return bool(api_key_env and stored_keys().get(api_key_env, "").strip())


def mask(key: str | None) -> str:
    """A form safe to print: enough to recognise, not enough to use."""
    if not key:
        return "(none)"
    if len(key) <= 8:
        return "*" * len(key)
    return f"{key[:4]}…{key[-4:]}"
