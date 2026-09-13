# Split out of core/console.py; import it from core.console, never from here.

"""The /skills command surface: attach, inspect, bundles, auto-routing.

Calls into console through the module namespace so test patches of the
interactive seams (menus) stay effective.
"""

from __future__ import annotations

# Calls go through the console namespace so the test suite's
# seam patches (mock.patch("core.console.<name>")) stay effective.
# Patchable seams: _menu, _skills_launch.
from core import console as _c

import core.agent.skills as skills
from core.agent.loop import RunResult
from core.agent.settings import set_skills_prefs, skills_prefs
from core.tui.overlays import Option

def _skills(session: "ConsoleSession", argument: str) -> None:
    """/skills — EASY: type "/skills " + Tab shows all, type to filter by name/type.

    Usage:
      /skills              — list all
      /skills <name>       — attach skill in 1 step (e.g. /skills tdd)
      /skill is alias for /skills
    """
    parts = argument.split() if argument else []
    raw_head = parts[0] if parts else ""
    head = raw_head.lower() if parts else ""
    rest = " ".join(parts[1:]).strip()
    # Friendly aliases so manual is forgiving. "run" is the use path: a
    # bare skill name is a one-shot run, not a bundle launch.
    alias = {"attach": "use", "detach": "clear", "ls": "list", "info": "show", "cat": "show", "rm": "clear", "search": "find", "route": "find", "run": "use", "apply": "use", "on": "use"}
    head = alias.get(head, head)

    if head in ("help", "?", "-h", "--help", "manual", "h"):
        _skills_help(session)
    elif head == "":
        _skills_dashboard(session)
    elif head == "list":
        _skills_list(session)
    elif head == "show":
        _skills_show(session, rest)
    elif head == "use":
        # Support "use all" / "use --all" for one-step bulk attach
        if rest.lower() in ("all", "--all", "autoload", "autoloadall", "allskills", "autoloadallskill"):
            _skills_use_all(session)
        else:
            _skills_use(session, rest)
    elif head in ("bundles", "bundle"):
        _skills_bundles(session)
    elif head == "launch":
        _c._skills_launch(session, rest)
    elif head == "find":
        _skills_find(session, rest)
    elif head == "auto":
        _skills_auto(session, rest)
    elif head in ("all", "autoload", "autoloadall", "allskills", "autoloadallskill"):
        # Direct one-step: /skills all → attach all
        _skills_use_all(session)
    elif head in ("clear", "off", "drop"):
        if session.active_skills:
            session._print(session.style.dim("  skills detached: " + ", ".join(session.active_skills)))
            session.active_skills = []
        else:
            session._print(session.style.dim("  no skills attached"))
    else:
        # EASY: /skills <name> — attach in 1 step, no "use" needed
        # e.g. /skills tdd  →  same as /skills use tdd
        if skills.get(argument):
            _skills_use(session, argument)
            return
        if skills.get(head):
            # The first token is an exact skill name: attach it (bare),
            # or run it once when a reference follows. The full argument
            # carries the reference through to the one-shot path.
            _skills_use(session, argument)
            return
        # Try find — if single hit, use it; else show options
        hits = skills.find(argument, limit=5)
        if len(hits) == 1:
            _skills_use(session, hits[0].name)
            return
        elif hits:
            _skills_find(session, argument)
            return
        else:
            _skills_show(session, argument)
            return


def _skills_help(session: "ConsoleSession") -> None:
    s = session.style
    session._print(s.bold("  /skills — EASY"))
    session._print(s.dim("  Skills are reusable procedures that ride along with a turn."))
    session._print("")
    session._print("    /skills              — list all")
    session._print("    /skills <name>       — attach in 1 step  (e.g. /skills tdd)")
    session._print("")
    session._print(s.dim("  type /skills + space, Tab shows all, type to filter by name/type"))


def _skills_dashboard(session: "ConsoleSession") -> None:
    # EASY: no menu, just list.  "/skills space" completion does the filtering.
    _skills_list(session)


