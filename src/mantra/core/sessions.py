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
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")


def derive_name(workspace: str = "", model: str = "") -> str:
    """A name for a session the operator never bothered to name.

    ``C:\\Users\\arif-\\K-CHAT`` -> ``k-chat-20260829-0812``. The stamp
    keeps two sessions from the same directory from colliding, and it
    sorts usefully because it reads year-month-day.
    """
    import uuid

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
    import hashlib
    safe = re.sub(r"[^a-zA-Z0-9._-]", "-", name).strip("-._")
    original = name
    if not safe or safe != name:
        # Use slug fallback but incorporate hash of original for uniqueness
        slug_base = _slug(name) or "session"
        # Short hash of original name to disambiguate different unsafe inputs
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
    content = json.dumps(record, ensure_ascii=False, indent=2)
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
        try:
            with open(target, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(content)
            try:
                os.chmod(target, 0o600)
            except OSError:
                pass
        except OSError:
            return None
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
            with open(cand, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict) or not isinstance(data.get("messages"), list):
            continue
        return data
    return None


def delete(name: str) -> bool:
    # Try current path, then legacy
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
        try:
            with open(file, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        messages = data.get("messages")
        if not isinstance(messages, list) or not messages:
            continue
        try:
            stamp = file.stat().st_mtime
        except OSError:  # pragma: no cover - race with deletion
            stamp = 0.0
        found.append(
            {
                "name": data.get("name") or file.stem,
                "saved_at": data.get("saved_at") or "",
                "mtime": stamp,
                "workspace": data.get("workspace") or "",
                "model": data.get("model") or "",
                "turns": sum(1 for m in messages if isinstance(m, dict)
                             and m.get("role") == "user"),
                "messages": len(messages),
                "summary": data.get("summary") or _summarise(messages),
                "path": str(file),
            }
        )

    found.sort(key=lambda item: item["mtime"], reverse=True)
    return found[:limit]


def latest() -> dict[str, Any] | None:
    """The most recently saved session, or None when there are none."""
    found = list_sessions(limit=1)
    return found[0] if found else None
