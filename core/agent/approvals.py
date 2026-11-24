"""Tool approval: classify risk, gate mutations, delegate prompting."""

from __future__ import annotations

import re
import sys
import threading
from typing import Any, Callable

from core.agent.permissions import icacls_owner_only
from core.agent.repairs import ALIASES, canonical_command

AskCallback = Callable[[str], str]  # returns "y" | "n" | "a"
NoteCallback = Callable[[str], None]

MUTATING_TOOLS = frozenset({"write_file", "edit_file", "run_command", "git_reset", "kill_shell"})

# Plan mode is a read-only boundary, so it works from an explicit allowlist
# rather than a deny list: a newly registered or renamed tool is refused
# until someone deliberately adds it here.
READONLY_TOOLS = frozenset(
    {
        "read_file", "list_dir", "git_diff", "web_fetch",
        "search_code", "find_file", "extract_document", "shell_output",
    }
)


def _canonical_path(arguments: dict[str, Any]) -> str:
    """Resolve the path under any alias the repair layer accepts.

    Keeps the fenced-file check effective even when this policy is called
    directly with raw tool arguments (the loop repairs before check()).
    """
    value = arguments.get("path")
    if value:
        return str(value)
    for alias in ALIASES.get("path", ()):
        if alias != "path" and arguments.get(alias):
            return str(arguments[alias])
    return ""

# Remote tools (an MCP server) whose effect the local patterns cannot
# screen are always confirmed, never auto-allowed. Names are registered
# when the adapter is built.
EXTERNAL_TOOLS: set[str] = set()


def register_external_tool(name: str) -> None:
    """Mark a tool as remote, so it always needs explicit confirmation."""
    if name:
        EXTERNAL_TOOLS.add(name)

MODES = ("default", "auto", "yolo", "plan")

# Patterns that can irreversibly destroy work.
_DESTRUCTIVE = (
    r"rm\s+(-[rRfF]+\s+)*[^\s]*\s*(-[rRfF]+)",  # rm -rf / rm -fr
    r"\brm\s+-[rR]",
    r"\brmdir\b",
    r"\brm\s+.*\*",
    r"\bformat\s+[a-zA-Z]{1,2}:",  # format C: - not "--format=..." flags
    r"\bmkfs\b",
    r"\bfind\b[^\n;|&]*-\s?exec(dir)?\b[^\n;|&]*\brm\b",  # find ... -exec rm {}
    r"\bdd\b\s+if=",
    r"del\s+/[sfqSFQ]",
    r"\bdel\b",
    r"\berase\b",
    r"\bRemove-Item\b[^\n]*-Recurse",
    r"\bRemove-Item\b",
    r"git\s+push[^\n]*--force",
    r"git\s+push\s+-f\b",
    r"git\s+reset\s+--hard",
    r"git\s+clean\s+-[fdx]",
    r"git\s+checkout\s+--\s",
    r"git\s+restore\s+--?\w*\s*\.",
    r"\bshutdown\b",
    r"\bStop-Computer\b",
    r"\bRestart-Computer\b",
    r"\btaskkill\b",
    r"\bStop-Process\b",
    r"curl[^\n|]*\|\s*(ba|z|d)?sh",
    r"wget[^\n|]*\|\s*(ba|z|d)?sh",
    r"\biex\b",
    r"Invoke-Expression",
    r"Set-ExecutionPolicy",
    r"\bchmod\s+777\b",
    r"\bsudo\b",
    r"\breg\s+delete\b",
    r"\bnet\s+user\b",
    r"\bdiskpart\b",
    r"\bcertutil\b",
    r"\bsc\s+delete\b",
    r">\s*/dev/sd",
    r"\btruncate\b",
    r"\battrib\s+",
)

# Low-risk commands auto-allowed in all modes (includes test runners).
_SAFE_COMMANDS = (
    r"^\s*(ls|dir|cat|type|echo|head|tail|wc|find|rg|grep|where|which|pwd|cd)\b",
    r"^\s*git\s+(status|diff|log|show|branch|rev-parse|ls-files|remote)\b",
    r"^\s*python\s+-m\s+(pytest|unittest|mypy|ruff|flake8)\b",
    r"^\s*python\s+--version\s*$",
    r"^\s*(pytest|py\.test|node|npm|git|go|cargo|dotnet)\s+--version\s*$",
    r"^\s*(pytest|py\.test)\b",
    r"^\s*(npm|pnpm|yarn)\s+(test|run\s+test|run\s+lint)\b",
    r"^\s*(go|cargo)\s+test\b",
    r"^\s*dotnet\s+test\b",
)

