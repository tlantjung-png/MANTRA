"""Shared stale-lock breaking: verified two-stat removal, never raises."""

from __future__ import annotations

import os
import time

from core.locking import break_stale_lock


def _make_lock(tmp_path, age_seconds: float) -> str:
    path = str(tmp_path / "store.json.lock")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("1\n")
    old = time.time() - age_seconds
    os.utime(path, (old, old))
    return path


def test_fresh_lock_is_not_broken(tmp_path) -> None:
    """A lock younger than the threshold is left alone."""
    path = _make_lock(tmp_path, age_seconds=1.0)
    assert break_stale_lock(path, stale_seconds=5.0) is False
    assert os.path.exists(path)


def test_stale_lock_is_broken(tmp_path) -> None:
    path = _make_lock(tmp_path, age_seconds=30.0)
    assert break_stale_lock(path, stale_seconds=5.0) is True
    assert not os.path.exists(path)


def test_boundary_age_is_broken(tmp_path) -> None:
    """Exactly at the threshold counts as stale (the check is strict <)."""
    path = _make_lock(tmp_path, age_seconds=5.0)
    assert break_stale_lock(path, stale_seconds=5.0) is True
    assert not os.path.exists(path)


def test_missing_lock_reports_false(tmp_path) -> None:
    """Nothing to break: False, matching the original helpers."""
    missing = str(tmp_path / "nope.lock")
    assert break_stale_lock(missing, stale_seconds=5.0) is False


def test_vanishing_lock_between_stats_reports_true(tmp_path, monkeypatch) -> None:
    """A lock that disappears between the two stats: True (cleared elsewhere)."""
    import core.locking as locking_module

    path = _make_lock(tmp_path, age_seconds=30.0)
    real_stat = os.stat
    calls = {"n": 0}

    def _vanishing_stat(target, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise FileNotFoundError(target)
        return real_stat(target, *args, **kwargs)

    monkeypatch.setattr(locking_module.os, "stat", _vanishing_stat)
    try:
        assert break_stale_lock(path, stale_seconds=5.0) is True
    finally:
        monkeypatch.undo()
    # os.remove never ran, so the file survives; the caller's retry loop
    # re-checks existence anyway.
    assert os.path.exists(path)


def test_lock_refreshed_between_stats_is_not_broken(tmp_path, monkeypatch) -> None:
    """A mtime change between the stats means a live holder: leave it."""
    import core.locking as locking_module

    path = _make_lock(tmp_path, age_seconds=30.0)
    real_stat = os.stat
    calls = {"n": 0}

    def _refreshing_stat(target, *args, **kwargs):
        calls["n"] += 1
        result = real_stat(target, *args, **kwargs)
        if calls["n"] == 1:
            # Simulate another writer touching the lock right after the
            # first stat: the second stat must see a different mtime.
            fresh = time.time()
            os.utime(path, (fresh, fresh))
        return result

    monkeypatch.setattr(locking_module.os, "stat", _refreshing_stat)
    try:
        assert break_stale_lock(path, stale_seconds=5.0) is False
    finally:
        monkeypatch.undo()
    assert os.path.exists(path)


def test_removal_failure_reports_false(tmp_path, monkeypatch) -> None:
    """An OSError from os.remove degrades to False, never raises."""
    import core.locking as locking_module

    path = _make_lock(tmp_path, age_seconds=30.0)

    def _failing_remove(target):
        raise OSError("permission denied")

    monkeypatch.setattr(locking_module.os, "remove", _failing_remove)
    try:
        assert break_stale_lock(path, stale_seconds=5.0) is False
    finally:
        monkeypatch.undo()
    assert os.path.exists(path)
