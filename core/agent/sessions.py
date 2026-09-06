"""Sessions: one JSON per saved conversation."""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

_VERSION = 1
_OVERRIDE_ENV = "MANTRA_SESSIONS"



def sessions_dir() -> Path:
    """Where saved sessions live. Created on demand."""
    override = os.environ.get(_OVERRIDE_ENV)
    if override and override.strip():
        target = Path(override.strip())
    else:
        target = Path.home() / ".mantra" / "sessions"
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError:  # pragma: no cover - read-only home
        pass
    return target


def _slug(text: str) -> str:
    # Slug: lowercase alphanumeric runs joined by hyphens, shared by all
    # name-derivation paths.
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")


def derive_name(workspace: str = "", model: str = "") -> str:
    """A name for a session the operator never bothered to name.

    ``C:\\Users\\arif-\\K-CHAT`` -> ``k-chat-20260829-0812``. The stamp
    keeps two sessions from the same directory from colliding, and it
    sorts usefully because it reads year-month-day.
    """
    import uuid  # Imported lazily: only needed on the rare same-second collision.

    base = _slug(Path(workspace or "").name) if workspace else ""
    if not base and model:
        base = _slug(model)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    candidate = f"{base}-{stamp}" if base else stamp
    # Avoid collisions within the same second by appending a short suffix
    # only when a file with the same name already exists.
    if _path(candidate).exists():
        candidate = f"{candidate}-{uuid.uuid4().hex[:4]}"
        # Extremely unlikely second collision
        if _path(candidate).exists():
            candidate = f"{candidate}-{uuid.uuid4().hex[:2]}"
    return candidate


def _path(name: str) -> Path:
    # Sanitize name to prevent path traversal (e.g. ../../etc/passwd)
    # Include hash of original name in fallback to avoid collisions where
    # distinct unsafe names previously mapped to same generic slug.
    # Lowercase up front: Foo and foo must not become case-distinct
    # files that collide on a case-insensitive filesystem.
    import hashlib
    name_lower = (name or "").lower()
    safe = re.sub(r"[^a-z0-9._-]", "-", name_lower).strip("-._")
    original = name
    if not safe or safe != name_lower:
        # Fallback slug plus a short hash keeps distinct unsafe names unique.
        slug_base = _slug(name) or "session"
        digest = hashlib.sha256(original.encode("utf-8")).hexdigest()[:6]
        safe = f"{slug_base}-{digest}"
    # Prevent directory traversal via Path
    safe = Path(safe).name
    return sessions_dir() / f"{safe}.json"


# A transcript is rewritten after every turn, so without a ceiling both
# the file and the cost of a turn grow with the length of the session.
# Long tool output is trimmed rather than the message dropped, because a
# dropped message can orphan the tool call it answers.
_MAX_MESSAGE_CHARS = 20_000

# Sessions over this size are unusable as context and too costly to parse
# on every listing; matches the console's explicit-load guard so /sessions
# cannot bypass it by reading the file directly.
_MAX_SESSION_BYTES = 10_000_000

# Inter-process save lock: an exclusive create is the arbiter, matching
# the settings and workflow stores.
_LOCK_STALE_SECONDS = 5.0
_LOCK_WAIT = 0.5


def _break_stale_lock(lock_path: Path) -> bool:
    try:
        stat = lock_path.stat()
        age = time.time() - stat.st_mtime
        if age < _LOCK_STALE_SECONDS:
            return False
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


def _trim_messages(messages: list[Any]) -> list[Any]:
    """Cap each message's content so the transcript stays bounded."""
    trimmed: list[Any] = []
    for message in messages:
        if not isinstance(message, dict):
            trimmed.append(message)
            continue
        content = message.get("content")
        if not isinstance(content, str) or len(content) <= _MAX_MESSAGE_CHARS:
            trimmed.append(message)
            continue
        copy = dict(message)
        copy["content"] = (
            content[:_MAX_MESSAGE_CHARS].rstrip() + "\n... [truncated on save]"
        )
        trimmed.append(copy)
    return trimmed


def save(name: str, payload: dict[str, Any]) -> str | None:
    """Write a session. Returns the path, or None when it could not be written."""
    record = {
        "version": _VERSION,
        "name": name,
        "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        # Caller payload spread last, so it can override metadata keys.
        **payload,
    }
    messages = record.get("messages")
    if isinstance(messages, list):
        record["messages"] = _trim_messages(messages)
    target = _path(name)
    # Ensure directory exists and has restricted permissions
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(target.parent, 0o700)
        except OSError:
            pass
    except OSError:
        pass
    try:
        content = json.dumps(record, ensure_ascii=False, indent=2)
    except (TypeError, ValueError):
        # A payload value json cannot encode must not raise out of save():
        # the caller autosaves every turn, and one bad value is not worth
        # losing the session over; skip the write instead.
        return None
    # Inter-process lock: two consoles autosaving the same session name
    # must not interleave. If the lock cannot be taken in time, skip the
    # save rather than write a half-written transcript over a good one.
    lock_path = target.with_suffix(target.suffix + ".lock")
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
            try:
                if time.time() - lock_path.stat().st_mtime >= _LOCK_STALE_SECONDS:
                    _break_stale_lock(lock_path)
            except OSError:
                pass
        except OSError:
            # Lock creation failed for a non-conflict reason: skip the save.
            break
    if not acquired:
        return None
    # Unique temp name: no fixed path for a planted symlink, no shared
    # file for two writers to interleave into.
    import tempfile

    tmp_name = None
    try:
        fd, tmp_name = tempfile.mkstemp(
            dir=str(target.parent), prefix=target.name + ".", suffix=".tmp"
        )
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
        try:
            os.chmod(tmp_name, 0o600)
        except OSError:
            pass
        os.replace(tmp_name, target)
        tmp_name = None
    except OSError:
        # No direct-write fallback: a non-atomic write to the target path
        # could follow a planted symlink. The transcript is still in
        # memory; the next autosave retries, and nothing on disk is
        # half-overwritten.
        return None
    finally:
        # Clean up a staged temp file that never got promoted, if any.
        if tmp_name is not None:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
        if acquired and lock_fd is not None:
            try:
                os.close(lock_fd)
            except OSError:
                pass
            try:
                lock_path.unlink(missing_ok=True)
            except OSError:
                pass
    return str(target)