def _skills_list(session: "ConsoleSession") -> None:
    known = skills.list_skills()
    if not known:
        session._print(session.style.dim("  no skills found"))
        session._print(session.style.dim(f"  looked in: {', '.join(str(r) for r in skills.roots())}"))
        session._print(session.style.dim(f"  set {skills._OVERRIDE_ENV} to point at a skills directory"))
        return
    index = skills.routing_table()
    session._print(session.style.bold(f"  skills ({len(known)})"))
    for skill in known:
        entry = index.get(skill.name.lower(), {})
        function = entry.get("function") or skill.description
        function = " ".join(str(function).split())
        if len(function) > 68:
            function = function[:65].rstrip() + "..."
        mark = "*" if skill.name.lower() in session.active_skills else " "
        session._print(f" {mark} {skill.name:<18} {session.style.dim(function)}")
    session._print("")
    session._print(session.style.dim("  EASY: /skills <name> to attach  ·  type /skills + space, Tab to filter"))


def _skills_show(session: "ConsoleSession", name: str) -> None:
    if not name:
        _skills_list(session)
        return
    found = skills.get(name)
    if found is None:
        cands = skills.find(name, limit=5)
        session._print(session.style.ember(f"  no skill named '{name}'"))
        if cands:
            session._print(session.style.dim("  did you mean: " + ", ".join(c.name for c in cands)))
        return
    session._print(session.style.bold(f"  {found.name}"))
    if found.description:
        session._print(session.style.dim(f"  {found.description}"))
    meta = []
    if found.version:
        meta.append(f"v{found.version}")
    if found.resources:
        meta.append("bundles " + ", ".join(found.resources))
    if meta:
        session._print(session.style.dim("  " + " · ".join(meta)))
    body = found.body.strip()
    if not body:
        session._print(session.style.dim("  (empty)"))
        return
    session._print("")
    # Indented so the procedure reads as a block inside the frame
    # rather than as more console output.
    for line in body.split("\n"):
        session._print("  " + line.rstrip())
    session._print("")
    session._print(session.style.dim(f"  /skills use {found.name} to attach it"))


# Skills already flagged as coming from an external (non-bundled) root;
# each is warned about once per process, not on every attachment.
_UNTRUSTED_SKILL_WARNED: set[str] = set()


def _warn_untrusted_skill(session: "ConsoleSession", skill) -> None:
    """Warn once per external skill: its procedure is prompt input."""
    if skills.is_bundled(skill):
        return
    key = skill.name.lower()
    if key in _UNTRUSTED_SKILL_WARNED:
        return
    _UNTRUSTED_SKILL_WARNED.add(key)
    root = str(skill.root) if skill.root else "an external root"
    session._print(
        session.style.warn(
            f"  '{skill.name}' comes from {root} - its procedure is injected "
            "into the model prompt as instructions. Treat it as untrusted input."
        )
    )


def _skills_use(session: "ConsoleSession", name: str) -> None:
    if not name:
        session._print(session.style.dim("  usage: /skills <name>"))
        return
    found = skills.get(name)
    rest = ""
    if found is None:
        # Tolerate a trailing reference ("use code-review @flappy.py"):
        # the first token being an exact skill name is what matters.
        head = name.split()[0].lower()
        if head != name.lower():
            found = skills.get(head)
            rest = name.split(maxsplit=1)[1].strip()
    if found is None:
        cands = skills.find(name, limit=5)
        session._print(session.style.ember(f"  no skill named '{name}'"))
        if cands:
            session._print(session.style.dim("  did you mean: " + ", ".join(c.name for c in cands)))
        return
    if rest:
        # A trailing reference ("/skills code-review @flappy.py") is a
        # one-shot: run the skill once on those files, then detach - not
        # an every-turn attachment.
        _skills_one_shot(session, found, rest)
        return
    key = found.name.lower()
    if key in session.active_skills:
        session._print(session.style.dim(f"  '{found.name}' is already attached"))
        return
    _warn_untrusted_skill(session, found)
    session.active_skills.append(key)
    session._print(session.style.dim(f"  attached '{found.name}' - it now rides along with every turn"))
    session._print(session.style.dim("  /skills clear to detach"))


