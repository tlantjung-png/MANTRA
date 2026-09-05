"""Tool approval: classify risk, gate mutations, delegate prompting."""

from __future__ import annotations

import re
from typing import Any, Callable

AskCallback = Callable[[str], str]  # returns "y" | "n" | "a"
NoteCallback = Callable[[str], None]

MUTATING_TOOLS = frozenset({"write_file", "edit_file", "run_command", "git_reset", "kill_shell"})

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
    r"\bRemove-Item\b[^\n]*-Recurse",
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

# Mid-session AGENTS.md / MEMORY.md cache invalidation guard
_FORBIDRE = [
    re.compile(r"(>|>>)\s*[^|\r\n]*AGENTS\.md|(set-content|add-content|out-file|new-item|remove-item|move-item|rename-item)\b[^|\r\n]*AGENTS\.md", re.IGNORECASE),
    re.compile(r"(set-content|add-content|out-file|new-item|remove-item|move-item|rename-item)\b[^|\r\n]*MEMORY\.md", re.IGNORECASE),
]

# Rules file support (commands.rules)
_RULES_PATH = None
_RULES_CACHE: list[dict[str, Any]] | None = None
_RULES_MTIME: float = 0.0

def _load_rules() -> list[dict[str, Any]]:
    global _RULES_CACHE, _RULES_MTIME, _RULES_PATH
    # Try env override, then MANTRA/rules/commands.rules, then ~/.mantra/rules
    import os

    candidates = []
    if os.environ.get("MANTRA_RULES_FILE"):
        candidates.append(os.environ["MANTRA_RULES_FILE"])
    # Repo-shipped rules at the project root.
    try:
        from pathlib import Path
        candidates.append(str(Path(__file__).resolve().parents[3] / "rules" / "commands.rules"))
    except Exception:
        pass
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
        return _RULES_CACHE or []

def _redact_sensitive(s: str) -> str:
    # Redact sensitive keys: known prefixes plus generic high-entropy tokens
    # that appear near assignment-like syntax or as bare values.
    s = re.sub(r"(?i)(?<![A-Za-z0-9])(sk-[a-zA-Z0-9_\-]{12,}|sk_[a-f0-9_\-]{12,}|AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{20,}|ghu_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|xox[baprs]-[A-Za-z0-9\-]{10,}|AIza[0-9A-Za-z_\-]{20,})(?![A-Za-z0-9])", "[REDACTED]", s)
    s = re.sub(r"(?i)(passw(or)?d|secret|token|apikey|api[-_]?key|authorization|bearer|credential)\s*[=:]\s*[\"']?(?:bearer\s+)?[^\"'\s,;]+", r"\1=[REDACTED]", s)
    # Generic high-entropy bare tokens near assignment (e.g. key=abc123... 20+ chars)
    s = re.sub(r"(?i)\b(key|secret|token)\s*=\s*[\"']?[A-Za-z0-9_\-]{20,}[\"']?", r"\1=[REDACTED]", s)
    # Bare bearer tokens (eyJ... JWT style) without prefix
    s = re.sub(r"\b(eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,})\b", "[REDACTED]", s)
    # Also redact any value that exactly matches a stored credential (exact match)
    try:
        from mantra.core.keys import stored_keys as _sk
        for _v in _sk().values():
            _v = (_v or "").strip()
            if len(_v) >= 12 and _v in s:
                s = s.replace(_v, "[REDACTED]")
    except Exception:
        pass
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

def _get_tokens(segment: str) -> list[str]:
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
        return _get_tokens(inner) + tokens[1:]
    # Wrapper expansion: classify the inner command only, so the wrapper
    # does not double-count the risk. Slice the token stream after the
    # switch token instead of re-searching the raw segment — the switch
    # text can appear earlier inside a quoted body or a path, and a
    # leftmost search there would truncate the real inner command.
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