_DESTRUCTIVE_RE = re.compile("|".join(_DESTRUCTIVE), re.IGNORECASE)
_SAFE_RE = re.compile("|".join(_SAFE_COMMANDS), re.IGNORECASE)

# Mid-session fence for AGENTS.md / MEMORY.md: shell redirection and
# PowerShell content setters are refused, and write_file/edit_file to
# either file classifies as confirm.
_FORBIDRE = [
    re.compile(r"(>|>>)\s*[^|\r\n]*AGENTS\.md|(set-content|add-content|out-file|new-item|remove-item|move-item|rename-item)\b[^|\r\n]*AGENTS\.md", re.IGNORECASE),
    re.compile(r"(>|>>)\s*[^|\r\n]*MEMORY\.md|(set-content|add-content|out-file|new-item|remove-item|move-item|rename-item)\b[^|\r\n]*MEMORY\.md", re.IGNORECASE),
]

# Optional user rules file (commands.rules), consulted by the classifier.
_RULES_PATH = None
_RULES_CACHE: list[dict[str, Any]] | None = None
_RULES_MTIME: float = 0.0
# Serialises cache reloads: a concurrent caller must never observe a
# half-updated rules set.
_RULES_LOCK = threading.Lock()


def _load_rules() -> list[dict[str, Any]]:
    """Return the parsed rules, reloading them under a lock when stale."""
    with _RULES_LOCK:
        return _load_rules_locked()


def _load_rules_locked() -> list[dict[str, Any]]:
    global _RULES_CACHE, _RULES_MTIME, _RULES_PATH
    # Try env override, then MANTRA/rules/commands.rules, then ~/.mantra/rules
    import os
    import sys

    candidates = []
    if os.environ.get("MANTRA_RULES_FILE"):
        candidates.append(os.environ["MANTRA_RULES_FILE"])
    # Repo-shipped rules at the project root.
    try:
        from pathlib import Path
        candidates.append(str(Path(__file__).resolve().parents[2] / "rules" / "commands.rules"))
    except Exception:
        pass
    # Installed-wheel location (data-files land under sys.prefix).
    candidates.append(os.path.join(sys.prefix, "rules", "commands.rules"))
    candidates.append(os.path.join(os.path.expanduser("~"), ".mantra", "rules", "commands.rules"))
    path = None
    for cand in candidates:
        if cand and os.path.isfile(cand):
            path = cand
            break
    if not path:
        return []
    try:
        mtime = os.path.getmtime(path)
        if _RULES_CACHE is not None and _RULES_PATH == path and mtime == _RULES_MTIME:
            return _RULES_CACHE
        rules: list[dict[str, Any]] = []
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                t = line.strip()
                if not t or t.startswith("#"):
                    continue
                if t.startswith("forbidre|"):
                    # regex rule: forbidre|regex|justification, regex may contain |
                    first = t.find("|")
                    last = t.rfind("|")
                    if first < 0 or last <= first:
                        continue
                    pat = t[first + 1 : last].strip()
                    just = t[last + 1 :].strip()
                    if not pat:
                        continue
                    try:
                        re.compile(pat, re.IGNORECASE)
                    except re.error:
                        continue
                    rules.append({"decision": "forbid", "kind": "regex", "pattern": pat, "justification": just})
                else:
                    parts = [p.strip() for p in t.split("|")]
                    if len(parts) < 2:
                        continue
                    decision = parts[0].lower()
                    if decision not in ("forbid", "prompt", "allow"):
                        continue
                    just = parts[2] if len(parts) >= 3 else ""
                    toks = [w for w in parts[1].split() if w]
                    if not toks:
                        continue
                    rules.append({"decision": decision, "kind": "tokens", "pattern": toks, "justification": just})
        _RULES_CACHE = rules
        _RULES_PATH = path
        _RULES_MTIME = mtime
        return rules
    except Exception:
        # A corrupt or unreadable rules file must not silently drop the
        # forbid/prompt rules: keep a previously loaded set, otherwise
        # surface the failure so the caller can fail closed.
        if _RULES_CACHE is not None:
            return _RULES_CACHE
        raise