def _legacy_path(name: str) -> Path:
    """Path used before hash suffix was added, for backward compatibility."""
    safe = re.sub(r"[^a-zA-Z0-9._-]", "-", name).strip("-._")
    if not safe or safe != name:
        safe = _slug(name) or "session"
    safe = Path(safe).name
    return sessions_dir() / f"{safe}.json"


def load(name: str) -> dict[str, Any] | None:
    """Read a session by name. None when missing or unreadable."""
    # Try current hashed path first, then legacy path for old sessions
    for cand in (_path(name), _legacy_path(name)):
        try:
            if cand.stat().st_size > _MAX_SESSION_BYTES:
                continue
        except OSError:
            continue
        try:
            with open(cand, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            # ValueError covers json.JSONDecodeError and the
            # UnicodeDecodeError raised by a file in an unexpected
            # encoding; both mean "unusable session file".
            continue
        if not isinstance(data, dict) or not isinstance(data.get("messages"), list):
            continue
        return data
    return None


def delete(name: str) -> bool:
    """Remove a saved session. True only when a file was actually unlinked."""
    # Try the current hashed path first, then the legacy path.
    for cand in (_path(name), _legacy_path(name)):
        try:
            cand.unlink()
            return True
        except OSError:
            continue
    return False


def _summarise(messages: list[Any]) -> str:
    """The first thing the operator said, which is the only useful label."""
    for message in messages:
        if isinstance(message, dict) and message.get("role") == "user":
            content = message.get("content")
            if isinstance(content, list):
                # Multimodal content blocks: take the first piece of text.
                content = " ".join(
                    part.get("text", "")
                    for part in content
                    if isinstance(part, dict) and part.get("type") == "text"
                )
            text = re.sub(r"\s+", " ", str(content or "")).strip()
            if text:
                return text[:70]
    return ""


# Listing cache: (path, mtime_ns, size) -> summary metadata, so the resume
# picker does not re-parse every saved transcript on every open. Re-saved
# files get a new stat key and are re-read; the cache is size-capped.
_LISTING_CACHE: dict[tuple[str, int, int], dict[str, Any]] = {}
_LISTING_CACHE_MAX = 512


def _listing_metadata(file: Path) -> dict[str, Any] | None:
    """Summary metadata for one saved session, via the stat-keyed cache."""
    try:
        stat = file.stat()
    except OSError:
        return None
    if stat.st_size > _MAX_SESSION_BYTES:
        # Skip oversized transcripts: parsing them into memory on every
        # picker open is exactly what the size cap prevents.
        return None
    key = (str(file), stat.st_mtime_ns, stat.st_size)
    cached = _LISTING_CACHE.get(key)
    if cached is not None:
        return cached
    try:
        with open(file, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        # ValueError covers JSONDecodeError and UnicodeDecodeError so
        # one undecodable file cannot abort the whole listing.
        return None
    if not isinstance(data, dict):
        return None
    messages = data.get("messages")
    if not isinstance(messages, list) or not messages:
        return None
    metadata = {
        "name": data.get("name") or file.stem,
        "saved_at": data.get("saved_at") or "",
        "mtime": stat.st_mtime,
        "workspace": data.get("workspace") or "",
        "model": data.get("model") or "",
        "turns": sum(1 for m in messages if isinstance(m, dict)
                     and m.get("role") == "user"),  # one turn per user message
        "messages": len(messages),
        "summary": data.get("summary") or _summarise(messages),
    }
    if len(_LISTING_CACHE) >= _LISTING_CACHE_MAX:
        _LISTING_CACHE.pop(next(iter(_LISTING_CACHE)), None)
    _LISTING_CACHE[key] = metadata
    return metadata


def list_sessions(limit: int = 20) -> list[dict[str, Any]]:
    """Saved sessions, newest first, with enough metadata for a picker.

    Corrupt files are skipped rather than raised: one bad JSON file in
    the directory should not make every session unlistable.
    """
    directory = sessions_dir()
    try:
        files = list(directory.glob("*.json"))
    except OSError:  # pragma: no cover - unreadable directory
        return []

    found: list[dict[str, Any]] = []
    for file in files:
        metadata = _listing_metadata(file)
        if metadata is None:
            continue
        entry = dict(metadata)
        entry["path"] = str(file)
        found.append(entry)

    found.sort(key=lambda item: item["mtime"], reverse=True)
    return found[:limit]


def latest() -> dict[str, Any] | None:
    """The most recently saved session, or None when there are none."""
    found = list_sessions(limit=1)
    return found[0] if found else None
