"""File tools: read, write, edit, list with caps, ledger, and harness repairs.

Adapted from Command Code read-tool harness engineering:
- 3 ceilings: 2000 lines / 128KB / 2000ch per line
- Recovery notes instead of silent empty
- Dedup self-expiring cache
- Partial-view ledger
- Unicode/did-you-mean retry
"""

from __future__ import annotations

import glob
import os
import re
import shlex
import unicodedata
from typing import Any

from core.types import Sandbox
from core.types import Tool
from core.tools.search import _SKIP_DIRS

_SHELL_META_RE = re.compile(r"[;&|`$()<>]")

_MAX_READ_CHARS = 20000  # legacy cap for non-windowed callers
_MAX_WRITE_CHARS = 1_000_000

# Command Code ceilings
_LINE_WINDOW = 2000
_BYTE_BUDGET = 128 * 1024  # 128KB
_PER_LINE_CLAMP = 2000

# Cached read results are kept only below this size so the dedup cache
# stays bounded (~100 entries x 100KB worst case).
_DEDUP_CACHE_MAX_CHARS = 100_000

def _is_strict_positive_int(s: str, allow_zero: bool = False) -> bool:
    """harness Convert-StrictPositiveInt: regex ^(0|[1-9][0-9]*)$, no 2abc, no 1.5"""
    if not isinstance(s, str):
        s = str(s)
    if not re.match(r"^(0|[1-9][0-9]*)$", s):
        return False
    if not allow_zero and s == "0":
        return False
    try:
        if int(s) > 2147483647:
            return False
    except Exception:
        return False
    return True

def _is_blocked_path_harness(path: str) -> str | None:
    """harness Test-BlockedPath: device namespace, trailing dot/space, ADS, CON/PRN etc."""
    if not path or not path.strip():
        return "path is empty"
    if path.startswith("\\\\?\\") or path.startswith("\\\\.\\"):
        return "device namespace paths are blocked"
    parts = [p for p in re.split(r"[\\/]", path) if p != ""]
    if not parts:
        return "path has no file name"
    for part in parts:
        if part.endswith(".") or part.endswith(" "):
            return "path segments may not end in a dot or space"
    last = parts[-1]
    if ":" in last:
        return "alternate data streams are blocked"
    if re.match(r"^(?i:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?$", last):
        return "device names are blocked"
    return None

_DEVICE_BLOCKLIST = ("/dev/zero", "/dev/urandom", "/dev/stdin")

def _is_blocked_device(path: str) -> bool:
    p = path.replace("\\", "/")
    for blocked in _DEVICE_BLOCKLIST:
        if p == blocked or p.startswith(blocked + "/"):
            return True
    # Block all /proc — not just /proc/self/fd
    if p == "/proc" or p.startswith("/proc/"):
        return True
    if p.startswith("/dev/"):
        # any /dev/ is suspicious unless explicitly allowed
        if p in ("/dev/null",):
            return False
        return True
    return False

def _normalize_narrow_space(s: str) -> str:
    # NARROW NO-BREAK SPACE (U+202F) vs regular space
    return s.replace("\u202f", " ").replace("\u00a0", " ")

def _candidate_spellings(path: str) -> list[str]:
    cands = []
    # narrow <-> regular, NFD/NFC, curly quotes
    variants = [path]
    # narrow space
    if "\u202f" in path or " " in path:
        variants.append(_normalize_narrow_space(path))
        variants.append(path.replace(" ", "\u202f"))
    # quotes
    for v in list(variants):
        if "'" in v:
            variants.append(v.replace("'", "’"))
        if "’" in v:
            variants.append(v.replace("’", "'"))
    # NFD/NFC
    for v in list(variants):
        try:
            variants.append(unicodedata.normalize("NFD", v))
            variants.append(unicodedata.normalize("NFC", v))
        except (TypeError, ValueError):
            pass  # surrogate-heavy names cannot be normalized; skip the variant
    # dedup preserve order
    seen = set()
    out = []
    for v in variants:
        if v not in seen:
            seen.add(v)
            out.append(v)
    return out[:7]