# Stored credentials cannot be checked when the store is unreadable. The
# audit writer consults this so it never persists an entry it cannot prove
# is secret-free.
_REDACTION_OK = True
_REDACTION_WARNED = False


def _note_redaction_failure(exc: BaseException) -> None:
    """Flag stored-key redaction as unavailable and warn once."""
    global _REDACTION_OK, _REDACTION_WARNED
    _REDACTION_OK = False
    if _REDACTION_WARNED:
        return
    _REDACTION_WARNED = True
    print(
        f"[approvals] stored-credential redaction unavailable: {str(exc)[:120]}; "
        "audit entries are skipped until it recovers",
        file=sys.stderr,
    )


def redaction_ok() -> bool:
    """Whether stored credentials could be checked for redaction."""
    return _REDACTION_OK


def _redact_sensitive(s: str) -> str:
    # Redact sensitive keys: known prefixes plus generic high-entropy tokens
    # that appear near assignment-like syntax or as bare values.
    s = re.sub(r"(?i)(?<![A-Za-z0-9])(sk-[a-zA-Z0-9_\-]{12,}|sk_[a-f0-9_\-]{12,}|AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{20,}|ghu_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|xox[baprs]-[A-Za-z0-9\-]{10,}|AIza[0-9A-Za-z_\-]{20,})(?![A-Za-z0-9])", "[REDACTED]", s)
    s = re.sub(r"(?i)(passw(or)?d|secret|token|apikey|api[-_]?key|authorization|bearer|credential)\s*[=:]\s*[\"']?(?:bearer\s+)?[^\"'\s,;]+", r"\1=[REDACTED]", s)
    # Generic key assignments: 6+ chars are almost always credentials
    # (key=abc123), not words like key=value, so the threshold sits well
    # below the prefix-less token cutoff elsewhere.
    s = re.sub(r"(?i)\b(key|secret|token)\s*=\s*[\"']?[A-Za-z0-9_\-]{6,}[\"']?", r"\1=[REDACTED]", s)
    # Bare bearer tokens (eyJ... JWT style) without prefix
    s = re.sub(r"\b(eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,})\b", "[REDACTED]", s)
    # Also redact any value that exactly matches a stored credential (exact match)
    global _REDACTION_OK
    try:
        from core.agent.keys import stored_keys as _sk
        stored = _sk()
    except (OSError, ValueError, TypeError, ImportError) as exc:
        _note_redaction_failure(exc)
    except Exception as exc:  # noqa: BLE001 - a redaction bug must not crash the caller
        _note_redaction_failure(exc)
    else:
        _REDACTION_OK = True
        for value in stored.values():
            value = (value or "").strip()
            if len(value) >= 12 and value in s:
                s = s.replace(value, "[REDACTED]")
    return s

def _is_escaped(text: str, idx: int) -> bool:
    # Only backslashes escape in POSIX shells; a backtick is command
    # substitution, not an escape, and must not affect quote state.
    slashes = 0
    for j in range(idx - 1, -1, -1):
        if text[j] == "\\":
            slashes += 1
        else:
            break
    return (slashes % 2) == 1

def _tokenize(segment: str) -> list[str]:
    """Tokenize outside quotes; handle PowerShell grouping delimiters."""
    tokens: list[str] = []
    cur: list[str] = []
    in_single = False
    in_double = False
    for i, ch in enumerate(segment):
        esc = _is_escaped(segment, i)
        if ch == "'" and not in_double and not esc:
            in_single = not in_single
            continue
        if ch == '"' and not in_single and not esc:
            in_double = not in_double
            continue
        if not in_single and not in_double and (ch.isspace() or ch in ("(", ")")):
            if cur:
                tokens.append("".join(cur))
                cur = []
            continue
        cur.append(ch)
    if cur:
        tokens.append("".join(cur))
    # Handle quoted leading command like "\"rm\" -rf"
    trimmed = segment.strip()
    if tokens and len(tokens) == 1 and trimmed and trimmed[0] in ('"', "'") and " " in tokens[0]:
        # Recursively tokenize the inner quoted command
        inner = tokens[0]
        return _tokenize(inner) + tokens[1:]
    return tokens


