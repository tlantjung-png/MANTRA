"""Assemble system prompt from base, env, failures, memory, instructions."""

from __future__ import annotations

import os
import platform
import re
import subprocess
import tempfile
import threading
import time
from typing import Any

_memory_lock = threading.Lock()

# Lock wait and stale thresholds for memory writes. The wait is a little
# longer than the other stores because append_memory runs on the
# interactive turn path, where brief contention is worth absorbing.
_LOCK_WAIT_SECONDS = 1.5
_LOCK_STALE_SECONDS = 10.0

MEMORY_CAP_CHARS = 8000
INSTRUCTIONS_CAP_CHARS = 4000
INSTRUCTION_FILENAMES = ("AGENTS.md", "CLAUDE.md", ".mantra-instructions.md")


def find_instructions_file(workspace: str) -> str | None:
    """First instruction file present at the workspace root, if any."""
    for name in INSTRUCTION_FILENAMES:
        candidate = os.path.join(workspace, name)
        if os.path.isfile(candidate):
            return candidate
    return None


def render_environment(workspace: str) -> str:
    """Collect host and workspace facts for correct command choice."""
    lines = [
        f"- date: {time.strftime('%Y-%m-%d')}",
        f"- os: {platform.system()} {platform.release()}",
        f"- shell: {_shell_name()}",
        f"- python: {platform.python_version()}",
        f"- workspace: {workspace}",
    ]
    repo_state, flag = _git_checked(workspace, "rev-parse", "--is-inside-work-tree")
    if repo_state == "ok" and flag == "true":
        branch_state, branch = _git_checked(workspace, "rev-parse", "--abbrev-ref", "HEAD")
        # New repo has no HEAD until first commit.
        branch = (branch or "(no commits yet)") if branch_state == "ok" else "unknown"
        dirty_state, dirty = _git_checked(workspace, "status", "--porcelain")
        if dirty_state == "ok":
            state = "clean" if not dirty else f"{len(dirty.strip().splitlines())} modified"
        else:
            # Avoid guessing clean when probe failed.
            state = "state unknown (status probe failed)"
        lines.append(f"- git: branch {branch} ({state})")
    elif repo_state == "error":
        lines.append("- git: state unknown (probe failed)")
    else:
        lines.append("- git: not a repository")
    return "\n".join(lines)


def _shell_name() -> str:
    if os.name == "nt":
        return "cmd.exe via subprocess (PowerShell available; no Unix coreutils)"
    return os.environ.get("SHELL", "/bin/sh")


def _git(workspace: str, *args: str) -> str:
    """Run git; return output or empty on failure. Use _git_checked if reason matters."""
    ok, output = _git_checked(workspace, *args)
    return output if ok else ""


def _git_checked(workspace: str, *args: str) -> tuple[str, str]:
    """Run git; return (ok/no/error, output)."""
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=workspace,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return "error", ""
    if completed.returncode != 0:
        return "no", ""
    return "ok", completed.stdout.strip()


def assemble_system_prompt(
    base_prompt: str,
    known_failures_path: str | None = None,
    memory_path: str | None = None,
    instructions_path: str | None = None,
    environment: str | None = None,
) -> str:
    """Append optional sections when present."""
    sections = [base_prompt]

    if environment:
        sections.append("## Environment (facts, do not guess)\n\n" + environment.strip())

    kf_text = _read_capped(known_failures_path, MEMORY_CAP_CHARS)
    if kf_text:
        sections.append(
            "## Known-failure classes (never repeat these)\n\n" + kf_text.strip()
        )

    mem_text = read_active_memory_tail(memory_path, MEMORY_CAP_CHARS)
    if mem_text:
        sections.append(
            "## Workspace memory (durable notes from earlier sessions)\n\n"
            + mem_text.strip()
        )

    instr_text = _read_capped(instructions_path, INSTRUCTIONS_CAP_CHARS)
    if instr_text:
        sections.append(
            "## Project instructions (from the workspace itself; follow these)\n\n"
            + instr_text.strip()
        )

    result = "\n\n".join(sections)
    # Cap total size to prevent blow-up.
    TOTAL_CAP = 20000
    if len(result) > TOTAL_CAP:
        result = result[:TOTAL_CAP] + "\n... [truncated]"
    return result