def _levenshtein(a: str, b: str, max_dist: int = 2) -> int:
    if abs(len(a) - len(b)) > max_dist:
        return max_dist + 1
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cost = 0 if ca == cb else 1
            cur.append(min(prev[j] + 1, cur[j-1] + 1, prev[j-1] + cost))
        prev = cur
        if min(prev) > max_dist:
            return max_dist + 1
    return prev[-1]

class ReadFileTool(Tool):
    name = "read_file"
    description = (
        "Read a text file relative to the workspace root. "
        "Supports offset/limit window (default 0/2000), 128KB byte budget, 2000ch/line clamp. "
        "Also accepts glob via path (e.g. src/**/*.ts) or comma-separated list."
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Relative file path or glob"},
            "offset": {"type": "integer", "description": "Start line (0-indexed)"},
            "limit": {"type": "integer", "description": "Max lines to return"},
        },
        "required": ["path"],
    }
    ledger = None  # injected by registry

    def __init__(self) -> None:
        # dedup cache: (path, offset, limit) -> (mtime, size, content)
        self._dedup: dict[tuple[str, int, int], tuple[float, int, str]] = {}

    def execute(self, sandbox: Sandbox, path: str, offset: int = 0, limit: int = 2000) -> str:  # type: ignore[override]
        # repairs for null string etc handled in the argument-repair pass, but be defensive
        if path is None:
            return "ERROR: path is required"
        if not isinstance(path, str):
            path = str(path)
        if "\x00" in path or "\n" in path or "\r" in path:
            return "ERROR: invalid path"
        # Strict integer validation: reject "2abc" and "1.5".
        if isinstance(offset, str):
            if not _is_strict_positive_int(offset, allow_zero=True):
                return f"ERROR: offset must be a non-negative integer, got {offset!r}"
            offset = int(offset)
        elif isinstance(offset, float):
            # A float would silently truncate via int(); reject it like
            # the string form (D18).
            return f"ERROR: offset must be an integer, got {offset!r}"
        elif not isinstance(offset, int):
            try:
                offset = int(offset)
            except Exception:
                return "ERROR: offset must be integer"
        if isinstance(limit, str):
            if not _is_strict_positive_int(limit, allow_zero=True):
                return f"ERROR: limit must be a non-negative integer, got {limit!r}"
            limit = int(limit)
        elif isinstance(limit, float):
            return f"ERROR: limit must be an integer, got {limit!r}"
        elif not isinstance(limit, int):
            try:
                limit = int(limit)
            except Exception:
                return "ERROR: limit must be integer"
        if offset < 0 or limit < 0:
            return "ERROR: offset/limit must be >=0"
        if limit == 0:
            limit = _LINE_WINDOW
        if limit > 5000:
            limit = 5000
        # harness blocked paths (device namespace, CON/PRN, trailing dot/space, ADS)
        blocked = _is_blocked_path_harness(path)
        if blocked:
            return f"ERROR: refusing to read path {path!r}: {blocked}"
        if _is_blocked_device(path):
            return f"ERROR: refusing to read device path {path}"

        # Handle bulk via glob or comma list (merged read_file)
        # If path contains glob chars, expand — guard traversal in pattern
        if any(c in path for c in ["*", "?", "[", "**"]):
            # A genuine file whose name happens to contain a metacharacter
            # ("foo[1].txt") must win over pattern reading: check for the
            # literal file first and fall through to the glob only when no
            # such file exists.
            root_ = getattr(sandbox, "root", None)
            if root_ is not None:
                try:
                    if os.path.isfile(os.path.join(root_, path)):
                        return self._execute_single(sandbox, path, offset, limit)
                except OSError:
                    pass
            # Block patterns that could escape workspace (e.g. ../../etc/passwd)
            if ".." in path.replace("\\", "/").split("/") or path.startswith("/") or ":\\" in path:
                return f"ERROR: refusing to read pattern {path!r}: traversal blocked"
            if _is_blocked_path_harness(path) or _is_blocked_device(path):
                return f"ERROR: refusing to read pattern {path!r}: blocked"
            return self._execute_bulk(sandbox, path, offset, limit)
        # Comma-separated list heuristic — every segment must be a plain
        # separator-free name, and the whole path must not itself exist:
        # a genuine filename that contains a comma wins over the list form.
        if "," in path:
            parts = [p.strip() for p in path.split(",") if p.strip()]
            whole_exists = False
            root_ = getattr(sandbox, "root", None)
            if root_ is not None:
                try:
                    whole_exists = os.path.isfile(os.path.join(root_, path))
                except OSError:
                    whole_exists = False
            if len(parts) > 1 and not whole_exists and all(("/" not in p and "\\" not in p) for p in parts):
                # Validate each part before bulk
                for p in parts:
                    if _is_blocked_path_harness(p) or _is_blocked_device(p):
                        return f"ERROR: refusing to read path {p!r}: blocked"
                    if ".." in p.replace("\\", "/").split("/"):
                        return f"ERROR: refusing to read path {p!r}: traversal blocked"
                return self._execute_bulk_list(sandbox, parts, limit)

        return self._execute_single(sandbox, path, offset, limit)

    def _execute_bulk(self, sandbox: Sandbox, pattern: str, offset: int, limit: int) -> str:
        root = getattr(sandbox, "root", None)
        if root is None:
            return "ERROR: bulk read not supported in this sandbox"
        try:
            import pathlib  # noqa: F401 - root_str normalization below
            pat = pattern.replace("\\", "/").lstrip("/")
            # Ensure root is str for glob, handle Windows
            root_str = str(pathlib.Path(root))
            # iglob keeps memory and runtime bounded: a recursive glob on a
            # huge tree is never materialized in full. Walking stops at the
            # scan ceiling; the 20-file display cap still applies so the
            # per-file read loop below stays bounded too.
            files = []
            scan_ceiling = 3000
            scan_overflow = False
            for m in glob.iglob(pat, root_dir=root_str, recursive=True):
                # m is POSIX relative, convert to OS path for isfile check
                full = os.path.join(root_str, m.replace("/", os.sep))
                if os.path.isfile(full):
                    files.append(m)
                    if len(files) >= scan_ceiling:
                        scan_overflow = True
                        break
            total_matched = len(files)
            truncated_match_note = ""
            if scan_overflow:
                truncated_match_note = " (showing first 20 of many matched; scan ceiling 3000 reached)"
                files = files[:20]
            elif total_matched > 20:
                truncated_match_note = f" (showing first 20 of {total_matched} matched, rest omitted by per-call ceiling)"
                files = files[:20]
            if not files:
                return f"Note: no files matched pattern {pattern!r}"
            # Aggregate with cap ~100KB across files
            out_parts: list[str] = []
            total = 0
            skipped = 0
            unreadable = 0
            for rel in sorted(files):
                res = self._execute_single(sandbox, rel, offset, limit)
                # Strip notes for bulk, keep content. A per-file note can be
                # legitimate ("file is empty"), so only errors count as
                # unreadable here.
                if res.startswith("ERROR"):
                    unreadable += 1
                    continue
                chunk = f"--- {rel} ---\n{res}\n"
                if total + len(chunk) > 100_000:
                    # Count the files not shown at all (the current one
                    # included); errors and notes were never cap-skipped.
                    skipped = len(files) - len(out_parts)
                    break
                out_parts.append(chunk)
                total += len(chunk)
            header = f"READ {len(out_parts)}/{total_matched} files matched {pattern!r}{truncated_match_note}"
            if skipped:
                header += f" (+{skipped} more, aggregate cap 100KB)"
            return header + "\n" + "\n".join(out_parts)
        except (OSError, ValueError) as exc:
            # Only filesystem/pattern failures land here now; a coding bug
            # surfaces as a crash instead of a suspiciously empty bulk read.
            return f"ERROR: glob failed for {pattern!r}: {exc}"

    def _execute_bulk_list(self, sandbox: Sandbox, paths: list[str], limit: int) -> str:
        out_parts: list[str] = []
        file_count = 0
        total = 0
        for rel in paths[:20]:
            res = self._execute_single(sandbox, rel.strip(), 0, 400)
            if res.startswith("ERROR"):
                out_parts.append(f"--- {rel} ---\n{res}\n")
                file_count += 1
                continue
            chunk = f"--- {rel} ---\n{res}\n"
            if total + len(chunk) > 100_000:
                out_parts.append(f"... aggregate cap 100KB, {len(paths) - file_count} more skipped")
                break
            out_parts.append(chunk)
            file_count += 1
            total += len(chunk)
        return f"READ {file_count}/{len(paths)} files\n" + "\n".join(out_parts)

    def _execute_single(self, sandbox: Sandbox, path: str, offset: int, limit: int) -> str:
        # Dedup check: an unchanged window of an unchanged file returns the
        # cached result itself, so the model gets the content instead of a
        # note that forces a second read.
        root = getattr(sandbox, "root", None)
        dedup_key = (path, offset, limit)
        try:
            if root is not None:
                full_check = os.path.join(root, path)
                st = os.stat(full_check)
                cached = self._dedup.get(dedup_key)
                if cached and cached[0] == st.st_mtime_ns and cached[1] == st.st_size:
                    # Self-expiring: consume record
                    del self._dedup[dedup_key]
                    return cached[2]
        except OSError:
            pass  # stat failed (vanished file, bad join): treat as a cache miss

        # Try reading; on failure try candidate spellings
        content: str | None = None
        last_exc: Exception | None = None
        candidates = [path] + _candidate_spellings(path)[1:]
        for cand in candidates:
            try:
                # Confinement is enforced by sandbox.read_file; device
                # blocking already ran above.
                content = sandbox.read_file(cand)
                if cand != path:
                    path = cand  # use successful spelling
                break
            except Exception as exc:
                last_exc = exc
                continue

        if content is None:
            # Not found — did-you-mean
            if root is not None:
                try:
                    # substring match + levenshtein 2. Walk cost is bounded:
                    # heavy directories (caches, dependencies) are skipped,
                    # matching the search tool's behavior, and the walk is
                    # capped so a miss on a huge repo cannot stall the turn.
                    base = os.path.basename(path)
                    base_lower = base.lower()
                    candidates = []
                    visited = 0
                    for dirpath, dirnames, filenames in os.walk(root):
                        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
                        visited += len(filenames)
                        if visited > 20_000:
                            break
                        for fn in filenames:
                            if base_lower in fn.lower() or _levenshtein(base_lower, fn.lower(), 2) <= 2:
                                rel = os.path.relpath(os.path.join(dirpath, fn), root)
                                candidates.append(rel)
                        if len(candidates) >= 5:
                            break
                    if candidates:
                        return f"ERROR: file not found {path!r} — did you mean: {', '.join(candidates[:3])}?"
                except Exception:
                    pass
            return f"ERROR: cannot read {path!r}: {last_exc}"

        # Handle binary / mime — sample multiple windows, not just first 4k
        # Check first 8k, middle, and last 4k for null bytes
        _sample = content[:8192]
        if len(content) > 16384:
            mid = len(content)//2
            _sample += content[mid:mid+4096]
            _sample += content[-4096:]
        elif len(content) > 8192:
            _sample += content[-4096:]
        if "\x00" in _sample:
            # Detected binary content is never displayed; the extension
            # only decides the advisory note. SVG is XML, so it stays
            # readable despite occasionally carrying null-free markers.
            ext = os.path.splitext(path)[1].lower()
            if ext == ".svg":
                pass
            elif ext == ".pdf":
                return f"Note: {path!r} is a PDF ({len(content)} bytes). Use pdftotext or read specific pages."
            else:
                mime = ext.lstrip(".") or "unknown"
                return f"Note: {path!r} is binary ({mime}, {len(content)} bytes) — not displayed."

        # Empty file
        if not content:
            # Remember empty for ledger
            if self.ledger is not None:
                self.ledger.remember(path, content)
            return f"Note: {path!r} is empty (0 lines)."

        lines = content.splitlines()
        total_lines = len(lines)

        # Past EOF
        if offset >= total_lines:
            return f"Note: offset {offset} is beyond end of {path!r} ({total_lines} lines). Retry with smaller offset (e.g. offset=0, limit=2000)."

        # Window
        window = lines[offset : offset + limit]
        # Per-line clamp — avoid splitting surrogate pair
        clamped = False
        for i, line in enumerate(window):
            if len(line) > _PER_LINE_CLAMP:
                cut = _PER_LINE_CLAMP
                # Avoid splitting high surrogate
                if 0xD800 <= ord(line[cut-1]) <= 0xDBFF and cut < len(line) and 0xDC00 <= ord(line[cut]) <= 0xDFFF:
                    cut -= 1
                window[i] = line[:cut] + f" ... [line {offset+i+1} truncated at {_PER_LINE_CLAMP} chars]"
                clamped = True

        # Byte budget
        text = "\n".join(window)
        truncated_by_bytes = False
        shown_window = len(window)
        if len(text.encode("utf-8", errors="replace")) > _BYTE_BUDGET:
            raw = "\n".join(window).encode("utf-8", errors="replace")
            cut = min(_BYTE_BUDGET, len(raw) - 1)
            # Walk back over UTF-8 continuation bytes so the cut never
            # splits a multi-byte character.
            while cut > 0 and cut < len(raw) and (raw[cut] & 0xC0) == 0x80:
                cut -= 1
            text = raw[:cut].decode("utf-8", errors="replace")
            # Prefer a line boundary; keep the raw chunk when no newline exists.
            cut_text = text.rsplit("\n", 1)[0]
            text = cut_text if cut_text else text
            truncated_by_bytes = True

        # Build header with resume info
        shown_lines = text.split("\n") if text else []
        if truncated_by_bytes:
            # The resume offset must advance past what was actually shown.
            # Recount from the content rather than reusing the window list,
            # whose entries carry the clamp markers, not the raw text.
            shown_window = len(shown_lines)
        remaining = total_lines - (offset + shown_window)
        header = ""
        if offset != 0 or limit != _LINE_WINDOW or truncated_by_bytes or clamped or len(window) < total_lines:
            header = f"Note: {path!r} ({total_lines} lines, showing {shown_window} lines offset={offset} limit={limit}"
            if truncated_by_bytes:
                header += f", byte cap {_BYTE_BUDGET//1024}KB hit"
            if clamped:
                header += f", {sum(1 for l in window if l.endswith(f'truncated at {_PER_LINE_CLAMP} chars]'))} lines clamped at {_PER_LINE_CLAMP}ch"
            if remaining > 0:
                header += f", {remaining} more lines remain — retry with offset={offset + shown_window}"
            header += ").\n"

        result = header + text
        # Remember for ledger — track whether view was partial
        if self.ledger is not None:
            # Store full content hash regardless, but remember partial flag
            self.ledger.remember(path, content)
            # Also store ledger partial state via attribute if available
            try:
                if hasattr(self.ledger, "remember_partial"):
                    self.ledger.remember_partial(path, is_partial=(len(window) < total_lines or truncated_by_bytes))
            except Exception:
                pass

        # Dedup: store the full previous result for the unchanged check
        # (bounded: only results under the cache ceiling are kept).
        try:
            if root is not None and len(result) <= _DEDUP_CACHE_MAX_CHARS:
                full = os.path.join(root, path)
                st = os.stat(full)
                # Delete before insert so a re-read (file changed since the
                # cached entry) moves the key to the end of the dict: the
                # eviction below pops the first key, which is then the least
                # recently used rather than merely the first ever inserted.
                self._dedup.pop(dedup_key, None)
                self._dedup[dedup_key] = (st.st_mtime_ns, st.st_size, result)
                # Prune cache: pop the least recently used entry.
                if len(self._dedup) > 100:
                    self._dedup.pop(next(iter(self._dedup)))
        except Exception:
            pass

        # Legacy truncation note for old callers expecting _MAX_READ_CHARS
        if len(result) > _MAX_READ_CHARS and not header:
            result = result[:_MAX_READ_CHARS] + "\n... [truncated]"
            # The tail is hidden from the caller, so record the view as
            # partial: the ledger entry above claims a complete read and
            # would otherwise allow an edit over unseen content.
            if self.ledger is not None:
                try:
                    if hasattr(self.ledger, "remember_partial"):
                        self.ledger.remember_partial(path, is_partial=True)
                except Exception:
                    pass

        return result


