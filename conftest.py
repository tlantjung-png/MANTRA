"""Test isolation for the whole suite.

Console turns call ``session.autosave()``, which writes into the real
sessions store (``~/.mantra/sessions`` by default), and
``ApprovalPolicy.check`` appends to the real pre-tool-use audit log.
Point ``MANTRA_SESSIONS`` and ``MANTRA_PRE_TOOL_USE_LOG`` at fresh temp
paths for the duration of the run so no test leaks a stray file into the
developer's store or logs.
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


def pytest_unconfigure(config) -> None:  # noqa: ARG001
    global _store
    if _store:
        shutil.rmtree(_store, ignore_errors=True)
        os.environ.pop("MANTRA_SESSIONS", None)
        os.environ.pop("MANTRA_PRE_TOOL_USE_LOG", None)
        _store = None


def pytest_runtest_setup(item) -> None:  # noqa: ARG001
    if _store:
        os.environ["MANTRA_SESSIONS"] = _store
        os.environ["MANTRA_PRE_TOOL_USE_LOG"] = _audit_log()


def pytest_runtest_teardown(item, nextitem) -> None:  # noqa: ARG001
    # Re-assert after every test: a suite's own cleanup may have popped
    # the variable, which would leak the next test back to the real store.
    if _store:
        os.environ["MANTRA_SESSIONS"] = _store
        os.environ["MANTRA_PRE_TOOL_USE_LOG"] = _audit_log()