def _skills_one_shot(session: "ConsoleSession", found, reference: str) -> None:
    """Run one skill once against the given reference, then detach.

    "/skills code-review @flappy.py" runs code-review on the file for a
    single turn instead of attaching it to every turn. The skill is
    marked auto-attached so the turn's end detaches it again.
    """
    key = found.name.lower()
    if key not in session.active_skills:
        session.active_skills.append(key)
        session.auto_attached.append(key)
    _warn_untrusted_skill(session, found)
    session._print(session.style.dim(f"  running '{found.name}' once on: {reference}"))
    session.handle(f"Apply the {found.name} skill to: {reference}")
    # handle detaches auto-attached skills at turn end; this guards the
    # path where the turn aborts before its own cleanup runs.
    session._detach_auto()
    session._print(session.style.dim(f"  '{found.name}' ran once and is detached"))


def _skills_use_all(session: "ConsoleSession") -> None:
    """One-step bulk attach: /skills all  (also autoload, by type)"""
    known = skills.list_skills()
    if not known:
        session._print(session.style.dim("  no skills found"))
        session._print(session.style.dim(f"  looked in: {', '.join(str(r) for r in skills.roots())}"))
        return
    # If already all attached, say so
    all_keys = [s.name.lower() for s in known]
    new = [k for k in all_keys if k not in session.active_skills]
    if not new:
        session._print(session.style.dim(f"  all {len(known)} skills already attached"))
        return
    for k in new:
        _warn_untrusted_skill(session, skills.get(k))
        session.active_skills.append(k)
    session._print(session.style.dim(f"  attached all {len(new)} skills: " + ", ".join(new)))
    session._print(session.style.dim("  bundle auto is kept — /skills auto bundle on|off to change"))
    session._print(session.style.dim("  /skills clear to detach all"))


def _skills_bundles(session: "ConsoleSession") -> None:
    bundles = skills.load_bundles()
    if not bundles:
        session._print(session.style.dim("  no bundles found (no BUNDLES.md in any skills root)"))
        return
    session._print(session.style.bold(f"  bundles ({len(bundles)})"))
    for name, steps in sorted(bundles.items()):
        session._print(f"  {name:<16} {session.style.dim(' > '.join(steps))}")
    session._print("")
    session._print(session.style.dim("  /skills launch <bundle> to run one in order"))


def _skills_launch(
    session: "ConsoleSession", name: str, initial_text: str = ""
) -> RunResult | None:
    """Run a bundle as ordered steps, attaching each skill in turn.

    ``initial_text`` is the operator's own request when the bundle was
    launched on its behalf: the first step then works on that request
    instead of a generic "apply the skill" line that names no subject —
    dropping the request would silently replace what the operator asked
    for with an instruction the model cannot act on.
    """
    if not name:
        bundles = skills.load_bundles()
        if not bundles:
            session._print(session.style.dim("  no bundles found"))
            return None
        choice = _c._menu(session, "Launch bundle", [Option(value=n, label=n, hint=" > ".join(v[:2])) for n, v in sorted(bundles.items())])
        if not choice:
            session._print(session.style.dim("  usage: /skills launch <bundle>"))
            return None
        name = choice
    steps = skills.get_bundle(name)
    if steps is None:
        bundles = skills.load_bundles()
        if bundles:
            choice = _c._menu(session, f"Bundle '{name}' not found", [Option(value=n, label=n, hint=" > ".join(v[:2])) for n, v in sorted(bundles.items())])
            if choice:
                return _c._skills_launch(session, choice)
        session._print(session.style.ember(f"  no bundle named '{name}'"))
        return None
    known = skills.load_all()
    missing = [s for s in steps if s.lower() not in known]
    if missing:
        session._print(session.style.warn(f"  bundle names skills that are not installed: {', '.join(missing)}"))
        return None
    count = len(steps)
    label = f"{count} step" if count == 1 else f"{count} steps"
    session._print(session.style.bold(f"  launching bundle '{name}' ({label})"))
    previous = list(session.active_skills)
    last: RunResult | None = None
    was_in_bundle = session.in_bundle
    # Steps are turns, and the router would otherwise re-read each step's
    # boilerplate and attach something of its own over the top.
    session.in_bundle = True
    try:
        for position, step in enumerate(steps, 1):
            skill = known[step.lower()]
            session.active_skills = [skill.name.lower()]
            session._print("")
            session._print(
                session.style.dim(f"  step {position} of {count}: {skill.name} — {skill.description}")
            )
            try:
                if position == 1 and initial_text.strip():
                    prompt = initial_text
                else:
                    prompt = f"Apply the {skill.name} skill to the current work."
                result = session.handle(prompt)
            except KeyboardInterrupt:
                session._print(session.style.warn("  bundle stopped"))
                return last
            if result is None:
                session._print(session.style.warn("  bundle stopped: the step did not complete"))
                return last
            last = result
    finally:
        session.in_bundle = was_in_bundle
        # Whatever the previous attachment was, put it back - a bundle
        # borrowing the slot must not silently drop what was there.
        session.active_skills = previous
    session._print("")
    session._print(session.style.dim(f"  bundle '{name}' finished"))
    return last