class WriteFileTool(Tool):
    name = "write_file"
    description = "Create or overwrite a file with the given content."
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Relative file path"},
            "content": {"type": "string", "description": "Full new file content"},
        },
        "required": ["path", "content"],
    }

    ledger = None  # EditLedger, injected by the registry

    def execute(self, sandbox: Sandbox, path: str, content: str) -> str:
        if "\x00" in path or "\n" in path or "\r" in path:
            return "ERROR: invalid path"
        blocked = _is_blocked_path_harness(path)
        if blocked:
            return f"ERROR: refusing to write path {path!r}: {blocked}"
        if _is_blocked_device(path):
            return f"ERROR: refusing to write device path {path}"
        if not isinstance(content, str):
            content = str(content)
        if len(content) > _MAX_WRITE_CHARS:
            return f"ERROR: content too large ({len(content)} > {_MAX_WRITE_CHARS})"
        try:
            sandbox.write_file(path, content)
        except Exception as exc:  # noqa: BLE001
            return f"ERROR: cannot write {path}: {exc}"
        if self.ledger is not None:
            self.ledger.remember(path, content)
        return f"OK: wrote {len(content)} chars to {path}"


class EditFileTool(Tool):
    name = "edit_file"
    description = (
        "Replace the first exact occurrence of old_string with new_string "
        "in an existing file. The file must have been read this session; "
        "the edit is rejected if the file changed since that read."
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Relative file path"},
            "old_string": {"type": "string"},
            "new_string": {"type": "string"},
        },
        "required": ["path", "old_string", "new_string"],
    }
    ledger = None  # EditLedger, injected by the registry

    def execute(
        self, sandbox: Sandbox, path: str, old_string: str, new_string: str
    ) -> str:
        if "\x00" in path or "\n" in path or "\r" in path:
            return "ERROR: invalid path"
        # Same harness/device blocklist as read_file and write_file; an
        # edit must not reach device paths or ADS/CON/PRN forms.
        blocked = _is_blocked_path_harness(path)
        if blocked:
            return f"ERROR: refusing to edit path {path!r}: {blocked}"
        if _is_blocked_device(path):
            return f"ERROR: refusing to edit device path {path}"
        if old_string == "":
            return "ERROR: old_string must be non-empty"
        if len(old_string) > _MAX_READ_CHARS or len(new_string) > _MAX_WRITE_CHARS:
            return "ERROR: old_string or new_string too large"
        try:
            content = sandbox.read_file(path)
        except Exception as exc:  # noqa: BLE001
            return f"ERROR: cannot read {path}: {exc}"
        if content.endswith("\n... [truncated]"):
            # The sandbox itself had to cut the read: editing would write
            # the truncated form back and destroy the file's tail.
            return (
                f"ERROR: {path} is too large to read in one pass, so it cannot "
                "be edited with edit_file (an edit would write back a truncated "
                "file). Use read_file windows plus run_command, or rewrite the "
                "file in sections."
            )
        if "\ufffd" in content:
            # The sandbox read decodes with errors="replace", so any byte
            # that is not valid UTF-8 arrives as U+FFFD and a write-back
            # would silently destroy it across the whole file, not just at
            # the edited region. Refuse rather than corrupt.
            return (
                f"ERROR: {path} contains bytes that are not valid UTF-8 (or "
                "literal U+FFFD), so edit_file would corrupt them. Use "
                "write_file, or convert the file to UTF-8 first."
            )
        if self.ledger is not None:
            if not self.ledger.has_seen(path):
                return (
                    f"ERROR: read {path} with read_file before editing "
                    "(no recorded read this session)"
                )
            if not self.ledger.is_current(path, content):
                return (
                    f"ERROR: {path} changed on disk since your last read - "
                    "read it again, then retry the edit"
                )
            # Check partial view
            try:
                if hasattr(self.ledger, "is_partial") and self.ledger.is_partial(path):
                    return f"ERROR: Only part of {path!r} has been read (windowed view). Read with larger limit or use write_file."
            except Exception:
                pass
        else:
            # No ledger is wiring bug; fail closed.
            return "ERROR: edit ledger not configured"
        if old_string not in content:
            return f"ERROR: old_string not found in {path}"
        occurrences = content.count(old_string)
        if occurrences > 1:
            # An ambiguous needle silently edits whichever occurrence comes
            # first — usually not the one the model meant. Refuse instead
            # and make the model narrow the needle.
            return (
                f"ERROR: old_string occurs {occurrences} times in {path} - "
                "refusing an ambiguous edit. Extend old_string with "
                "surrounding lines until it matches exactly once, then retry."
            )
        new_content = content.replace(old_string, new_string, 1)
        if len(new_content) > _MAX_WRITE_CHARS:
            return f"ERROR: result too large ({len(new_content)} > {_MAX_WRITE_CHARS})"
        try:
            sandbox.write_file(path, new_content)
        except Exception as exc:  # noqa: BLE001
            return f"ERROR: cannot write {path}: {exc}"
        if self.ledger is not None:
            self.ledger.remember(path, new_content)
        return f"OK: edited {path}"


