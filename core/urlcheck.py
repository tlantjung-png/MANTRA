"""Repository URL and commit reference validation shared by the sandboxes.

Both the host and the container sandbox clone a task's repository and
check out an optional base commit before the agent runs, so both must
apply the same refusal rules to those two model-supplied strings. One
definition here; the sandbox classes re-export it as staticmethods so
callers and tests keep their existing seams.
"""

from __future__ import annotations

import os

# Shared refusal rules: the container and host sandboxes previously
# carried identical copies; keep them in lockstep from one place.
_SAFE_REPO_SCHEMES = ("http://", "https://", "git@", "ssh://", "git://")
_COMMIT_META_CHARS = (";", "&", "|", "`", "$", "(", ")", "<", ">", '"', "'")


def is_safe_repo_url(url: str) -> bool:
    """Whether a repository address is allowed for git clone.

    File scheme is disabled by default because it allows reading
    arbitrary local paths. Enable only for tests via the
    MANTRA_ALLOW_FILE_URL environment variable.
    """
    url = url.strip()
    if not url or len(url) > 2048 or "\n" in url or "\r" in url or "\x00" in url:
        return False
    if url.startswith(_SAFE_REPO_SCHEMES):
        return True
    if url.startswith("file://"):
        return bool(os.environ.get("MANTRA_ALLOW_FILE_URL"))
    return False


def is_safe_commit(commit: str) -> bool:
    """Whether a commit-ish is safe to hand to ``git checkout``.

    Shell metacharacters are refused: the reference must never be able
    to inject commands into the checkout invocation.
    """
    commit = commit.strip()
    if not commit or len(commit) > 256 or "\n" in commit or "\r" in commit or "\x00" in commit:
        return False
    if any(c in commit for c in _COMMIT_META_CHARS):
        return False
    return True
