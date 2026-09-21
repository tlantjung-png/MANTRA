# Split out of core/console.py; import it from core.console, never from here.

"""The /model surface: endpoints, keys, and model selection.

Calls into console through the module namespace so test patches of the
interactive seams (menus, readers, catalogue fetch) stay effective.
"""

from __future__ import annotations

# Calls go through the console namespace so the test suite's
# seam patches (mock.patch("core.console.<name>")) stay effective.
# Patchable seams: _apply_model, _choose_model, _connect, _connect_remove,
# _derive_key_env, _derive_name, _is_auth_failure, _menu, _model_command,
# _pick_effort, _read_choice, _read_secret, _replace_key, _rescue_catalogue,
# _store_key, _try_fetch, _type_a_model, fetch_models, is_reasoning_model,
# provider_needs_key.
from core import console as _c

import os
import time
from urllib.parse import urlparse

from core.agent.exceptions import HarnessError
from core.config import REASONING_EFFORTS
from core.agent.keys import mask, store as store_key, stored_keys
from core.agent.settings import (
    add_endpoint,
    endpoints as known_endpoints,
    models_for,
    remove_endpoint,
    set_active,
    set_models,
    settings_path,
    validate_endpoint,
)
from core.tui.overlays import Option

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # Type-only: console.py imports this module back, so a runtime import
    # here would be circular. ruff F821 is otherwise raised on every
    # forward reference in signatures.
    from core.console import ConsoleSession

def _effort_options(current: str | None) -> list[Option]:
    """Thinking levels, with the one in force marked.

    "off" leads because it is the safe answer for a model that does not
    reason, and it is what most models should be left on.
    """
    levels = ("off", *REASONING_EFFORTS)
    options = []
    for level in levels:
        hints = []
        if level == current:
            hints.append("current")
        if level == "off":
            hints.append("send no effort field")
        elif level == "high":
            hints.append("most thorough, slowest")
        options.append(Option(value=level, hint=", ".join(hints)))
    return options


def _pick_effort(session: "ConsoleSession", model: str) -> str | None:
    """Offer a thinking level for the model just chosen.

    Always offered: gating on the model name was wrong often enough to
    be worse than asking.
    """
    current = session.config.get("llm", {}).get("reasoning_effort") or "off"
    options = _effort_options(current)
    index = next((i for i, o in enumerate(options) if o.value == current), 0)
    title = f"reasoning effort for {model}"
    if not _c.is_reasoning_model(model):
        title += session.style.dim(" (this model may ignore it)")
    chosen = _c._menu(session, title, options, allow_filter=False, cursor=index)
    return chosen or None


def _apply_model(session: "ConsoleSession", model: str, effort: str | None = None) -> None:
    """Set a model and settle reasoning as a property of that model.

    "off" is the default rather than an inherited level, so switching to
    a model that does not reason stops sending a field chosen for
    something else instead of leaving it behind.
    """
    session.set_model(model, quiet=True)
    session.set_reasoning(effort or "off", quiet=True)
    # One line for both, because model and effort are one choice here.
    effort_now = session.config["llm"].get("reasoning_effort") or "off"
    session._print(
        session.style.dim(
            f"  model is now {model}, reasoning {effort_now}"
        )
    )
    # Remember the pairing so the next /model menu opens on it.
    set_active(model=model, reasoning_effort=session.config["llm"].get("reasoning_effort"))
    # Refresh info bar.
    if session.layout is not None and session.layout.active:
        session.layout.draw_chrome()


def _try_fetch(session: "ConsoleSession") -> tuple[list[str], Exception | None]:
    """Ask the endpoint what it serves.

    Returns the models and the failure, because the two need different
    remedies: a rejected key is fixable on the spot, while a gateway
    with no catalogue just means typing the name.
    """
    llm = session.config.get("llm", {})
    base_url = llm.get("base_url", "")
    if not base_url:
        return [], None
    try:
        return _c.fetch_models(base_url, llm.get("api_key_env")), None
    except HarnessError as exc:
        session._print(session.style.dim(f"  (could not list models: {exc})"))
        return [], exc