def _wrapper_oneliner(tokens: list[str]) -> bool:
    """True when the segment is a wrapper interpreter with inline code.

    ``bash -c``, ``cmd /c`` and ``powershell -c``/``-Command`` execute
    their remaining text as code, exactly like the interpreters in
    ``_is_interpreter_oneliner``; ``_get_tokens`` strips them before
    that check runs, so the wrapper head is detected here instead.
    """
    if len(tokens) < 3:
        return False
    wrapper = tokens[0].lower().replace("\\", "/").split("/")[-1]
    if wrapper.endswith(".exe"):
        wrapper = wrapper[:-4]
    switch = tokens[1].lower()
    return (
        (wrapper in ("cmd", "cmd.exe") and switch == "/c")
        or (wrapper in ("powershell", "powershell.exe", "pwsh", "pwsh.exe") and switch in ("-command", "-c"))
        or (wrapper in ("bash", "bash.exe", "sh", "dash", "zsh") and switch == "-c")
    )


def _get_tokens(segment: str) -> list[str]:
    """Tokenize outside quotes, then expand wrappers to the inner command."""
    tokens = _tokenize(segment)
    # Classify the inner command only so the wrapper is not double-counted;
    # _wrapper_oneliner still gates bash/powershell/cmd -c one-liners, so
    # those remain confirm.
    if len(tokens) >= 3:
        wrapper = tokens[0].lower().replace("\\", "/").split("/")[-1]
        switch = tokens[1].lower()
        is_wrapper = (
            (wrapper in ("cmd", "cmd.exe") and switch == "/c")
            or (wrapper in ("powershell", "powershell.exe", "pwsh", "pwsh.exe") and switch in ("-command", "-c"))
            or (wrapper in ("bash", "bash.exe") and switch == "-c")
        )
        if is_wrapper:
            # The outer tokenizer already flattened quoted inner arguments,
            # so tokens after the switch are the inner command's tokens.
            return tokens[2:]
    return tokens

def _split_command(cmd: str) -> list[str]:
    """Split on &&, ||, ;, |, &, and newlines outside quotes."""
    # Newlines are separators too; folding them into ';' first also closes
    # the hole where a destructive pattern hid after a line break.
    cmd = cmd.replace("\r\n", "\n").replace("\n", "; ")
    parts: list[str] = []
    start = 0
    in_single = False
    in_double = False
    i = 0
    while i < len(cmd):
        ch = cmd[i]
        esc = _is_escaped(cmd, i)
        if ch == "'" and not in_double and not esc:
            in_single = not in_single
            i += 1
            continue
        if ch == '"' and not in_single and not esc:
            in_double = not in_double
            i += 1
            continue
        if in_single or in_double:
            i += 1
            continue
        length = 0
        if i + 1 < len(cmd) and cmd[i : i + 2] in ("&&", "||"):
            length = 2
        elif ch in (";", "|", "&"):
            length = 1
        if length:
            part = cmd[start:i].strip()
            if part:
                parts.append(part)
            i += length
            start = i
            continue
        i += 1
    tail = cmd[start:].strip()
    if tail:
        parts.append(tail)
    return parts if parts else [cmd]

def _test_match(tokens: list[str], pattern: list[str], start: int = 0, exact_start: bool = False) -> bool:
    """Match pattern at any token position (or exact start if wrapper)."""
    if len(tokens) - start < len(pattern):
        return False
    end = start if exact_start else len(tokens) - len(pattern)
    for i in range(start, end + 1):
        if tokens[i : i + len(pattern)] == pattern:
            return True
    return False


def _has_redirect(segment: str) -> bool:
    """True when a redirect operator appears outside quotes."""
    in_single = False
    in_double = False
    for i, ch in enumerate(segment):
        esc = _is_escaped(segment, i)
        if ch == "'" and not in_double and not esc:
            in_single = not in_single
            continue
        if ch == '"' and not in_single and not esc:
            in_double = not in_double
            continue
        if in_single or in_double:
            continue
        if ch == ">":
            return True
    return False


