"""Key resolution: env first, then store; masked display."""

from __future__ import annotations

import contextlib
import json
import os
import threading
import time
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
    # A unique, unpredictable temp name in the same directory: a planted
    # symlink at a fixed path cannot hijack the write, and two writers
    # never share a temp file.
    import tempfile

    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        _restrict_file(tmp)
        try:
            tmp.replace(path)
        except OSError:
            # One short retry: transient failures (an antivirus scan, a
            # briefly held lock) usually clear. There is deliberately no
            # direct-write fallback: a non-atomic write to the target
            # path could follow a planted symlink, so a still-failing
            # replace must surface to the caller instead of silently
            # weakening the atomic-write guarantee.
            time.sleep(0.05)
            tmp.replace(path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


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
    """Best-effort owner-only access; only a hint where mode bits are ignored."""
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


_CRED_LOCK = threading.Lock()


@contextlib.contextmanager
def _store_locked():
    """Serialize credential read-modify-write.

    An in-process lock plus a best-effort advisory lock on a sibling
    lock file (fcntl/msvcrt). Locking failures degrade to unlocked
    rather than failing the write: the atomic replace already prevents
    file corruption, the lock only prevents lost updates from two
    concurrent writers.
    """
    with _CRED_LOCK:
        lock_path = credentials_path().with_name(credentials_path().name + ".lock")
        handle = None
        try:
            handle = open(lock_path, "a+b")
            # One byte must exist before msvcrt.locking; seek makes the
            # lock position well-defined.
            handle.write(b"\0")
            handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        except Exception:
            if handle is not None:
                try:
                    handle.close()
                except OSError:
                    pass
            handle = None
        try:
            yield
        finally:
            if handle is not None:
                try:
                    handle.seek(0)
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
                try:
                    handle.close()
                except OSError:
                    pass


def store(name: str, key: str) -> None:
    """Save a key. Empty or whitespace-only names are refused."""
    name = (name or "").strip()
    if not name:
        raise ValueError("a key needs a name")
    with _store_locked():
        data = _load()
        keys = data.get("keys")
        if not isinstance(keys, dict):
            keys = {}
        keys[name] = (key or "").strip()
        data["keys"] = keys
        data["version"] = _CREDENTIALS_VERSION  # schema version marker; not read back
        _save(data)


def remove(name: str) -> bool:
    """Delete a stored key. True when something was actually removed."""
    with _store_locked():
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