def _read_capped(path: str | None, cap: int) -> str:
    if not path or not os.path.isfile(path):
        return ""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read(cap)
    except OSError:
        return ""


def _read_tail(path: str | None, cap: int) -> str:
    """Keep tail; newest entries are last."""
    if not path or not os.path.isfile(path):
        return ""
    try:
        size = os.path.getsize(path)
        if size > cap * 2:
            # Avoid loading huge file; seek near tail.
            with open(path, "rb") as handle:
                handle.seek(max(0, size - cap - 500))
                data = handle.read(cap + 1000)
                content = data.decode("utf-8", errors="replace")
                # If we started mid-file, drop the partial first line.
                if "\n" in content:
                    content = content.split("\n", 1)[-1]
                if len(content) > cap:
                    content = content[-cap:]
                    content = content.split("\n", 1)[-1]
                return content
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            content = handle.read()
    except OSError:
        return ""
    if len(content) > cap:
        # Cut at line boundary to avoid half-entry.
        content = content[-cap:]
        content = content.split("\n", 1)[-1]
    return content


def append_memory(memory_path: str | None, text: str, cap: int = MEMORY_CAP_CHARS) -> bool:
    """Append entry, prune oldest lines, atomic write."""
    if not memory_path or not text.strip():
        return False
    entry = text.rstrip() + "\n"
    # Serialize concurrent appends in this process.
    with _memory_lock:
        existing = _read_file(memory_path)
        combined = (existing + "\n" + entry).lstrip("\n") if existing else entry
        while len(combined) > cap:
            lines = combined.split("\n")
            combined = "\n".join(lines[1:])
            if "\n" not in combined and len(combined) > cap:
                combined = combined[-cap:]
                break
        parent = os.path.dirname(memory_path)
        if parent:
            try:
                os.makedirs(parent, exist_ok=True)
                try:
                    os.chmod(parent, 0o700)
                except OSError:
                    pass
            except OSError:
                pass
        # Inter-process lock via file (best effort).
        lock_path = memory_path + ".lock"
        lock_acquired = False
        lock_handle = None
        try:
            # Opportunistically break a stale lock, but treat exclusive create
            # as the arbiter; stale removal is verified to avoid deleting a
            # freshly created lock.
            if os.path.exists(lock_path):
                _break_stale_lock(lock_path)
            # Brief lock wait on interactive thread.
            start = time.monotonic()
            while time.monotonic() - start < _LOCK_WAIT_SECONDS:
                try:
                    fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                    lock_handle = fd
                    lock_acquired = True
                    break
                except FileExistsError:
                    time.sleep(0.02)
                    try:
                        if time.time() - os.stat(lock_path).st_mtime >= _LOCK_STALE_SECONDS:
                            _break_stale_lock(lock_path)
                    except OSError:
                        pass
                except OSError:
                    break
            if not lock_acquired:
                # Another process holds the lock: skip the write rather
                # than race it. The entry is dropped this turn instead of
                # two writers clobbering each other's lines.
                return False
            # Re-read before write to avoid lost update (even without lock).
            fresh = _read_file(memory_path)
            if fresh != existing:
                combined = (fresh + "\n" + entry).lstrip("\n") if fresh else entry
                while len(combined) > cap:
                    lines = combined.split("\n")
                    combined = "\n".join(lines[1:])
                    if "\n" not in combined and len(combined) > cap:
                        combined = combined[-cap:]
                        break
            # Unique temp name in the same directory: no fixed path for a
            # planted symlink to hijack.
            try:
                fd_tmp, tmp_path = tempfile.mkstemp(
                    dir=os.path.dirname(memory_path) or ".",
                    prefix=os.path.basename(memory_path) + ".",
                    suffix=".tmp",
                )
            except OSError:
                # Temp creation failed: the fallback below still applies,
                # so no NameError may escape through the finally block.
                tmp_path = None
            if tmp_path is not None:
                try:
                    with os.fdopen(fd_tmp, "w", encoding="utf-8", newline="\n") as handle:
                        handle.write(combined)
                    try:
                        os.chmod(tmp_path, 0o600)
                    except OSError:
                        pass
                    os.replace(tmp_path, memory_path)
                    return True
                except OSError:
                    # No direct-write fallback: a non-atomic write to the
                    # target path could follow a planted symlink. The
                    # entry is dropped this turn; nothing on disk is
                    # half-overwritten.
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass
                    return False
        finally:
            if lock_acquired and lock_handle is not None:
                try:
                    os.close(lock_handle)
                except OSError:
                    pass
                try:
                    os.remove(lock_path)
                except OSError:
                    pass
    return False


