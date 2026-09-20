# Split out of core/console.py; import it from core.console, never from here.

"""Endpoint and model management on the session: switching models,
reasoning effort, saved endpoints, and the missing-key warnings."""

from __future__ import annotations

import os

from core.agent.exceptions import HarnessError
from core.agent.keys import has_stored, mask, stored_keys
from core.agent.settings import (
    endpoint_name_for_url,
    endpoints as known_endpoints,
    set_active,
    settings_path,
)
from core.config import REASONING_EFFORTS
from core.console_common import provider_needs_key
from core.registry import build_llm

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # Type-only: console.py imports this module back, so a runtime import
    # here would be circular. ruff F821 is otherwise raised on every
    # forward reference in signatures.
    from core.console import ConsoleSession


class EndpointsMixin:
    """Model, reasoning, and saved-endpoint switching."""

    def set_model(self: "ConsoleSession", name: str, quiet: bool = False) -> None:
        self.config.setdefault("llm", {})["model"] = name
        try:
            self.llm = build_llm(self.config["llm"])
        except HarnessError as exc:
            self._print(self.style.ember(f"  could not switch model: {exc}"))
            return
        if not quiet:
            self._print(self.style.dim(f"  model is now {name}"))
        self.refresh_title()
        self._warn_if_key_missing()
        # Wire top info instantly so MODEL shows new name without waiting for next turn
        if self.layout is not None and getattr(self.layout, "active", False):
            try:
                self.layout.draw_chrome()
            except Exception:
                pass  # duck-typed bridge: chrome redraw is cosmetic, never fatal

    def set_reasoning(self: "ConsoleSession", level: str, quiet: bool = False) -> None:
        """Set the thinking budget for the current model.

        Not every endpoint understands the field; the client sheds it on a
        400 and the reply simply arrives without the extra thinking.
        """
        wanted = level.strip().lower()
        if wanted in ("off", "none", ""):
            wanted = None
        elif wanted not in REASONING_EFFORTS:
            self._print(
                self.style.warn(f"  reasoning must be one of {', '.join(REASONING_EFFORTS)} or off")
            )
            return
        llm = self.config.setdefault("llm", {})
        llm["reasoning_effort"] = wanted
        try:
            self.llm = build_llm(llm)
        except HarnessError as exc:
            self._print(self.style.ember(f"  could not set reasoning: {exc}"))
            return
        if not quiet:
            self._print(
                self.style.dim(f"  reasoning is now {wanted}" if wanted else "  reasoning off")
            )
        self.refresh_title()
        if self.layout is not None and getattr(self.layout, "active", False):
            try:
                self.layout.draw_chrome()
            except Exception:
                pass  # duck-typed bridge: chrome redraw is cosmetic, never fatal

    def show_reasoning(self: "ConsoleSession") -> None:
        effort = self.config.get("llm", {}).get("reasoning_effort")
        current = effort or "off"
        options = " ".join(
            f"[{e}]" if e == effort else e for e in REASONING_EFFORTS
        )
        self._print(f"  reasoning  {current}   {self.style.dim(options + '  off')}")
        self._print(
            self.style.dim(
                "  higher means more thorough and slower; ignored by models "
                "that do not reason"
            )
        )

    @property
    def endpoint_name(self: "ConsoleSession") -> str:
        """Which saved endpoint the current base URL belongs to, if any."""
        llm = self.config.get("llm", {})
        return endpoint_name_for_url(llm.get("base_url", "")) or ""

    def use_endpoint(self: "ConsoleSession", name: str, model: str | None = None) -> bool:
        """Point the agent at a saved endpoint. True on success."""
        entry = known_endpoints().get(name.lower())
        if entry is None:
            self._print(self.style.warn(f"  no endpoint named '{name}'"))
            self._print(self.style.dim("  add one with /model, or list them: /model"))
            return False
        llm = self.config.setdefault("llm", {})
        llm["base_url"] = entry["base_url"]
        llm["api_key_env"] = entry.get("api_key_env") or ""
        # A model name rarely survives a move between endpoints, so take
        # the first one this endpoint offers unless one was asked for.
        llm["model"] = model or (entry.get("models") or [""])[0] or llm.get("model", "")
        try:
            self.llm = build_llm(llm)
        except HarnessError as exc:
            self._print(self.style.ember(f"  could not switch endpoint: {exc}"))
            return False
        set_active(endpoint=name.lower(), model=llm.get("model", ""))
        self._print(self.style.dim(f"  endpoint is now {entry['base_url']}"))
        self.refresh_title()
        self._warn_if_key_missing()
        if self.layout is not None and getattr(self.layout, "active", False):
            try:
                self.layout.draw_chrome()
            except Exception:
                pass  # duck-typed bridge: chrome redraw is cosmetic, never fatal
        return True

    def _warn_if_key_missing(self: "ConsoleSession") -> None:
        """Say so up front when the key variable is unset.

        Silence here turns into a confusing 401 three steps into a task.
        """
        llm = self.config.get("llm", {})
        key_env = llm.get("api_key_env") or ""
        if not provider_needs_key(llm.get("base_url", ""), key_env):
            return
        if os.environ.get(key_env) or has_stored(key_env):
            return
        self._print(self.style.warn(f"  warning: no key for ${key_env}"))
        self._print(
            self.style.dim(
                f"  store one with /model, or edit {settings_path()}"
            )
        )

    def _warn_if_any_key_missing(self: "ConsoleSession") -> None:
        """Warn once per saved endpoint whose key variable is unset.

        Called once at startup so a saved-but-unkeyed endpoint warns
        even when it is not the active one; the active endpoint is
        covered by _warn_if_key_missing on every switch.
        """
        llm = self.config.get("llm", {})
        current_url = (llm.get("base_url") or "").rstrip("/")
        warned: set[str] = set()
        for name, entry in known_endpoints().items():
            key_env = entry.get("api_key_env") or ""
            if not provider_needs_key(entry.get("base_url", ""), key_env):
                continue
            if (entry.get("base_url") or "").rstrip("/") == current_url:
                continue  # the active endpoint warns via the per-switch check
            if key_env in warned or os.environ.get(key_env) or has_stored(key_env):
                continue
            warned.add(key_env)
            self._print(self.style.warn(f"  warning: no key for ${key_env} ({name})"))
        if warned:
            self._print(
                self.style.dim(
                    f"  store keys with /model, or edit {settings_path()}"
                )
            )

    def show_endpoints(self: "ConsoleSession") -> None:
        """List what the user has configured, and where the file is."""
        llm = self.config.get("llm", {})
        current = (llm.get("base_url") or "").rstrip("/")
        known = known_endpoints()
        if not known:
            self._print(self.style.dim("  no endpoints yet - add one with /model"))
            # Name the file even here: an empty list is exactly when
            # somebody is most likely to want to type one in by hand.
            self._print(self.style.dim(f"  or add one to {settings_path()}"))
            return
        self._print(self.style.bold("  endpoints"))
        for name in sorted(known):
            entry = known[name]
            marker = "*" if entry["base_url"] == current else " "
            key_env = entry.get("api_key_env") or ""
            if not provider_needs_key(entry["base_url"], key_env):
                key_state = "no key needed"
            elif os.environ.get(key_env):
                key_state = "key in env"
            elif has_stored(key_env):
                key_state = f"stored {mask(stored_keys().get(key_env))}"
            else:
                key_state = "no key"
            count = len(entry.get("models") or [])
            model_bit = f"{count} model" + ("" if count == 1 else "s")
            tail = " · ".join(p for p in (key_state, model_bit) if p)
            self._print(
                f"  {marker} {name:<12} {entry['base_url']:<38}"
                f" {self.style.dim(tail)}"
            )
        self._print(self.style.dim("  * = current. add or switch: /model"))
        self._print(self.style.dim(f"  or edit by hand: {settings_path()}"))