# Interpreters that can execute code passed inline. A payload handed to
# one of these can do anything a destructive shell command can, spelled in
# a way the destructive patterns never see, so the invocation is gated
# behind explicit confirmation in every interactive mode rather than
# riding through as an ordinary mutation.
_INTERPRETER_TOKENS = frozenset(
    {
        "python", "python3", "pypy", "pypy3",
        "node", "deno", "bun", "perl", "ruby", "php", "lua", "jshell",
        "powershell", "pwsh",
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


def _classify_segment(segment: str) -> str:
    """Classify one command segment; safe only if the whole segment is read-only."""
    segment = segment.strip()
    if not segment:
        return "safe"
    tokens = [t.lower() for t in _get_tokens(segment)]
    if not tokens:
        return "safe"
    # Token-level destructive checks, including deletion-capable search
    # flags that no earlier pattern covers.
    for idx, tok in enumerate(tokens):
        if tok == "rm":
            for pat, _ in _DESTRUCTIVE_TOKENS:
                if _test_match(tokens, pat, start=idx):
                    return "destructive"
        elif tok == "rmdir":
            return "destructive"
        elif tok == "find":
            rest = tokens[idx + 1 :]
            if "-delete" in rest:
                return "destructive"
            if any(f in rest for f in ("-exec", "-execdir", "-ok", "-okdir")):
                return "mutating"
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

_DESTRUCTIVE_TOKENS = [
    (["rm", "-rf"], False),
    (["rm", "-r"], False),
    (["rmdir"], False),
]

def classify_command(command: str) -> str:
    """Risk of a shell command: destructive, safe, or mutating. Worst segment wins."""
    if not command or not command.strip():
        return "safe"
    # Rules file first: only regex forbid rules are enforced here.
    try:
        rules = _load_rules()
        if rules:
            worst = ""
            # Regex rules match the whole command; token allow/prompt rules
            # are not enforced here.
            for r in rules:
                if r["kind"] != "regex":
                    continue
                try:
                    if re.search(r["pattern"], command, re.IGNORECASE):
                        if worst != "forbid":
                            worst = "forbid"
                except re.error:
                    pass
            if worst == "forbid":
                return "destructive"
    except Exception:
        pass
    # Forbidre regex first — matches whole command even across wrappers/segments
    for pat in _FORBIDRE:
        try:
            if pat.search(command):
                return "destructive"
        except Exception:
            pass
    # Broad scan flags destructive text anywhere in the command; only
    # echo-quoted data is exempt.
    if _DESTRUCTIVE_RE.search(command):
        # Exclude quoted echo data: echo "rm -rf /" should be safe
        if re.match(r'^\s*echo\s+"[^"]*rm', command, re.IGNORECASE):
            pass
        elif re.match(r'^\s*echo\s+', command, re.IGNORECASE) and '"' in command and "rm" in command.lower():
            # Generic: if command is echo with quoted rm, don't flag as destructive
            # Let segment analysis decide
            pass
        else:
            return "destructive"
    # Split compounds and classify every segment; the worst verdict wins.
    # Severity ladder: destructive > confirm > mutating > safe.
    try:
        worst = "safe"
        rank = {"safe": 0, "mutating": 1, "confirm": 2, "destructive": 3}
        for seg in _split_command(command):
            risk = _classify_segment(seg)
            if risk == "destructive":
                return "destructive"
            if rank.get(risk, 1) > rank.get(worst, 0):
                worst = risk
        return worst
    except Exception:
        return "mutating"


def classify(tool: str, arguments: dict[str, Any]) -> tuple[str, str]:
    """Return ``(risk, human detail)`` for one tool call."""
    if tool not in MUTATING_TOOLS:
        return "safe", tool

    if tool in ("write_file", "edit_file"):
        path = str(arguments.get("path", "?"))
        verb = "overwrite" if tool == "write_file" else "edit"
        return "mutating", f"{verb} {path}"

    if tool == "git_reset":
        return "destructive", "discard all uncommitted changes (git checkout -- .)"

    if tool == "kill_shell":
        return "destructive", "kill background task/process"

    command = str(arguments.get("command", "")).strip()
    preview = command if len(command) <= 160 else command[:157] + "..."
    return classify_command(command), preview or "(empty command)"


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
        # Pre-tool-use logging (redacted)
        try:
            import os

            log_path = os.path.join(os.path.expanduser("~"), ".mantra", "logs", "pre-tool-use.log")
            os.makedirs(os.path.dirname(log_path), exist_ok=True)
            try:
                os.chmod(log_path, 0o600)
            except OSError:
                pass
            # Rotate before appending so the audit log cannot grow unbounded.
            # Size is sampled every 64th call; reading and rewriting the file
            # on every tool call is filesystem churn on long sessions.
            _log_check_counter = getattr(ApprovalPolicy, "_log_check_counter", 0) + 1
            ApprovalPolicy._log_check_counter = _log_check_counter
            try:
                if _log_check_counter % 64 == 0 and os.path.getsize(log_path) > 1_000_000:
                    with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                        tail = f.read()[-200_000:]
                    with open(log_path, "w", encoding="utf-8") as f:
                        f.write(tail)
            except OSError:
                pass
            safe_detail = _redact_sensitive(detail)[:200]
            safe_tool = _redact_sensitive(tool)
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(f"tool={safe_tool} risk={risk} detail={safe_detail}\n")
        except Exception:
            pass

        if self.mode == "plan" and tool in MUTATING_TOOLS:
            self._note(f"plan mode: refused {tool} ({detail})")
            return False

        if risk == "safe":
            return True

        if self.mode == "yolo":
            return True

        # "confirm" risk (interpreter one-liners and anything else whose
        # payload the pattern screen cannot see) never rides through on
        # the auto-mode mutating allowance: it always reaches the prompt
        # here, and a session-level "always" for the exact command is the
        # only way to stop being asked.
        if self.mode == "auto" and risk == "mutating":
            return True

        key = self._key(tool, arguments)
        if key in self.session_allowed:
            return True

        return self._confirm(tool, detail, risk, key)

    def allow_for_session(self, tool: str, arguments: dict[str, Any]) -> None:
        self.session_allowed.add(self._key(tool, arguments))

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
            command = str(arguments.get("command", "")).strip()
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
            p = str(arguments.get("path", "")).replace("\\", "/")
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