# Inline-code interpreters run payloads the pattern screen cannot see, so
# they always require confirmation. Wrappers (bash -c, cmd /c, powershell -c)
# are caught by _wrapper_oneliner before expansion strips them.
_INTERPRETER_TOKENS = frozenset(
    {
        "python", "python3", "pypy", "pypy3",
        "node", "deno", "bun", "perl", "ruby", "php", "lua", "jshell",
        "powershell", "pwsh", "bash", "dash", "sh", "zsh",
    }
)

# Flags that mean "run the next argument as code".
_INLINE_CODE_FLAGS = frozenset(
    {"-c", "-e", "-r", "--eval", "-enc", "-encodedcommand", "-command", "-ie"}
)


def _is_interpreter_oneliner(tokens: list[str]) -> bool:
    """Interpreter invoked with its code inline."""
    if not tokens:
        return False
    head = tokens[0].lower().replace("\\", "/").split("/")[-1]
    if head.endswith(".exe"):
        head = head[:-4]
    if head not in _INTERPRETER_TOKENS:
        return False
    return any(tok.lower() in _INLINE_CODE_FLAGS for tok in tokens[1:])


def _classify_segment(segment: str, sole_segment: bool = False) -> str:
    """Classify one command segment; safe only if the whole segment is read-only.

    ``sole_segment`` marks a segment that is the entire command, which lets a
    lone echo be treated as data (see the echo handling below).
    """
    segment = segment.strip()
    if not segment:
        return "safe"
    tokens = [t.lower() for t in _get_tokens(segment)]
    if not tokens:
        return "safe"
    # Token-level destructive checks, including deletion-capable search
    # flags that no earlier pattern covers.
    for idx, tok in enumerate(tokens):
        # A plain delete verb (rm, del, erase, rd, Remove-Item) destroys
        # work even without recursive flags, so it must never pass as an
        # ordinary mutation in auto mode.
        if tok in ("rm", "del", "erase", "rmdir", "rd", "remove-item"):
            if sole_segment and tokens[0] == "echo" and not re.search(r"(\$\(|\$\{|`)", segment):
                continue  # a lone echo prints these words; it does not run them
            return "destructive"
        elif tok == "find":
            rest = tokens[idx + 1 :]
            if "-delete" in rest:
                return "destructive"
            if any(f in rest for f in ("-exec", "-execdir", "-ok", "-okdir")):
                return "mutating"
    # _get_tokens expands wrappers to the inner command, hiding the
    # interpreter from the check below; detect it from the pre-expansion
    # token head and gate it like any inline code.
    if _wrapper_oneliner(_tokenize(segment)):
        return "confirm"
    # Interpreter one-liners: the payload is invisible to every pattern
    # above, so the invocation needs a human yes/no regardless of mode.
    if _is_interpreter_oneliner(tokens):
        return "confirm"
    # echo never executes its arguments, unless an expansion inside them
    # could; the expansion check below handles that case.
    if tokens[0] == "echo" and not re.search(r"(\$\(|\$\{|`)", segment):
        return "mutating" if _has_redirect(segment) else "safe"
    if _has_redirect(segment):
        return "mutating"
    if re.search(r"(\$\(|\$\{|`)", segment):
        return "mutating"
    if _SAFE_RE.match(segment):
        return "safe"
    return "mutating"


def _apply_token_rules(rules: list[dict[str, Any]], segment: str, risk: str) -> str:
    """Apply token-sequence rules to one classified segment.

    Forbid wins outright. Prompt lifts an ordinary mutating (or safe)
    verdict to an explicit confirmation; it never lowers anything. Allow
    relaxes only the ordinary mutating verdict — destructive and
    interpreter-confirm verdicts always stand, because a rule file must
    not be able to wave through what the pattern screen caught.
    """
    if not rules:
        return risk
    try:
        toks = [t.lower() for t in _get_tokens(segment)]
    except Exception:
        return risk
    rank = {"safe": 0, "mutating": 1, "confirm": 2, "destructive": 3}
    out = risk
    for rule in rules:
        if rule.get("kind") != "tokens":
            continue
        pattern = rule.get("pattern") or []
        if not pattern or not _test_match(toks, pattern):
            continue
        decision = rule.get("decision")
        if decision == "forbid":
            return "destructive"
        if decision == "prompt":
            if rank.get(out, 1) < rank["confirm"]:
                out = "confirm"
        elif decision == "allow":
            if out == "mutating":
                out = "safe"
    return out


