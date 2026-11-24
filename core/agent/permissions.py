"""Best-effort owner-only ACL tightening on Windows.

The callers keep their own guards and error handling: this module only
owns the ``icacls`` invocation that was previously spelled out at each
call site.
"""

from __future__ import annotations

_ACL_WARNED = False


def icacls_owner_only(path: str, *, directory: bool = False) -> bool:
    """Grant the current user full access and drop inherited entries.

    Returns True when icacls reported success. A non-zero result is warned
    once per process instead of being discarded silently.
    """
    import getpass
    import subprocess
    import warnings

    global _ACL_WARNED
    user = getpass.getuser()
    grant = f"{user}:(OI)(CI)F" if directory else f"{user}:F"
    completed = subprocess.run(
        ["icacls", str(path), "/inheritance:r", "/grant:r", grant],
        capture_output=True,
        timeout=5,
    )
    if completed.returncode != 0:
        if not _ACL_WARNED:
            _ACL_WARNED = True
            warnings.warn(
                f"icacls could not tighten owner-only permissions on {path}; "
                "the file may remain accessible to other accounts",
                UserWarning,
                stacklevel=2,
            )
        return False
    return True
