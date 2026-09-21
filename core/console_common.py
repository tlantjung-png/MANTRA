# Shared console primitives. Import through core.console, not directly.

"""Console primitives shared by the command modules: help text, menus,
secret/line readers, endpoint naming, and the keyless-host rule."""

from __future__ import annotations

import os
import re
import sys
from contextlib import contextmanager
from typing import Any
from urllib.parse import urlparse

from core.agent.keys import has_stored
from core.agent.settings import endpoint_name_for_url, endpoints as known_endpoints
from core.llm import KEYLESS_HOSTS
from core.term import safe_write

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # Type-only: console.py imports this module back, so a runtime import
    # here would be circular. ruff F821 is otherwise raised on every
    # forward reference in signatures.
    from core.console import ConsoleSession


# Single definition of the project root: the console facade, the
# persistence mixin, and the data-file locator must agree on it.
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# Commands are listed alphabetically, here and in SLASH_COMMANDS below, so a
# reader can find one without scanning the whole list.
HELP_TEXT = """Commands:
  /approve              set approval mode (yolo is the default: yolo|default|auto|plan)
  /clear                clear conversation
  /compact              summarise conversation
  /cost                 show token usage
  /diff                 show uncommitted changes
  /exit                 exit (Ctrl+C)
  /help                 show help
  /mcp                  external tool servers: list, /mcp tools <server>
  /model                provider & model: add endpoint, pick a model, replace key
  /sessions             saved conversations: browse and resume
  /skills <name>        attach skill; /skills + space, Tab filter
  /suggestions on|off   post-task next-step line (bare: show state)
  /undo                 discard changes (confirm)
  /workspace            show workspace path + files
  /                     same as /help

Reference files with @ in any message:
  explain @src/app.py
  why is @tests/test_smoke.py failing?
  review @src/*.py
  what's in @docs/

@path attaches a file's contents, a directory listing, or every file a glob
matches. Paths are relative to the workspace and cannot escape it.

Anything else you type is sent to the agent as a task.
Ctrl+C once stops the current run; twice leaves the console.
Escape also stops the current run while it is streaming."""


# @name mentions: a bare word, path, or glob. The lookbehind keeps
# "user@example.com" and "a@b" from being read as file references.
# Trailing punctuation like .,;:!?) is stripped later so "@src/app.py." at
# the end of a sentence still resolves to the file.
MENTION_RE = re.compile(r"(?<![\w])@([A-Za-z0-9_][\w.:/\\\-*]*)")
# Characters that are often trailing punctuation after a mention and
# should not be considered part of the path.
_MENTION_TRIM = ".,;:!?)]'\"`"


# There are no built-in endpoints. Everything MANTRA knows about lives
# in the user's own settings file, which is hand-editable and is
# written by /model. See core/agent/settings.py for the shape.

# KEYLESS_HOSTS is imported from core.llm above: one tuple shared by
# the setup flow and the request path, so they cannot drift apart.


def provider_needs_key(base_url: str, api_key_env: str) -> bool:
    """False for local endpoints, which simply do not check a key."""
    if not api_key_env:
        return False
    # Compare the parsed hostname exactly, never a substring of the whole
    # URL: a substring match would treat https://evil-localhost.proxy.com
    # as keyless. A dotted-suffix .localhost name resolves to the loopback
    # interface (RFC 6761) and is keyless too.
    hostname = (urlparse(base_url or "").hostname or "").lower()
    if hostname in KEYLESS_HOSTS or hostname.endswith(".localhost"):
        return False
    return True


# Alphabetical by command name; HELP_TEXT keeps the same order.
SLASH_COMMANDS = [
    ("/approve", "set approve mode"),
    ("/clear", "clear chat"),
    ("/compact", "summarise chat"),
    ("/cost", "show usage"),
    ("/diff", "show changes"),
    ("/exit", "exit"),
    ("/help", "show help"),
    ("/mcp", "external tool servers: list, tools"),
    ("/model", "provider & model: add endpoint, pick a model, replace key"),
    ("/sessions", "saved conversations: browse and resume"),
    ("/skills", "attach skill"),
    ("/suggestions", "next-step suggestions on|off"),
    ("/undo", "discard changes"),
    ("/workspace", "show workspace"),
]

