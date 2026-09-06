import os
import sys

# Repo root, derived from this file's location: the old hardcoded
# ``C:\Users\arif-\MANTRA\src`` checkout path no longer exists.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if not os.path.isdir(_REPO_ROOT):
    sys.exit(f"probe root missing: {_REPO_ROOT}")
sys.path.insert(0, _REPO_ROOT)
from core.console import _infer_workspace, PROJECT_ROOT

# A non-repository home directory falls back to the default workspace.
os.chdir(os.path.expanduser("~"))
print("home_infers_to_default =", _infer_workspace() == os.path.join(PROJECT_ROOT, "workspace"))

os.chdir("C:\\")
print("root_infers_to_default =", _infer_workspace() == os.path.join(PROJECT_ROOT, "workspace"))

# A foreign directory infers to itself.
os.chdir(r"C:\Users\arif-\K-CHAT")
print("kchat_infers_to_kchat =", _infer_workspace() == r"C:\Users\arif-\K-CHAT")
