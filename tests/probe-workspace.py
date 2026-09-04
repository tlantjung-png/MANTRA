import os
import sys

# Hardcoded checkout path: this probe only runs on this host.
sys.path.insert(0, r"C:\Users\arif-\MANTRA\src")
from mantra.console import _infer_workspace, PROJECT_ROOT

# A non-repository home directory falls back to the default workspace.
os.chdir(os.path.expanduser("~"))
print("home_infers_to_default =", _infer_workspace() == os.path.join(PROJECT_ROOT, "workspace"))

os.chdir("C:\\")
print("root_infers_to_default =", _infer_workspace() == os.path.join(PROJECT_ROOT, "workspace"))

# A foreign directory infers to itself.
os.chdir(r"C:\Users\arif-\K-CHAT")
print("kchat_infers_to_kchat =", _infer_workspace() == r"C:\Users\arif-\K-CHAT")
