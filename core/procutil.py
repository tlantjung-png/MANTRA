"""Process helpers shared by the sandboxes and the command tools.

One definition of the process-group spawn flags, the capped pipe
reader, and the tree-kill helper: the host sandbox, the container
sandbox, and the background command tools previously carried their own
copies of all three, which had started to drift (catch breadth on the
taskkill call, docstring wording) and had to be fixed in lockstep.
"""

from __future__ import annotations

import os
import signal
import subprocess
import threading

# Spawn each child in its own process group/session so a timeout or abort
# can kill the whole tree (grandchildren included), not just the direct
# child — otherwise survivors keep the stdout/stderr pipes open and the
# reader threads never see EOF.
POPEN_GROUP_KWARGS: dict = {}
if os.name == "nt":
    POPEN_GROUP_KWARGS["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
else:
    POPEN_GROUP_KWARGS["start_new_session"] = True


def read_pipe_into(stream, buf: bytearray, cap: int, done: threading.Event) -> None:
    """Append streamed bytes to ``buf`` until EOF or ``cap``; set ``done``.

    Runs in a daemon thread so the main loop keeps watching the deadline
    and the abort signal while output is read incrementally; the cap keeps
    memory bounded for chatty commands.
    """
    try:
        while len(buf) < cap:
            chunk = stream.read(cap - len(buf) + 1)
            if not chunk:
                break
            buf.extend(chunk[: cap - len(buf)])
    except (OSError, ValueError):
        pass
    finally:
        done.set()


def kill_process_tree(proc: subprocess.Popen) -> None:
    """Terminate the child and its whole tree; best effort, never raises."""
    if os.name == "nt":
        # taskkill /T walks the process tree, /F force-kills. The child
        # was spawned in its own process group, so taskkill starts there.
        try:
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                capture_output=True,
                timeout=5,
            )
        except Exception:
            pass  # taskkill unavailable; the direct proc.kill below still runs
        try:
            proc.kill()
        except OSError:
            pass
        return
    # POSIX: the child leads its own session (start_new_session), so
    # signalling the group reaches every descendant, not just the shell.
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
    except OSError:
        try:
            proc.kill()
        except OSError:
            pass