_SKIP_DIRS = {
    ".git", "__pycache__", "node_modules", ".venv", "venv",
    ".mypy_cache", ".pytest_cache", ".tox", "dist", "build", ".idea", ".vscode",
}
MAX_INDEX_ENTRIES = 4000


# ------------------------------------------------------------------ commands

@contextmanager
def private_write(path: str, newline: str = "\n"):
    """Open ``path`` for writing with owner-only permissions from creation.

    The file is created with mode 0o600 instead of the process umask and
    narrowed afterwards: these files hold conversation content, which can
    include secrets seen in tool output, and the umask window is both
    observable and permanent if the process dies inside it. The final
    chmod still runs so a pre-existing, looser file is narrowed too.
    """
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline=newline) as handle:
            yield handle
    finally:
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass  # best effort: the file was already created owner-only


def _read_multiline(session: "ConsoleSession") -> str:
    """Read several lines, ended by a lone dot.

    Each line goes through the editor so that, inside the frame, they
    are drawn as frame rows rather than spilling out past the border.
    """
    session._print("  (paste your message; finish with a line containing only .)")
    lines = []
    try:
        while True:
            line = _read_choice(session, "")
            if line.strip() == ".":
                break
            lines.append(line)
    except (KeyboardInterrupt, EOFError):
        pass
    return "\n".join(lines).strip()


def _read_choice(session: "ConsoleSession", prompt_text: str) -> str:
    """Read a line from the operator; empty when there is no terminal.

    Inside the terminal application the answer is typed into a card and
    the caller's thread blocks until it is submitted. Every caller must
    tolerate an empty answer, because a piped run has nobody to answer.
    """
    if session.ui is not None:
        try:
            return session.ui.ask_line(prompt_text).strip()
        except Exception:
            # Duck-typed UI bridge (TUI or console): every caller must
            # tolerate an empty answer, so any bridge failure means empty.
            return ""
    if not sys.stdin.isatty():
        return ""
    try:
        return input(prompt_text).strip()
    except (KeyboardInterrupt, EOFError):
        return ""


def _short(count: int) -> str:
    """1234 -> 1.2k. Token counts only ever need two significant figures."""
    if count < 1000:
        return str(count)
    if count < 10_000:
        return f"{count / 1000:.1f}k"
    return f"{round(count / 1000)}k"


def _menu(
    session: "ConsoleSession",
    title: str,
    options: list[Any],
    hint: str = "",
    allow_filter: bool = True,
    cursor: int = 0,
    allow_delete: bool = False,
    on_delete: Any = None,
) -> str | None:
    """Open a selectable menu and return the chosen value.

    Inside the terminal application this is an overlay card; the caller's
    thread blocks until the operator picks or cancels. Returns None when
    cancelled or when there is nothing to show; callers treat None as
    "no change".
    """
    if not options:
        return None
    if session.ui is not None:
        return session.ui.choose(
            title,
            options,
            allow_filter=allow_filter,
            allow_delete=allow_delete,
            on_delete=on_delete,
        )
    # No application (piped runs): menus need a terminal.
    return None


def _read_one() -> str:
    """One keystroke, without waiting for a line."""
    if os.name == "nt":
        import msvcrt

        return msvcrt.getwch()
    return sys.stdin.read(1)


