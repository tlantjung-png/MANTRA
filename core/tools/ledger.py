"""Ledger: enforce read-before-edit via content hash."""

from __future__ import annotations

import hashlib
import os


def content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _detect_case_insensitive_fs() -> bool:
    """True when the local filesystem ignores path case.

    Windows is always case-insensitive. Elsewhere the answer is probed once
    by looking up a case-flipped variant of this module's own path: it
    resolves on a case-insensitive volume and does not on a case-sensitive
    one.
    """
    if os.path.normcase("A") == "a":
        return True
    try:
        module = os.path.abspath(__file__)
        folder, name = os.path.dirname(module), os.path.basename(module)
        flipped = name.swapcase()
        if flipped != name and os.path.exists(os.path.join(folder, flipped)):
            return True
    except OSError:
        pass
    return False


_CASE_INSENSITIVE = _detect_case_insensitive_fs()


class EditLedger:
    """Per-session path -> hash of last seen content, with partial flag."""

    def __init__(self) -> None:
        self._seen: dict[str, str] = {}
        self._partial: set[str] = set()

    def remember(self, path: str, content: str) -> None:
        """Record content hash for path (full view)."""
        self._seen[self._key(path)] = content_hash(content)
        self._partial.discard(self._key(path))

    def remember_partial(self, path: str, is_partial: bool) -> None:
        """Mark whether last view was partial."""
        key = self._key(path)
        if is_partial:
            self._partial.add(key)
        else:
            self._partial.discard(key)

    def has_seen(self, path: str) -> bool:
        return self._key(path) in self._seen

    def is_current(self, path: str, content: str) -> bool:
        return self._seen.get(self._key(path)) == content_hash(content)

    def is_partial(self, path: str) -> bool:
        return self._key(path) in self._partial

    def forget_all(self) -> None:
        # Called by the console at the start of each turn.
        self._seen.clear()
        self._partial.clear()

    @staticmethod
    def _key(path: str) -> str:
        """Normalize path to one key per file.

        Case is folded only where the platform's filesystem is
        case-insensitive, matching how the read tool keys its own
        per-file state.
        """
        import posixpath

        normalized = path.replace("\\", "/")
        # Collapses redundant separators and ./; trailing slashes are
        # stripped and stay stripped, so a dir and same-named file share
        # one key.
        normalized = posixpath.normpath(normalized)
        # normpath turns "" into ".", restore empty
        if normalized == ".":
            normalized = ""
        # Remove leading ./ that normpath may leave as "./a"
        if normalized.startswith("./"):
            normalized = normalized[2:]
        if _CASE_INSENSITIVE:
            normalized = normalized.lower()
        return normalized
