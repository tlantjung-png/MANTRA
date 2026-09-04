"""Test isolation for the whole suite.

Console turns call ``session.autosave()``, which writes into the real
sessions store (``~/.mantra/sessions`` by default). Point ``MANTRA_SESSIONS``
at a fresh temp dir for the duration of the run so no test leaks a stray
session file into the developer's store.
"""

from __future__ import annotations

import os
import shutil
import tempfile

# Module-level store outlives any single test; cleanup happens in
# pytest_unconfigure.
_store: str | None = None


def pytest_configure(config) -> None:  # noqa: ARG001
    global _store
    _store = tempfile.mkdtemp(prefix="mantra-test-sessions-")
    os.environ["MANTRA_SESSIONS"] = _store


def pytest_unconfigure(config) -> None:  # noqa: ARG001
    global _store
    if _store:
        shutil.rmtree(_store, ignore_errors=True)
        os.environ.pop("MANTRA_SESSIONS", None)


def pytest_runtest_setup(item) -> None:  # noqa: ARG001
    if _store:
        os.environ["MANTRA_SESSIONS"] = _store


def pytest_runtest_teardown(item, nextitem) -> None:  # noqa: ARG001
    # Re-assert after every test: a suite's own cleanup may have popped
    # the variable, which would leak the next test back to the real store.
    if _store:
        os.environ["MANTRA_SESSIONS"] = _store