def classify_command(command: str) -> str:
    """Risk of a shell command: destructive, safe, or mutating. Worst segment wins."""
    if not command or not command.strip():
        return "safe"
    # Regex forbid rules match the whole command first (spanning segments
    # and wrappers); token rules are applied per segment below with the
    # same worst-wins combination as the built-in classifier.
    try:
        rules = _load_rules()
        if rules:
            for r in rules:
                if r["kind"] != "regex":
                    continue
                try:
                    if re.search(r["pattern"], command, re.IGNORECASE):
                        return "destructive"
                except re.error:
                    pass
    except Exception:
        # An unreadable or corrupt rules file must not silently weaken
        # the gate; fail closed to an explicit confirmation.
        return "confirm"
    # Forbidre regex first — matches whole command even across wrappers/segments
    for pat in _FORBIDRE:
        try:
            if pat.search(command):
                return "destructive"
        except Exception:
            pass
    # The broad scan flags destructive text anywhere. A lone echo is data,
    # not commands, so it is exempt - but expansions, pipes or chains that
    # execute its text (echo "rm -rf /" | bash) stay destructive.
    if _DESTRUCTIVE_RE.search(command):
        segments0 = _split_command(command)
        echo_only = (
            len(segments0) == 1
            and re.match(r"^\s*echo\s+", segments0[0], re.IGNORECASE) is not None
            and re.search(r"(\$\(|\$\{|`)", segments0[0]) is None
        )
        if not echo_only:
            return "destructive"
    # Split compounds and classify every segment; the worst verdict wins.
    # Severity ladder: destructive > confirm > mutating > safe.
    try:
        worst = "safe"
        rank = {"safe": 0, "mutating": 1, "confirm": 2, "destructive": 3}
        segments = _split_command(command)
        sole_segment = len(segments) == 1
        for idx, seg in enumerate(segments):
            risk = _classify_segment(seg, sole_segment=sole_segment)
            risk = _apply_token_rules(rules if isinstance(rules, list) else [], seg, risk)
            # A bare interpreter fed by a pipe/chain executes the incoming
            # text as commands, so escalate to confirm. Wrapper one-liners
            # already classify as confirm via _wrapper_oneliner.
            if idx > 0:
                head = _get_tokens(seg)
                if len(head) == 1:
                    head_name = head[0].lower().replace("\\", "/").split("/")[-1]
                    if head_name.endswith(".exe"):
                        head_name = head_name[:-4]
                    if head_name in _INTERPRETER_TOKENS:
                        if rank.get(risk, 1) < rank["confirm"]:
                            risk = "confirm"
            if risk == "destructive":
                return "destructive"
            if rank.get(risk, 1) > rank.get(worst, 0):
                worst = risk
        return worst
    except Exception:
        # Fail closed: an internal classification error must not let a
        # command ride through auto/headless approval as an ordinary
        # mutation; route it to explicit confirmation instead.
        return "confirm"


def classify(tool: str, arguments: dict[str, Any]) -> tuple[str, str]:
    """Return ``(risk, human detail)`` for one tool call."""
    if tool in EXTERNAL_TOOLS:
        # Unknown remote effect: never silent, whatever the mode.
        return "confirm", f"external tool {tool}"
    if tool not in MUTATING_TOOLS:
        return "safe", tool

    if tool in ("write_file", "edit_file"):
        path = _canonical_path(arguments) or "?"
        verb = "overwrite" if tool == "write_file" else "edit"
        # The shell-layer fence must also cover tool-level writes: a
        # direct write_file to AGENTS.md/MEMORY.md bypasses the run_command
        # forbid rules, so route it to explicit confirmation.
        import os as _os

        base = _os.path.basename(path.replace("\\", "/")).lower()
        if base in ("agents.md", "memory.md"):
            return "confirm", f"{verb} {path} (fenced file)"
        return "mutating", f"{verb} {path}"

    if tool == "git_reset":
        return "destructive", "discard all uncommitted changes (git checkout -- .)"

    if tool == "kill_shell":
        return "destructive", "kill background task/process"

    if tool == "run_command":
        # Repair aliases (cmd, shellCommand, ...) must not bypass the
        # gate: classification runs before the loop's repair pass, so an
        # alias key would otherwise classify as "safe" with no prompt.
        command = canonical_command(arguments)
        preview = command if len(command) <= 160 else command[:157] + "..."
        return classify_command(command), preview or "(empty command)"

    # Fail closed for future mutating tools without a dedicated branch.
    return "confirm", f"{tool} (unclassified mutating tool)"