# Ways out of an empty catalogue. These are menu entries rather than
# printed advice because the operator is already at the point where
# something is wrong - being told to edit a file is not a fix.
TYPE_A_MODEL = "+ type a model name"
RE_ENTER_KEY = "+ re-enter the api key"
SWITCH_ENDPOINT = "+ connect a different endpoint"


def _is_auth_failure(error: Exception | None) -> bool:
    """True when the endpoint refused the credential.

    Detected from the message rather than a typed exception because the
    client raises the harness-level error, and the only thing that
    matters here is which remedy to offer.
    """
    return error is not None and "refused the key" in str(error)


def _rescue_catalogue(session: "ConsoleSession", error: Exception | None) -> bool:
    """Offer a way forward when no model list can be had.

    An empty catalogue is usually a mistyped key, which is fixable here
    rather than by hand-editing the settings file.
    """
    s = session.style
    llm = session.config.get("llm", {})
    key_env = llm.get("api_key_env") or ""
    options: list[Option] = []
    # Offer the key only when it could plausibly be the cause. Offering
    # it for an unreachable host sends someone off to re-paste a key
    # that was never the problem; not offering it for an unknown cause
    # leaves the most common fix off the menu.
    if _c.provider_needs_key(llm.get("base_url", ""), key_env) and (
        error is None or _c._is_auth_failure(error)
    ):
        options.append(Option(value=RE_ENTER_KEY, hint="most likely if the key was mistyped"))
    options.append(Option(value=TYPE_A_MODEL, hint="if the endpoint hides its list"))
    options.append(Option(value=SWITCH_ENDPOINT, hint=""))
    session._print(s.warn("  no models to choose from yet"))
    choice = _c._menu(session, "how do you want to fix it?", options, allow_filter=False)
    if choice == RE_ENTER_KEY:
        if not _c._replace_key(session):
            return False
        return _c._choose_model(session)
    if choice == TYPE_A_MODEL:
        return _c._type_a_model(session)
    if choice == SWITCH_ENDPOINT:
        return _c._connect(session, [])
    session._print(s.dim(f"  or add models by hand: {settings_path()}"))
    return False


def _type_a_model(session: "ConsoleSession") -> bool:
    """Take a model name from the operator and adopt it."""
    name = _c._read_choice(session, "  model name> ").strip()
    if not name:
        session._print(session.style.dim("  cancelled"))
        return False
    _c._apply_model(session, name, _c._pick_effort(session, name))
    endpoint = session.endpoint_name
    if endpoint:
        saved = models_for(endpoint)
        if name not in saved:
            set_models(endpoint, [*saved, name])
    return True