class ListDirTool(Tool):
    name = "list_dir"
    description = "List immediate children of a directory ('.' for the workspace root)."
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
    }

    def execute(self, sandbox: Sandbox, path: str) -> str:
        # Validate to reduce injection risk.
        if "\x00" in path or "\n" in path or "\r" in path:
            return "ERROR: invalid path"
        root = getattr(sandbox, "root", None)
        if root is None:
            # No direct view: use shell with safe quoting. Mirror the
            # direct path's traversal guard: ".." components and absolute
            # paths are refused before they reach the container shell.
            if any(c == ".." for c in path.replace("\\", "/").split("/")):
                return "ERROR: path must stay inside the workspace"
            if path.startswith("/") or re.match(r"^[a-zA-Z]:[\\/]", path):
                return "ERROR: path must stay inside the workspace"
            if _SHELL_META_RE.search(path) or '"' in path or "'" in path:
                return "ERROR: path contains unsupported characters for shell listing"
            quoted = shlex.quote(path)
            last_error = "listing failed"
            for cmd in (
                f"ls -la {quoted}",
                f"ls -1 {quoted}",
                f"python -c \"import os,sys; p=sys.argv[1]; print(chr(10).join(sorted(os.listdir(p))))\" {quoted}",
            ):
                result = sandbox.exec(cmd)
                if result.exit_code == 0:
                    if result.stdout.strip():
                        return result.stdout
                    return "(empty directory)"
                last_error = result.stderr or "listing failed"
            return f"ERROR: {last_error}"

        # Direct view: resolve and check confinement.
        base = root if path in (".", "") else os.path.join(root, path)
        try:
            real_base = os.path.realpath(base)
            real_root = os.path.realpath(root)
            if not (real_base == real_root or real_base.startswith(real_root + os.sep)):
                return f"ERROR: path escapes workspace: {path}"
            entries = sorted(os.listdir(real_base))
        except OSError as exc:
            return f"ERROR: {exc}"
        lines = []
        for entry in entries:
            full = os.path.join(real_base, entry)
            marker = "/" if os.path.isdir(full) else ""
            lines.append(entry + marker)
        return "\n".join(lines) or "(empty directory)"