def _break_stale_lock(lock_path: str) -> bool:
    """Remove a lock whose holder is no longer around to remove it."""
    try:
        stat = os.stat(lock_path)
        age = time.time() - stat.st_mtime
        if age < _LOCK_STALE_SECONDS:
            return False
        # Verify mtime hasn't changed since we checked to avoid deleting
        # a lock that was just freshly created by another process.
        try:
            stat2 = os.stat(lock_path)
            if stat2.st_mtime != stat.st_mtime:
                return False
            os.remove(lock_path)
            return True
        except FileNotFoundError:
            return True
    except OSError:
        return False


def _read_file(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError:
        return ""


# ------------------------------------------------------------ memory entries
# Durable entries carry trailing lifecycle metadata:
#   "- 2026-09-06 12:00 | task-id | reason: text | status=active"
# The dedupe/supersede writer and the filtered reads below share this
# shape (adapted from the OKF search-before-write / status / stale ideas).

_STOP_WORDS = frozenset({
    "a", "an", "the", "and", "or", "but", "for", "with", "from", "this",
    "that", "was", "were", "been", "are", "is", "has", "have", "had",
    "will", "would", "should", "could", "can", "not", "its", "into",
    "than", "then", "there", "about", "after", "before", "during", "each",
    "other", "some", "such", "their", "these", "those", "when", "where",
    "which", "while", "your", "you", "they", "them", "been", "being",
})

_META_RE = re.compile(r"\|\s*(?:status=([a-z]+)|stale=(\d{4}-\d{2}-\d{2})|source=([^\s|]+))")


def _cap_body(body: str, cap: int) -> str:
    if len(body) <= cap:
        return body
    lines = body.split("\n")
    while len("\n".join(lines)) > cap and len(lines) > 1:
        lines = lines[1:]
    return "\n".join(lines)[-cap:]


def parse_entries(text: str) -> list[dict]:
    """Parse a memory block into entries with their lifecycle metadata."""
    entries = []
    for line in text.splitlines():
        if not line.startswith("- "):
            continue
        status, stale, source = "active", None, ""
        for st, sl, src in _META_RE.findall(line):
            if st:
                status = st
            if sl:
                stale = sl
            if src:
                source = src
        entries.append({"line": line, "status": status, "stale": stale, "source": source})
    return entries


def active_entries(text: str) -> list[dict]:
    """Non-superseded, non-stale entries, newest last."""
    today = time.strftime("%Y-%m-%d")
    return [
        e for e in parse_entries(text)
        if e["status"] != "superseded" and not (e["stale"] and e["stale"] <= today)
    ]


def _words(text: str) -> set[str]:
    lowered = (text or "").lower()
    return {w for w in re.findall(r"[a-z0-9_]{4,}", lowered) if w not in _STOP_WORDS}


def _line_words(line: str) -> set[str]:
    """Significant words of a memory line's CONTENT.

    Metadata is excluded: the date, task-id tokens, key=value markers and
    the ``done:``/``error:`` reason prefix carry no topic signal and would
    otherwise make every entry look related.
    """
    content: list[str] = []
    for part in line.split(" | ")[1:]:
        p = part.strip()
        if re.match(r"^(status|stale|source)=", p):
            continue
        if re.match(r"^[A-Za-z0-9]+(?:-[A-Za-z0-9]+)+$", p):
            continue  # task ids like console-42
        p = re.sub(r"^[a-z_]+\s*:", "", p)  # drop the "done:" reason prefix
        content.append(p)
    return _words(" ".join(content))


def plan_memory_write(existing: str, entry_line: str) -> tuple[str, str]:
    """Dedupe/supersede plan for a new memory entry.

    Returns ``(action, updated_existing)``:
      "skip"      - a near-duplicate of the newest active entry (same
                    topic, mostly the same words): nothing is written.
      "supersede" - the same topic as a recent active entry with new
                    content: that entry gets a ``status=superseded``
                    marker so the new one replaces it.
      "append"    - a fresh topic: write as usual.
    """
    entries = active_entries(existing)
    if not entries:
        return "append", existing
    new_words = _words(entry_line)
    if not new_words:
        return "append", existing
    for e in reversed(entries):
        old_words = _line_words(e["line"])
        if not old_words:
            continue
        overlap = len(new_words & old_words)
        smaller = min(len(new_words), len(old_words))
        if overlap >= 3 and smaller and overlap >= 0.7 * smaller:
            return "skip", existing
        if overlap >= 2:
            marked = e["line"] + " | status=superseded"
            return "supersede", existing.replace(e["line"], marked, 1)
    return "append", existing


def read_raw_tail(path: str | None, cap: int = MEMORY_CAP_CHARS) -> str:
    """The raw newest entries (superseded ones included), for planning."""
    return _read_tail(path, cap * 2) if path else ""


def read_active_memory_tail(path: str | None, cap: int = MEMORY_CAP_CHARS) -> str:
    """The newest memory entries still in force, for prompt injection."""
    if not path:
        return ""
    tail = _read_tail(path, cap * 2)
    if not tail:
        return ""
    entries = active_entries(tail)
    if not entries:
        return ""
    body = "\n".join(e["line"] for e in entries[-50:])
    return _cap_body(body, cap)


def relevant_memory(path: str | None, query: str, cap: int = 4000, extra: int = 5) -> str:
    """Keyword-ranked memory for one request.

    Progressive disclosure: the newest entries already ride in the base
    prompt, so this adds the older entries the request actually touches,
    matched by significant-word overlap and capped.
    """
    if not query or not path or not os.path.isfile(path):
        return ""
    tail = _read_tail(path, cap * 4)
    if not tail:
        return ""
    entries = active_entries(tail)
    if not entries:
        return ""
    query_words = _words(query)
    # The newest entries ride in the base prompt; with a larger ledger
    # skip them here, otherwise keep everything (small memories have
    # nothing to spare).
    pool = entries[:-extra] if len(entries) > extra * 2 else entries
    scored: list[tuple[int, str]] = []
    for e in pool:
        words = _line_words(e["line"])
        if not words:
            continue
        overlap = len(query_words & words)
        if overlap:
            scored.append((overlap, e["line"]))
    scored.sort(key=lambda item: (-item[0], item[1]))
    body = "\n".join(line for _, line in scored[:5])
    return _cap_body(body, cap)


def rewrite_memory(memory_path: str | None, body: str, new_entry: str | None = None,
                   cap: int = MEMORY_CAP_CHARS) -> bool:
    """Write an explicit memory body (used to mark an entry superseded),
    then append the new entry. Mirrors append_memory's locked, atomic
    write path so the supersede lands without racing a concurrent append.
    """
    if not memory_path:
        return False
    combined = body.strip() + "\n" + (new_entry.strip() + "\n" if new_entry else "")
    combined = _cap_body(combined, cap)

    parent = os.path.dirname(memory_path)
    if parent:
        try:
            os.makedirs(parent, exist_ok=True)
            try:
                os.chmod(parent, 0o700)
            except OSError:
                pass
        except OSError:
            pass
    lock_path = memory_path + ".lock"
    lock_acquired = False
    lock_handle = None
    try:
        if os.path.exists(lock_path):
            _break_stale_lock(lock_path)
        start = time.monotonic()
        while time.monotonic() - start < _LOCK_WAIT_SECONDS:
            try:
                fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                lock_handle = fd
                lock_acquired = True
                break
            except FileExistsError:
                time.sleep(0.02)
                try:
                    if time.time() - os.stat(lock_path).st_mtime >= _LOCK_STALE_SECONDS:
                        _break_stale_lock(lock_path)
                except OSError:
                    pass
            except OSError:
                break
        if not lock_acquired:
            return False
        try:
            fd_tmp, tmp_path = tempfile.mkstemp(
                dir=os.path.dirname(memory_path) or ".",
                prefix=os.path.basename(memory_path) + ".",
                suffix=".tmp",
            )
        except OSError:
            return False
        try:
            with os.fdopen(fd_tmp, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(combined)
            try:
                os.chmod(tmp_path, 0o600)
            except OSError:
                pass
            os.replace(tmp_path, memory_path)
            return True
        except OSError:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            return False
    finally:
        if lock_acquired and lock_handle is not None:
            try:
                os.close(lock_handle)
            except OSError:
                pass
            try:
                os.remove(lock_path)
            except OSError:
                pass
    return False
