"""Shared path resolution: project root, bundled data, and input paths."""

from __future__ import annotations

import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def resolve_input_path(path: str) -> str:
    """Resolve an input path against cwd, then the project root."""
    if os.path.exists(path):
        return path
    candidate = os.path.join(PROJECT_ROOT, path)
    return candidate if os.path.exists(candidate) else path


def _resolve_data_path(*parts: str) -> str:
    """Locate a bundled data file in a source tree or an installed wheel.

    Installed wheels place data-files under ``sys.prefix`` while the code
    sits in site-packages, so a single PROJECT_ROOT-relative path would
    miss them (installed console could not start with its default config).
    """
    candidates = [
        os.path.join(PROJECT_ROOT, *parts),
        os.path.join(os.path.dirname(PROJECT_ROOT), *parts),
        os.path.join(sys.prefix, *parts),
    ]
    for cand in candidates:
        if os.path.isfile(cand):
            return cand
    return candidates[0]