def _choose_model(session: "ConsoleSession") -> bool:
    """Open the model menu; effort follows as part of the same choice.

    All stored endpoints' models are shown together, and picking one
    switches to its owning endpoint automatically.
    """
    llm = session.config.get("llm", {})
    base_url = llm.get("base_url", "")
    # Build combined catalogue from all stored endpoints
    all_by_model: dict[str, str] = {}  # model -> provider
    for ep_name, entry in known_endpoints().items():
        for m in entry.get("models", []) or []:
            if m not in all_by_model:
                all_by_model[m] = ep_name
    # Prefer live catalogue from current endpoint, merged into all
    name = session.endpoint_name
    fetched, error = _c._try_fetch(session)
    if fetched:
        session.known_models = fetched
        if name:
            set_models(name, fetched)
        for m in fetched:
            if m not in all_by_model:
                all_by_model[m] = name or "current"
        # A very large catalogue (an aggregator like OpenRouter) is a
        # wall of names: let the operator choose how to find a model
        # before the full menu opens.
        if len(fetched) > _LARGE_MODEL_CATALOGUE and name:
            how = _c._menu(session, f"{len(fetched)} models at {name}", [
                Option(value=SHOW_ALL_MODELS, hint="type to filter as you go"),
                Option(value=SHOW_FIRST_MODELS, hint=f"first {_FIRST_MODEL_WINDOW} from the list"),
                Option(value=TYPE_A_MODEL, hint="if you already know the name"),
            ], allow_filter=False)
            if how == TYPE_A_MODEL:
                return _c._type_a_model(session)
            if how == SHOW_FIRST_MODELS:
                window = set(sorted(fetched)[:_FIRST_MODEL_WINDOW])
                all_by_model = {m: p for m, p in all_by_model.items() if m in window or p != name}
            elif not how:
                return False
            # SHOW_ALL_MODELS (or a cancel) falls through to the menu.
    elif not all_by_model:
        # No stored models anywhere and fetch failed
        if not base_url:
            session._print(session.style.warn("  no endpoint configured - /model first"))
            return False
        return _c._rescue_catalogue(session, error)
    else:
        # Use stored catalogue when fetch fails but we have something
        session.known_models = list(all_by_model.keys())

    # Prepare options with provider hint
    current = llm.get("model", "")
    options: list[Option] = []
    for m, provider in sorted(all_by_model.items(), key=lambda x: x[0].lower()):
        hint = provider
        if m == current:
            hint = (hint + ", current").strip(" ,") if hint else "current"
        if _c.is_reasoning_model(m):
            hint = (hint + ", thinks").strip(" ,") if hint else "thinks"
        options.append(Option(value=m, hint=hint))
    # Always offer typing a name
    options.append(Option(value=TYPE_A_MODEL, hint="not listed above"))
    title = "models - all providers" if len(known_endpoints()) > 1 else f"models at {base_url}" if base_url else "models"

    def _on_delete_model(model: str) -> None:
        provider = all_by_model.get(model)
        if not provider:
            return
        entry = known_endpoints().get(provider)
        if not entry:
            return
        models = [m for m in entry.get("models", []) if m != model]
        set_models(provider, models)
        session._print(session.style.dim(f"  removed model '{model}' from {provider}"))
        # keep map in sync
        all_by_model.pop(model, None)

    chosen = _c._menu(session, title, options, allow_delete=True, on_delete=_on_delete_model)
    if not chosen:
        return False
    if chosen == TYPE_A_MODEL:
        return _c._type_a_model(session)
    # Auto-switch endpoint if model belongs to different provider
    provider = all_by_model.get(chosen)
    if provider and provider != name:
        session._print(session.style.dim(f"  switching to {provider} for {chosen}"))
        session.use_endpoint(provider)
    _c._apply_model(session, chosen, _c._pick_effort(session, chosen))
    return True


NEW_ENDPOINT = "+ add a new endpoint"


def _endpoint_options(session: "ConsoleSession") -> list[Option]:
    """Saved endpoints, alphabetically, with an entry to add another."""
    known = known_endpoints()
    current = session.endpoint_name
    options = []
    for name in sorted(known):
        entry = known[name]
        hint = "current" if name == current else entry.get("base_url", "")
        options.append(Option(value=name, hint=hint))
    options.append(Option(value=NEW_ENDPOINT, hint=""))
    return options


