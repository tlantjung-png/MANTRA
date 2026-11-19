"""Best-effort owner-only ACL tightening on Windows.

The callers keep their own guards and error handling: this module only
owns the ``icacls`` invocation that was previously spelled out at each
call site.
"""

from __future__ import annotations


def icacls_owner_only(path: str, *, directory: bool = False) -> None:
    """Grant the current user full access and drop inherited entries."""
    import getpass
    import subprocess

    user = getpass.getuser()
    grant = f"{user}:(OI)(CI)F" if directory else f"{user}:F"
    subprocess.run(
        ["icacls", str(path), "/inheritance:r", "/grant:r", grant],
        capture_output=True,
        timeout=5,
    )
