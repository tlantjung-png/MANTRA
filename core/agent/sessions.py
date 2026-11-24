"""Sessions: one JSON per saved conversation."""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any

from core.locking import acquire_lock, release_lock

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
    # Lowercase alphanumeric runs joined by hyphens; shared by all name paths.
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
    # Sanitize against traversal; the hash keeps distinct unsafe names
    # from collapsing to one slug. Lowercase up front so Foo and foo do
    # not collide on a case-insensitive filesystem.
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


# Cap each message: without a ceiling the transcript and per-turn cost
# grow with session length. Trim long output rather than drop the message,
# which could orphan the tool call it answers.
_MAX_MESSAGE_CHARS = 20_000

# Sessions over this size are unusable as context and too costly to parse
# on every listing; matches the console's explicit-load guard so /sessions
# cannot bypass it by reading the file directly.
_MAX_SESSION_BYTES = 10_000_000

# Inter-process save lock: an exclusive create is the arbiter, matching
# the settings and workflow stores.
_LOCK_STALE_SECONDS = 5.0
_LOCK_WAIT = 0.5

# Observable autosave state. A single warning is easy to miss in a long
# session, so failures re-warn at a bounded interval and recovery is announced.
_save_failures = 0
_last_save_ok = True
_last_save_warning = 0.0
_REWARN_SECONDS = 120.0


def _note_save_result(ok: bool) -> None:
    """Track save outcome and warn on transition and on a long failure run."""
    global _save_failures, _last_save_ok, _last_save_warning
    if ok:
        if not _last_save_ok:
            print("[sessions] autosave recovered", file=sys.stderr)
        _save_failures = 0
        _last_save_ok = True
        return
    _last_save_ok = False
    _save_failures += 1
    now = time.monotonic()
    if _save_failures == 1 or now - _last_save_warning >= _REWARN_SECONDS:
        _last_save_warning = now
        print(
            "[sessions] autosave failed; the session will not be saved until writes recover",
            file=sys.stderr,
        )


def save_health() -> dict[str, Any]:
    """Current autosave state, for a status row or diagnostics."""
    return {"ok": _last_save_ok, "consecutive_failures": _save_failures}


def _release(lock_path: Path, lock_fd: int | None) -> None:
    """Release a held lock without masking the save result (Low-23)."""
    try:
        release_lock(lock_path, lock_fd)
    except OSError:
        pass


def _trim_messages(messages: list[Any]) -> list[Any]:
    """Cap each message's content so the transcript stays bounded."""
    trimmed: list[Any] = []
    for message in messages:
        if not isinstance(message, dict):
            trimmed.append(message)
            continue
        content = message.get("content")
        if isinstance(content, list):
            # Multimodal content: cap each text block so a huge block
            # list cannot survive into the transcript.
            blocks = list(content)
            changed = False
            for i, block in enumerate(blocks):
                if (
                    isinstance(block, dict)
                    and isinstance(block.get("text"), str)
                    and len(block["text"]) > _MAX_MESSAGE_CHARS
                ):
                    copy = dict(block)
                    copy["text"] = block["text"][:_MAX_MESSAGE_CHARS].rstrip() + "\n... [truncated on save]"
                    blocks[i] = copy
                    changed = True
            if changed:
                copy = dict(message)
                copy["content"] = blocks
                trimmed.append(copy)
            else:
                trimmed.append(message)
            continue
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
    target = _path(name)
    # Create the session directory with restricted permissions.
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(target.parent, 0o700)
        except OSError:
            pass
    except OSError:
        pass
    # Hold the inter-process lock across build-and-write so two autosaves
    # cannot interleave; if it cannot be taken, skip the save rather than
    # overwrite a good transcript with a half-written one.
    lock_path = target.with_suffix(target.suffix + ".lock")
    lock_fd = acquire_lock(lock_path, _LOCK_WAIT, _LOCK_STALE_SECONDS)
    if lock_fd is None:
        _note_save_result(False)
        return None
    import tempfile

    tmp_name = None
    try:
        record = {
            # Caller payload first so canonical metadata cannot be spoofed.
            **payload,
            "version": _VERSION,
            "name": name,
            "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        messages = record.get("messages")
        if isinstance(messages, list):
            record["messages"] = _trim_messages(messages)
        try:
            content = json.dumps(record, ensure_ascii=False, indent=2)
        except (TypeError, ValueError):
            # A payload value json cannot encode must not raise out of save():
            # the caller autosaves every turn, and one bad value is not worth
            # losing the session over; skip the write instead.
            _note_save_result(False)
            return None
        # Unique temp name: no fixed path for a planted symlink, no shared
        # file for two writers to interleave into.
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
        _note_save_result(True)
        return str(target)
    except OSError:
        # No direct-write fallback (could follow a symlink); the in-memory
        # transcript retries on the next autosave and the target stays whole.
        _note_save_result(False)
        return None
    finally:
        # Clean up a staged temp file that never got promoted, if any.
        if tmp_name is not None:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
        _release(lock_path, lock_fd)


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
# Guards cache mutations: the TUI and console may list concurrently.
_LISTING_CACHE_LOCK = threading.Lock()


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
    with _LISTING_CACHE_LOCK:
        # Pop-and-reinsert on access orders the dict coldest-first, so the
        # insert-time eviction below really drops the least recently used.
        cached = _LISTING_CACHE.pop(key, None)
        if cached is not None:
            _LISTING_CACHE[key] = cached
    if cached is not None:
        return dict(cached)
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
    with _LISTING_CACHE_LOCK:
        if len(_LISTING_CACHE) >= _LISTING_CACHE_MAX:
            _LISTING_CACHE.pop(next(iter(_LISTING_CACHE)), None)
        _LISTING_CACHE[key] = dict(metadata)
    return dict(metadata)


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