def _connect_choose_endpoint(session: "ConsoleSession") -> str | None:
    """Menu over saved endpoints. None means the operator cancelled. Press d to remove."""
    known = known_endpoints()
    if not known:
        return NEW_ENDPOINT

    def _on_delete(name: str) -> None:
        # Remove endpoint and its key in one go (baseurl + key together)
        entry = known_endpoints().get(name.lower())
        key_env = entry.get("api_key_env") if entry else ""
        removed = remove_endpoint(name.lower())
        if removed:
            # Also remove stored key if only this endpoint used it
            key_removed = False
            key_removal_failed = False
            still_used = False
            if key_env:
                still_used = any(
                    e.get("api_key_env") == key_env
                    for n, e in known_endpoints().items()
                    if n != name.lower()
                )
                if not still_used:
                    try:
                        from core.agent.keys import remove as remove_key

                        remove_key(key_env)
                        key_removed = True
                    except Exception:
                        # Credential removal must not block endpoint removal;
                        # flag it so the operator can clean the store by hand.
                        key_removal_failed = True
            if key_removed:
                session._print(session.style.dim(f"  removed '{name}' (+ key {key_env})"))
            elif key_removal_failed:
                session._print(session.style.dim(f"  removed '{name}'"))
                session._print(session.style.warn(f"  warning: could not remove stored key {key_env}"))
            elif key_env and still_used:
                session._print(session.style.dim(f"  removed '{name}' (kept key {key_env} - still used by another endpoint)"))
            else:
                session._print(session.style.dim(f"  removed '{name}'"))

    return _c._menu(
        session,
        "endpoints",
        _endpoint_options(session),
        allow_filter=False,
        allow_delete=True,
        on_delete=_on_delete,
    )


def _store_key(session: "ConsoleSession", key_env: str, key: str) -> bool:
    """Persist a key; a failed write surfaces as an error line, not a crash."""
    try:
        store_key(key_env, key)
        return True
    except OSError as exc:
        session._print(session.style.ember(f"  could not save the key: {exc}"))
        return False


def _replace_key(session: "ConsoleSession", name: str = "") -> bool:
    """Store a key over whatever is already there.

    Always prompts, even when a key is stored: a mistyped key used to
    be permanent because /model skipped the prompt once the store
    held any value at all.
    """
    s = session.style
    llm = session.config.get("llm", {})
    name = (name or session.endpoint_name or "").lower()
    entry = known_endpoints().get(name)
    if entry is not None:
        base_url = entry.get("base_url", "")
        key_env = entry.get("api_key_env") or _c._derive_key_env(name)
    else:
        base_url = llm.get("base_url", "")
        if not base_url:
            session._print(s.warn("  no endpoint to set a key for - /model first"))
            return False
        name = _c._derive_name(base_url)
        key_env = _c._derive_key_env(name)

    if not _c.provider_needs_key(base_url, key_env):
        session._print(s.dim(f"  {base_url} does not take a key"))
        return False
    if os.environ.get(key_env):
        # The environment wins over the store, so a file key cannot
        # rescue a bad value that came from a variable. Say so rather
        # than accepting a key that will never be used.
        session._print(s.warn(f"  ${key_env} is set in this shell and wins over stored keys"))
        session._print(s.dim(f"  clear it with: set {key_env}=   then re-run /model key"))

    stored = stored_keys().get(key_env, "")
    if stored:
        session._print(s.dim(f"  {name} is using {mask(stored)}"))
    try:
        key = _c._read_secret(session, f"  new api key for {name} (hidden, enter confirms)> ").strip()
        if not key:
            # Fallback for tests that mock _c._read_choice instead of _c._read_secret
            alt = _c._read_choice(session, f"  new api key for {name} (visible, blank cancels)> ").strip()
            if alt:
                key = alt
    except Exception:
        # Hidden read failed (no TUI bridge, interrupted): fall back to the
        # visible prompt so a key can still be entered at all.
        key = _c._read_choice(session, f"  new api key for {name} (visible, blank cancels)> ").strip()
    if not key:
        session._print(s.dim("  cancelled"))
        return False
    if not _c._store_key(session, key_env, key):
        return False
    session._print(s.dim(f"  key stored ({mask(key)})"))
    return True