# The audit log is written by every tool check, so the ACL tightening
# runs once per process instead of spawning icacls on every call.
_LOG_ACL_DONE = False

# One-time stderr warning when the audit log cannot be written: a silent
# fail-open would leave tool use unlogged. The health flag is persistent so
# callers in modes above yolo can refuse an unrecordable mutation.
_LOG_WRITE_WARNED = False
_AUDIT_HEALTHY = True


def _warn_audit_log_failure(message: str | None = None) -> None:
    """Surface the first audit-log failure to the operator."""
    global _LOG_WRITE_WARNED
    if _LOG_WRITE_WARNED:
        return
    _LOG_WRITE_WARNED = True
    print(
        message
        or "[approvals] pre-tool-use audit log could not be written; tool calls are running unlogged",
        file=sys.stderr,
    )


def audit_log_healthy() -> bool:
    """Persistent health of the pre-tool-use audit log."""
    return _AUDIT_HEALTHY


def _set_audit_health(healthy: bool) -> None:
    global _AUDIT_HEALTHY
    _AUDIT_HEALTHY = healthy


def _rotate_audit_log(log_path: str) -> None:
    """Truncate a >1MB audit log to its tail, under a sibling lock.

    Two processes rotating concurrently would both read-modify-write the
    file and drop each other's recent lines; the exclusive-create lock
    makes rotation single-writer. Fails open (no rotation) on lock
    contention or OSError, matching the best-effort logging contract.
    """
    import os
    import time as _time

    lock_path = log_path + ".lock"
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except OSError:
        return  # another process is rotating; leave the file alone
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            tail = f.read()[-200_000:]
        with open(log_path, "w", encoding="utf-8") as f:
            f.write(tail)
            f.write(
                f"# rotated {_time.strftime('%Y-%m-%d %H:%M:%S')} — older entries truncated\n"
            )
    except OSError:
        pass
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.remove(lock_path)
        except OSError:
            pass


# Guards the shared audit-log rotation counter so concurrent approval
# checks never lose increments.
_LOG_CHECK_LOCK = threading.Lock()


def _restrict_audit_log(log_path: str) -> None:
    """Best-effort owner-only access for the audit log, once per process.

    os.chmod(0o600) is a no-op on Windows, so the icacls tightening is
    applied here too, minus the credentials warning from keys._restrict_file.
    Runs only once the file exists; the ACL then persists.
    """
    import os

    global _LOG_ACL_DONE
    if _LOG_ACL_DONE:
        return
    if not os.path.isfile(log_path):
        return
    _LOG_ACL_DONE = True
    try:
        os.chmod(log_path, 0o600)
    except OSError:
        pass
    if os.name == "nt":
        try:
            icacls_owner_only(log_path)
        except Exception:
            pass