def _skills_auto(session: "ConsoleSession", argument: str) -> None:
    """/skills auto [on|off|bundle on|off]: routing without being asked."""
    parts = argument.split() if argument else []
    head = parts[0].lower() if parts else ""
    rest = " ".join(parts[1:]).strip().lower()
    stored = skills_prefs()
    auto = bool(stored.get("auto", (session.config.get("skills") or {}).get("auto", True)))
    bundles = bool(
        stored.get("auto_bundle", (session.config.get("skills") or {}).get("auto_bundle", False))
    )

    if not head:
        session._print(session.style.bold("  skill auto-routing"))
        session._print(f"  {'auto':<14} {'on' if auto else 'off'}")
        session._print(f"  {'auto bundle':<14} {'on' if bundles else 'off'}")
        session._print("")
        session._print(
            session.style.dim(
                "  on - a matching skill attaches itself to a plain prompt, for that turn only"
            )
        )
        session._print(
            session.style.dim("  off - skills are used only when you name them with /skills use")
        )
        session._print("")
        session._print(session.style.dim("  /skills auto on|off · /skills auto bundle on|off"))
        return

    if head == "bundle":
        if rest not in ("on", "off"):
            session._print(session.style.dim("  usage: /skills auto bundle on|off"))
            return
        bundles = rest == "on"
        set_skills_prefs(auto_bundle=bundles)
        session._print(session.style.dim(f"  bundle auto-launch {'on' if bundles else 'off'}"))
        if bundles:
            session._print(
                session.style.warn(
                    "  a bundle runs several turns - it will start on its own when one fits"
                )
            )
        return

    if head not in ("on", "off"):
        session._print(session.style.dim("  usage: /skills auto [on|off|bundle on|off]"))
        return
    auto = head == "on"
    set_skills_prefs(auto=auto)
    # Turning routing off is also the way to say "stop guessing at this
    # conversation", so an attachment already in force goes with it.
    if not auto:
        session._detach_auto()
    session._print(session.style.dim(f"  skill auto-routing {'on' if auto else 'off'}"))


def _skills_find(session: "ConsoleSession", query: str) -> None:
    if not query:
        session._print(session.style.dim("  usage: /skills <name>  — Tab shows all, type to filter"))
        return
    hits = skills.find(query)
    if not hits:
        session._print(session.style.dim(f"  nothing matches '{query}'"))
        return
    session._print(session.style.bold(f"  skills for: {query}"))
    index = skills.routing_table()
    for found in hits:
        function = index.get(found.name.lower(), {}).get("function") or found.description
        function = " ".join(str(function).split())
        if len(function) > 60:
            function = function[:57].rstrip() + "..."
        session._print(f"  {found.name:<18} {session.style.dim(function)}")
    session._print("")
    session._print(session.style.dim("  EASY: /skills <name> to attach"))