def _connect_new(session: "ConsoleSession", url: str = "", key: str = "", model: str = "") -> bool:
    """Walk through adding an endpoint: URL, key, then pick a model."""
    s = session.style
    if not url:
        url = _c._read_choice(session, "  endpoint url (e.g. https://api.openai.com/v1)> ").strip()
        if not url:
            session._print(s.dim("  tip: paste a full URL, or try /model list to see saved ones"))
            session._print(s.dim("  examples: /model https://api.openai.com/v1 ; /model https://api.meta.ai/v1"))
            return False
    if "://" not in url:
        # Tolerate a host typed without a scheme rather than failing.
        # Anything that already carries a scheme is left alone, so a
        # wrong one is reported instead of being prefixed into nonsense.
        url = "https://" + url
    url = url.rstrip("/")
    problem = validate_endpoint({"base_url": url})
    if problem:
        session._print(s.ember(f"  {problem}"))
        return False
    if not urlparse(url).hostname:
        session._print(s.ember("  that does not look like a hostname"))
        return False

    name = _c._derive_name(url)
    key_env = _c._derive_key_env(name)

    if key:
        if not _c._store_key(session, key_env, key):
            return False
        session._print(s.dim(f"  key stored ({mask(key)})"))
    elif _c.provider_needs_key(url, key_env):
        # Always prompt for key in interactive add (visible), show existing
        existing = os.environ.get(key_env) or stored_keys().get(key_env, "")
        if existing:
            session._print(s.dim(f"  current key {mask(existing)} ({key_env}) - press enter to keep, or paste new"))
        key = _c._read_choice(session, f"  api key for {name}> ").strip()
        if key:
            if not _c._store_key(session, key_env, key):
                return False
            # Also clear env if it was wrong and now stored wins after restart; advise
            if os.environ.get(key_env) and os.environ.get(key_env) != key:
                session._print(s.warn(f"  note: ${key_env} env still set and wins until you restart shell"))
            session._print(s.dim(f"  key stored ({mask(key)})"))
        elif not existing:
            session._print(s.warn("  no key given - skipping the fetch"))
            session._print(s.dim("  store one later with /model key, or add one to"))
            session._print(s.dim(f"  {settings_path()}"))
            return False
        # else keep existing

    try:
        add_endpoint(name, url, key_env, note=f"added {time.strftime('%Y-%m-%d')}")
    except ValueError as exc:
        session._print(s.ember(f"  {exc}"))
        return False
    session._print(s.dim(f"  saved '{name}' > {url}"))
    if not session.use_endpoint(name):
        return False
    # If a model was supplied inline (3-arg form), use it directly
    if model:
        _c._apply_model(session, model)
        session.refresh_title()
        return True
    session.refresh_title()
    # Endpoint + key done — now fetch models in same flow
    session._print(s.dim("  fetching models…"))
    picked = _c._choose_model(session)
    # If fetch failed or user cancelled, hint one-liner
    if not picked:
        session._print(s.dim("  tip: /model <url> <key> <model> to set in one go"))
    return picked