class ApprovalPolicy:
    """Gate tool execution by risk and mode."""

    def __init__(
        self,
        mode: str = "default",
        ask: AskCallback | None = None,
        note: NoteCallback | None = None,
    ) -> None:
        if mode not in MODES:
            raise ValueError(f"unknown approval mode '{mode}' (known: {list(MODES)})")
        self.mode = mode
        self._ask = ask or (lambda prompt: "n")
        self._note = note or (lambda message: None)
        self.session_allowed: set[str] = set()

    def check(self, tool: str, arguments: dict[str, Any]) -> bool:
        risk, detail = classify(tool, arguments)
        audit_ok = True
        # Pre-tool-use logging (redacted)
        try:
            import os

            # MANTRA_PRE_TOOL_USE_LOG redirects the audit log (the test
            # harness points it at a temp file so suites never touch the
            # operator's ~/.mantra/logs).
            log_path = os.environ.get(
                "MANTRA_PRE_TOOL_USE_LOG",
                os.path.join(os.path.expanduser("~"), ".mantra", "logs", "pre-tool-use.log"),
            )
            os.makedirs(os.path.dirname(log_path), exist_ok=True)
            # Rotate before appending so the log stays bounded, but sample
            # size every 64th call to avoid churn. The shared counter is
            # lock-guarded so concurrent checks cannot skip rotation.
            with _LOG_CHECK_LOCK:
                ApprovalPolicy._log_check_counter = (
                    getattr(ApprovalPolicy, "_log_check_counter", 0) + 1
                )
                _log_check_counter = ApprovalPolicy._log_check_counter
            try:
                # A missing log file fails the size probe; that is normal
                # on the first call, not a logging failure.
                if _log_check_counter % 64 == 0 and os.path.getsize(log_path) > 1_000_000:
                    _rotate_audit_log(log_path)
            except OSError:
                pass
            safe_detail = _redact_sensitive(detail)[:200]
            safe_tool = _redact_sensitive(tool)
            if not redaction_ok():
                # Never persist an entry we cannot prove is secret-free.
                audit_ok = False
                _warn_audit_log_failure(
                    "[approvals] stored-credential redaction unavailable; audit entry skipped"
                )
            else:
                with open(log_path, "a", encoding="utf-8") as f:
                    f.write(f"tool={safe_tool} risk={risk} detail={safe_detail}\n")
                # Owner-only ACL: os.chmod alone is a no-op on Windows.
                _restrict_audit_log(log_path)
        except Exception:
            audit_ok = False
            _warn_audit_log_failure()
        _set_audit_health(audit_ok)

        if self.mode == "plan" and (tool in EXTERNAL_TOOLS or tool not in READONLY_TOOLS):
            self._note(f"plan mode: refused {tool} ({detail})")
            return False

        # A mode that enforces confirmation must not run a tool it cannot
        # record. yolo is explicitly permissive and stays best-effort.
        if not audit_ok and self.mode != "yolo" and risk != "safe":
            self._note(f"refused {tool}: audit log unavailable, tool cannot be recorded")
            return False

        if risk == "safe":
            return True

        if self.mode == "yolo":
            return True

        # "confirm" risk (hidden payloads, e.g. interpreter one-liners)
        # never rides the auto-mode mutating allowance: it always prompts
        # here, and only a session-level "always" for that key stops asking.
        if self.mode == "auto" and risk == "mutating":
            return True
        key = self._key(tool, arguments)
        if key in self.session_allowed:
            return True

        return self._confirm(tool, detail, risk, key)

    def reset_session(self) -> None:
        self.session_allowed.clear()

    def _confirm(self, tool: str, detail: str, risk: str, key: str) -> bool:
        tag = "DESTRUCTIVE " if risk == "destructive" else ""
        answer = self._ask(f"{tag}{tool}: {detail}")
        if answer == "a":
            self.session_allowed.add(key)
            return True
        return answer == "y"

    @staticmethod
    def _key(tool: str, arguments: dict[str, Any]) -> str:
        if tool == "run_command":
            # Alias spellings share the canonical key, matching classify
            # and the loop's repair pass.
            command = canonical_command(arguments)
            # Lowercase verb for matching; preserve path case.
            parts = command.split()
            if parts:
                parts[0] = parts[0].lower()
                # Lowercase flags, keep path-like tokens as-is.
                normalized = [parts[0]]
                for tok in parts[1:]:
                    if tok.startswith("-") or tok in ("|", "&&", ";", ">", ">>", "<"):
                        normalized.append(tok.lower())
                    elif "/" in tok or "\\" in tok or tok.endswith((".py", ".js", ".ts", ".json", ".md", ".txt")):
                        normalized.append(tok)
                    else:
                        normalized.append(tok.lower() if tok.isalpha() else tok)
                return f"run_command::{' '.join(normalized)}"
            return "run_command::"
        if tool in ("write_file", "edit_file"):
            # Preserve case; normalize separators for consistent keys.
            p = _canonical_path(arguments).replace("\\", "/")
            import posixpath

            # POSIX normalization governs Windows-style paths too, so both
            # spellings of a path produce the same session key.
            p = posixpath.normpath(p)
            if p == ".":
                p = ""
            return f"{tool}::{p}"
        if tool == "kill_shell":
            # Key by target: one "always" must not blanket-authorize every
            # future kill of any process for the session.
            target = (
                arguments.get("task_id")
                or arguments.get("pid")
                or arguments.get("port")
                or ""
            )
            return f"kill_shell::{target}"
        return tool


