"""Shared naming for corrupt-store backups."""

from __future__ import annotations

from pathlib import Path


def unique_corrupt_path(path: Path, stamp: str) -> Path | None:
    """Pick the next backup path without replacing an existing backup."""
    backup = path.with_suffix(path.suffix + f".corrupt-{stamp}")
    counter = 1
    while backup.exists():
        backup = path.with_suffix(path.suffix + f".corrupt-{stamp}-{counter}")
        counter += 1
        if counter > 100:
            return None
    return backup