def _connect(session: "ConsoleSession", args: list[str]) -> bool:
    """Add or switch endpoints, then pick a model from the menu.

    Only a base URL and a key are needed; the catalogue comes from the
    endpoint itself. /model is the command surface; this is the internal
    endpoint flow behind it.
    """
    # Subcommands first — before treating args as url
    if args and args[0].lower() == "remove":
        if len(args) >= 2:
            _c._connect_remove(session, args[1])
        else:
            eps = sorted(known_endpoints().keys())
            if not eps:
                session._print(session.style.dim("  no endpoints to remove"))
            else:
                choice = _c._menu(session, "Remove endpoint", [Option(value=n, label=n, hint=known_endpoints()[n].get("base_url","")) for n in eps])
                if choice:
                    _c._connect_remove(session, choice)
        return True
    if args and args[0].lower() == "key":
        if len(args) >= 3:
            # /model key <name> <key> — direct replace no prompt.
            # The endpoint may carry a custom api_key_env; deriving the
            # env name from the short name would store an orphan key
            # under the wrong variable that the resolver never reads.
            entry = known_endpoints().get(args[1].lower())
            key_env = (entry or {}).get("api_key_env") or _c._derive_key_env(args[1].lower())
            if not _c._store_key(session, key_env, args[2]):
                return False
            # The shared mask degrades short values to asterisks; slicing
            # fixed windows here once printed short keys almost in full.
            session._print(session.style.dim(f"  key stored for {args[1].lower()} ({mask(args[2])})"))
            return True
        if len(args) == 2:
            return _c._replace_key(session, args[1])
        return _c._replace_key(session, "")
    if len(args) >= 3:
        # Scripted form: /model <url> <key> <model>
        return _connect_new(session, args[0], args[1], args[2])
    if len(args) >= 2:
        # Scripted form: /model <url> <key>
        return _connect_new(session, args[0], args[1])
    if len(args) == 1:
        if args[0].lower() == "list":
            session.show_endpoints()
            return True
        # A saved endpoint's own name means "switch to it". Saved names
        # never contain a dot or a slash, so an exact match cannot be a
        # host someone meant to add - and without this, `/model groq`
        # would quietly invent https://groq and ask for a key.
        saved = known_endpoints().get(args[0].lower())
        if saved:
            return session.use_endpoint(args[0].lower())
        return _connect_new(session, args[0])

    choice = _connect_choose_endpoint(session)
    if not choice:
        return False
    if choice == NEW_ENDPOINT:
        return _connect_new(session)
    if not session.use_endpoint(choice):
        return False
    # Refresh info bar after endpoint change.
    if session.layout is not None and session.layout.active:
        session.layout.draw_chrome()
    return _c._choose_model(session)


def _connect_remove(session: "ConsoleSession", name: str) -> None:
    if remove_endpoint(name.lower()):
        session._print(session.style.dim(f"  removed '{name}'"))
    else:
        session._print(session.style.warn(f"  no endpoint named '{name}'"))


# ---------------------------------------------------------------- /model

# A catalogue this large (an aggregator such as OpenRouter) is a wall of
# names: offer the operator a choice of how to find a model instead of
# dumping the whole list into a menu.
_LARGE_MODEL_CATALOGUE = 40
_FIRST_MODEL_WINDOW = 20

ADD_ENDPOINT = "+ add a provider / endpoint"
PICK_MODEL = "pick a model"
SWITCH_ENDPOINT_ENTRY = "switch endpoint"
REPLACE_KEY_ENTRY = "replace the api key"
REMOVE_ENDPOINT_ENTRY = "remove an endpoint"
SHOW_ALL_MODELS = "pick from the full list"
SHOW_FIRST_MODELS = "show the first few models"


def _model_help(session: "ConsoleSession") -> None:
    """The merged provider-and-model help for /model."""
    s = session.style
    session._print(s.bold("  /model - provider & model, one place"))
    session._print(s.dim("  usage:"))
    session._print("    /model                          - menu: add a provider, pick a model")
    session._print("    /model <name>                   - switch to a model directly")
    session._print("    /model <name> <effort>          - switch and set reasoning")
    session._print("    /model <url> [key] [model]      - add a provider, then pick a model")
    session._print("    /model <endpoint-name>          - switch provider")
    session._print("    /model list                     - show saved providers")
    session._print("    /model remove <name>            - delete a provider")
    session._print("    /model key [name]               - replace a stored key")
    session._print(s.dim("  effort: off | minimal | low | medium | high | xhigh"))
    session._print(s.dim("  examples: /model gpt-4o ; /model gpt-5 high ; /model https://api.openai.com/v1"))


