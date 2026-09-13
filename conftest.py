"""Test isolation: redirect user stores and audit log to a temp directory.

Console autosave, the approval audit log, the credential store, and the
user settings file otherwise write to the real user stores; all are
pointed at fresh temp paths for the run.
"""

from __future__ import annotations

import os
import shutil
import tempfile

# Module-level store outlives any single test; cleanup happens in
# pytest_unconfigure.
_store: str | None = None


def _audit_log() -> str:
    return os.path.join(_store, "pre-tool-use.log")


def pytest_configure(config) -> None:  # noqa: ARG001
    global _store
    _store = tempfile.mkdtemp(prefix="mantra-test-sessions-")
    os.environ["MANTRA_SESSIONS"] = _store
    os.environ["MANTRA_PRE_TOOL_USE_LOG"] = _audit_log()
    # Credential and settings stores: point at (nonexistent) temp files so
    # redaction and key resolution never read the developer's real store.
    os.environ["MANTRA_CREDENTIALS"] = os.path.join(_store, "credentials.json")
    os.environ["MANTRA_SETTINGS"] = os.path.join(_store, "settings.json")
    # A stray script override in the operator environment would silently
    # replace the LLM client under test.
    os.environ.pop("MANTRA_SCRIPT", None)


def pytest_unconfigure(config) -> None:  # noqa: ARG001
    global _store
    if _store:
        shutil.rmtree(_store, ignore_errors=True)
        for name in (
            "MANTRA_SESSIONS",
            "MANTRA_PRE_TOOL_USE_LOG",
            "MANTRA_CREDENTIALS",
            "MANTRA_SETTINGS",
        ):
            os.environ.pop(name, None)
        _store = None


def pytest_runtest_setup(item) -> None:  # noqa: ARG001
    if _store:
        os.environ["MANTRA_SESSIONS"] = _store
        os.environ["MANTRA_PRE_TOOL_USE_LOG"] = _audit_log()
        os.environ["MANTRA_CREDENTIALS"] = os.path.join(_store, "credentials.json")
        os.environ["MANTRA_SETTINGS"] = os.path.join(_store, "settings.json")


def pytest_runtest_teardown(item, nextitem) -> None:  # noqa: ARG001
    # Re-assert after every test: a suite's own cleanup may have popped
    # the variables, which would leak the next test back to the real store.
    if _store:
        os.environ["MANTRA_SESSIONS"] = _store
        os.environ["MANTRA_PRE_TOOL_USE_LOG"] = _audit_log()
        os.environ["MANTRA_CREDENTIALS"] = os.path.join(_store, "credentials.json")
        os.environ["MANTRA_SETTINGS"] = os.path.join(_store, "settings.json")
