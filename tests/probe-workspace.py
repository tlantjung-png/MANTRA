import os
import sys
import tempfile

# Repo root, derived from this file's location: the old hardcoded
# ``C:\Users\arif-\MANTRA\src`` checkout path no longer exists.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if not os.path.isdir(_REPO_ROOT):
    sys.exit(f"probe root missing: {_REPO_ROOT}")
sys.path.insert(0, _REPO_ROOT)
from core.console import _infer_workspace, PROJECT_ROOT  # noqa: E402


def _cwd_infer(path: str) -> str:
    """_infer_workspace from an existing directory, restoring the cwd."""
    old = os.getcwd()
    try:
        os.chdir(path)
        return _infer_workspace()
    finally:
        os.chdir(old)


# A non-repository home directory falls back to the default workspace.
print("home_infers_to_default =", _cwd_infer(os.path.expanduser("~")) == os.path.join(PROJECT_ROOT, "workspace"))

# A drive root falls back to the default workspace (Windows-only guard).
if os.name == "nt":
    drive_root = os.path.splitdrive(os.getcwd())[0] + os.sep
    print("root_infers_to_default =", _cwd_infer(drive_root) == os.path.join(PROJECT_ROOT, "workspace"))

# A foreign directory infers to itself; derived from the temp root so the
# probe stays portable and survives a missing checkout path.
foreign = tempfile.mkdtemp(prefix="probe-foreign-")
try:
    print("foreign_infers_to_itself =", _cwd_infer(foreign) == foreign)
finally:
    os.rmdir(foreign)