def _model_master(session: "ConsoleSession") -> bool:
    """The simple entry for non-technical operators: one menu manages the
    provider and the model together."""
    eps = known_endpoints()
    model = session.config.get("llm", {}).get("model", "?")
    current = session.endpoint_name or "no endpoint"
    options = [Option(value=ADD_ENDPOINT, hint="paste a URL like https://api.openai.com/v1")]
    if eps:
        options.append(Option(value=PICK_MODEL, hint="from the endpoint's catalogue"))
        options.append(Option(value=TYPE_A_MODEL, hint="not listed above"))
        if len(eps) > 1:
            options.append(Option(value=SWITCH_ENDPOINT_ENTRY, hint=""))
        options.append(Option(value=REPLACE_KEY_ENTRY, hint=""))
        options.append(Option(value=REMOVE_ENDPOINT_ENTRY, hint=""))
    choice = _c._menu(session, f"model & endpoint - {current}, {model}", options, allow_filter=False)
    if not choice:
        llm = session.config.get("llm", {})
        session._print(f"model      {llm.get('model', '?')}")
        session._print(f"endpoint   {llm.get('base_url', '?')}")
        return False
    if choice == ADD_ENDPOINT:
        _connect_new(session)
    elif choice == PICK_MODEL:
        _c._choose_model(session)
    elif choice == TYPE_A_MODEL:
        _c._type_a_model(session)
    elif choice == SWITCH_ENDPOINT_ENTRY:
        picked = _connect_choose_endpoint(session)
        if not picked:
            return False
        if picked == NEW_ENDPOINT:
            _connect_new(session)
        elif session.use_endpoint(picked):
            _c._choose_model(session)
    elif choice == REPLACE_KEY_ENTRY:
        names = sorted(known_endpoints().keys())
        if len(names) == 1:
            _c._replace_key(session, names[0])
        elif names:
            picked = _c._menu(session, "Replace key for", [Option(value=n, label=n, hint=known_endpoints()[n].get("base_url", "")) for n in names])
            if picked:
                _c._replace_key(session, picked)
        else:
            session._print(session.style.dim("  no endpoints yet - add one with /model"))
    elif choice == REMOVE_ENDPOINT_ENTRY:
        names = sorted(known_endpoints().keys())
        if not names:
            session._print(session.style.dim("  no endpoints to remove"))
        else:
            picked = _c._menu(session, "Remove endpoint", [Option(value=n, label=n, hint=known_endpoints()[n].get("base_url", "")) for n in names])
            if picked:
                _c._connect_remove(session, picked)
    return True


def _model_command(session: "ConsoleSession", parts: list[str]) -> bool:
    """The single provider-and-model command.

    The bare form opens one simple menu; one-liners cover the rest so
    scripts and power users keep working.
    """
    if not parts:
        return _model_master(session)
    first = parts[0].lower()
    if first in ("help", "-h", "--help", "?", "h"):
        _model_help(session)
        return True
    # Endpoint management and the <url> [key] [model] add-form reuse the
    # internal _c._connect flow; only a URL (or a management subcommand)
    # belongs there.
    if first in ("list", "remove", "key") or "://" in first:
        return _c._connect(session, parts)
    saved = known_endpoints().get(first)
    if saved:
        if len(parts) >= 2:
            # /model <saved-endpoint> <model> — switch endpoint, then
            # apply the model. Never route this into the URL-add form:
            # _connect_new would overwrite the saved endpoint's base_url
            # and store the model name as its API key.
            if not session.use_endpoint(first):
                return False
            _c._apply_model(session, parts[1], _c._pick_effort(session, parts[1]))
            return True
        # /model <endpoint-name> — switch provider (then pick a model).
        return _c._connect(session, parts)
    # Otherwise it is a model name, with an optional reasoning effort.
    effort = parts[1] if len(parts) > 1 and parts[1] in (*REASONING_EFFORTS, "off") else None
    if effort is not None:
        _c._apply_model(session, parts[0], effort)
    else:
        # A bare model name still asks about reasoning as part of the
        # same choice.
        _c._apply_model(session, parts[0], _c._pick_effort(session, parts[0]))
    return True