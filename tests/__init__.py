"""MANTRA test suite. Run with: python -m pytest tests/ -q (preferred).

conftest.py redirects the sessions and audit-log stores per test for
pytest. Plain ``python -m unittest discover -s tests`` works too but
skips that harness, so the same redirection is applied here at import
time for every ``MANTRA_*`` storage variable. ``setdefault`` semantics
keep a value a test module sets itself in charge of its own store.
"""

from __future__ import annotations

import atexit
import os
import shutil
import tempfile

# Storage variables the console and agent modules read from the
# environment. Redirected to one scratch tree so neither runner can
# reach the operator's real ~/.mantra state.
_STORAGE_ENV = (
    "MANTRA_SESSIONS",
    "MANTRA_PRE_TOOL_USE_LOG",
    "MANTRA_SETTINGS",
    "MANTRA_CREDENTIALS",
    "MANTRA_WORKFLOWS",
    "MANTRA_SKILLS",
    "MANTRA_RULES_FILE",
)


def _isolate_storage() -> str:
    """Point the MANTRA_* storage variables at a fresh scratch tree.

    setdefault semantics: a variable a test module already set (or the
    conftest's pytest_configure redirect) keeps its value; only unset
    variables are redirected, so per-module overrides always win.
    """
    root = tempfile.mkdtemp(prefix="mantra-test-env-")
    for name in _STORAGE_ENV:
        if not os.environ.get(name):
            os.environ.setdefault(name, os.path.join(root, name.lower()))
    return root


# One tree per process; removed at interpreter exit.
_ENV_ROOT = _isolate_storage()
atexit.register(shutil.rmtree, _ENV_ROOT, True)