def _read_secret(
    session: "ConsoleSession",
    prompt_text: str,
) -> str:
    """Read a key without echoing it.

    Inside the terminal application the answer is typed into a masked
    card. Returns "" when there is no terminal to read from.
    """
    if session is not None and session.ui is not None:
        try:
            return session.ui.ask_line(prompt_text, secret=True)
        except Exception:
            # Same duck-typed bridge contract as _read_choice: empty on any
            # failure, never an exception into the command handler.
            return ""
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return ""
    # The prompt can carry a user-supplied endpoint name; on a narrow
    # locale that must degrade, not crash the secret reader.
    safe_write(prompt_text)
    sys.stdout.flush()
    chars: list[str] = []
    try:
        from core.term import raw_mode

        with raw_mode():
            while True:
                char = _read_one()
                if char in ("\r", "\n"):
                    break
                if char == "\x03":  # ctrl+c
                    raise KeyboardInterrupt
                if char == "\x04":  # ctrl+d on an empty answer cancels
                    if not chars:
                        break
                    continue
                if char in ("\x7f", "\b"):
                    if chars:
                        chars.pop()
                elif len(char) == 1 and char >= " ":
                    chars.append(char)
    except KeyboardInterrupt:
        sys.stdout.write("\n")
        sys.stdout.flush()
        return ""
    sys.stdout.write("\n")
    sys.stdout.flush()
    return "".join(chars).strip()


def _ask_secret(session: "ConsoleSession", label: str) -> str:
    """Read a key for ``session`` through its terminal application.

    Deprecated: a thin wrapper over _read_secret, kept because the
    test suite patches it directly.
    """
    return _read_secret(session, label)


def _derive_name(url: str) -> str:
    """A short handle for an endpoint, e.g. ``https://api.openai.com/v1``.

    Strips the ``api.`` and ``www.`` prefixes and the port, then keeps
    the first label: ``api.openai.com`` -> ``openai``. A path-derived
    suffix avoids collisions between endpoints on one host.
    """
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if not host:
        return "endpoint"
    for prefix in ("api.", "www."):
        if host.startswith(prefix):
            host = host[len(prefix) :]
            break
    host = host.split(":")[0]
    label = host.split(".")[0] if host else ""
    base = re.sub(r"[^a-z0-9]+", "", label) or "endpoint"
    # Add path-derived suffix when useful (last non-v segment)
    path = parsed.path.strip("/").lower()
    if path:
        parts = [p for p in path.split("/") if p not in ("v1", "v2", "v3", "api")]
        if parts:
            suffix = re.sub(r"[^a-z0-9]+", "", parts[-1])
            if suffix and suffix != base:
                return f"{base}-{suffix}"
    return base


def _derive_key_env(name: str) -> str:
    """``openai`` -> ``OPENAI_API_KEY``."""
    return re.sub(r"[^A-Z0-9]+", "_", name.upper()).strip("_") + "_API_KEY"


def _apply_endpoint_override(config: dict, url: str) -> None:
    """Point the llm section at ``url`` and pick the matching key env.

    The key env must follow the URL, not inherit the config default:
    the default is always truthy, so chaining a derivation onto it
    would never fire. Prefer the saved endpoint entry for this URL,
    then fall back to deriving from the hostname.
    """
    llm = config.setdefault("llm", {})
    llm["base_url"] = url.rstrip("/")
    ep_name = endpoint_name_for_url(url)
    if ep_name:
        entry = known_endpoints().get(ep_name) or {}
        llm["api_key_env"] = entry.get("api_key_env") or _derive_key_env(ep_name)
    else:
        llm["api_key_env"] = _derive_key_env(_derive_name(url))



def _needs_first_run(session: "ConsoleSession") -> bool:
    """True when no usable credential is configured yet.

    Used to walk a first-time operator through setup instead of greeting
    them with a dashboard that cannot reach anything.
    """
    llm = session.config.get("llm", {})
    base_url = llm.get("base_url", "")
    key_env = llm.get("api_key_env") or ""
    if not base_url:
        return True
    if provider_needs_key(base_url, key_env):
        return not (os.environ.get(key_env) or has_stored(key_env))
    return False


def _strip_leading_invisible(s: str) -> str:
    """Strip BOM, zero-width and other invisible leading characters."""
    # Include BOM, ZWSP, ZWNJ, ZWJ, NBSP, ideographic space and normal whitespace
    return s.lstrip("\ufeff\u200b\u200c\u200d\u00a0\u3000 \t\r\n")
