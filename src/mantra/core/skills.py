"""Skills: discoverable procedures from skill directories."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_OVERRIDE_ENV = "MANTRA_SKILLS"

# Directories to check when no override is set.
_CANDIDATE_ROOTS = (
    ".mantra/skills",
)

_MARKDOWN_EXT = ".md"


@dataclass
class Skill:
    name: str = ""
    description: str = ""
    version: str = ""
    user_invocable: bool = True
    # Where the SKILL.md lives, so /skills show can re-read it and so
    # two roots shipping the same name can be told apart.
    path: Path | None = None
    root: Path | None = None
    # The procedure itself, read lazily: indexing forty skills should
    # not mean holding forty files in memory.
    _body: str | None = field(default=None, repr=False)

    @property
    def body(self) -> str:
        if self._body is None:
            self._body = _read(self.path) if self.path else ""
        return self._body

    @property
    def resources(self) -> list[str]:
        """Bundled files beside the procedure, e.g. ``scripts/read-safe.ps1``."""
        if not self.path:
            return []
        return _resources(self.path.parent)


def _read(path: Path | None) -> str:
    if path is None or not path.is_file():
        return ""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError:
        return ""


def _resources(directory: Path) -> list[str]:
    out = []
    try:
        for file in sorted(directory.rglob("*")):
            if not file.is_file() or file.name == "SKILL.md":
                continue
            out.append(file.relative_to(directory).as_posix())
    except OSError:  # pragma: no cover - unreadable tree
        return out
    return out


def roots() -> list[Path]:
    """Skills directories to index, in priority order."""
    override = os.environ.get(_OVERRIDE_ENV, "")
    if override.strip():
        found = []
        # Only split on ';' — ':' breaks Windows absolute paths like C:\skills
        for part in override.split(";"):
            part = part.strip()
            if part:
                found.append(Path(part))
        return found
    home = Path.home()
    return [home / candidate for candidate in _CANDIDATE_ROOTS]


def parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Split a SKILL.md into its frontmatter mapping and its body.

    Hand-rolled rather than pulled from a YAML library: the project has
    no third-party dependencies, and skill frontmatter is a flat list of
    ``key: value`` lines. A file with no frontmatter is all body, which
    is a valid skill.
    """
    # Strip BOM if present (Windows files may start with \ufeff)
    if text.startswith("\ufeff"):
        text = text.lstrip("\ufeff")
    if not text.startswith("---"):
        return {}, text
    lines = text.split("\n")
    for index in range(1, len(lines)):
        if lines[index].strip() in ("---", "..."):
            meta: dict[str, str] = {}
            for line in lines[1:index]:
                if ":" not in line:
                    continue
                key, _, value = line.partition(":")
                key = key.strip().lower()
                if key:
                    # Only strip matching outer quotes
                    v = value.strip()
                    if len(v) >= 2 and v[0] == v[-1] and v[0] in ('"', "'"):
                        v = v[1:-1]
                    meta[key] = v
            return meta, "\n".join(lines[index + 1 :])
    # An unterminated block is not frontmatter, it is a horizontal rule.
    return {}, text


def _as_bool(value: str, default: bool = True) -> bool:
    return {"false": False, "0": False, "no": False}.get(value.strip().lower(), default)


def load_skill(directory: Path) -> Skill | None:
    """One skill from its directory. None when there is no SKILL.md."""
    path = directory / "SKILL.md"
    if not path.is_file():
        return None
    meta, body = parse_frontmatter(_read(path))
    name = meta.get("name") or directory.name
    return Skill(
        name=str(name).strip(),
        description=str(meta.get("description", "")),
        version=str(meta.get("version", "")),
        user_invocable=_as_bool(meta.get("user-invocable", "true")),
        path=path,
        root=directory.parent,
        _body=body,
    )


_CACHE_TTL = 1.5
_cache_all: dict[str, Skill] | None = None
_cache_all_ts: float = 0.0
_cache_all_roots: tuple[str, ...] | None = None


def load_all() -> dict[str, Skill]:
    """Every skill, keyed by lowercase name.

    Earlier roots win on a name collision, so a personal skills
    directory can shadow a project one. A directory without SKILL.md is
    skipped silently - skills/ legitimately holds INDEX.md and other
    documentation. Results are cached briefly to avoid scanning on every
    turn.
    """
    global _cache_all, _cache_all_ts, _cache_all_roots
    import time

    current_roots = tuple(str(r) for r in roots())
    now = time.monotonic()
    if _cache_all is not None and _cache_all_roots == current_roots and (now - _cache_all_ts) < _CACHE_TTL:
        return dict(_cache_all)
    found: dict[str, Skill] = {}
    for root in roots():
        if not root.is_dir():
            continue
        try:
            # Sort by name string to avoid pathlib _parts_normcase race on Windows 3.14
            entries = sorted((e for e in root.iterdir() if e.is_dir()), key=lambda p: p.name.lower())
        except OSError:  # pragma: no cover - unreadable directory
            continue
        for entry in entries:
            skill = load_skill(entry)
            if skill is None:
                continue
            key = skill.name.lower()
            if key not in found:
                found[key] = skill
    _cache_all = dict(found)
    _cache_all_ts = now
    _cache_all_roots = current_roots
    return found


def invalidate_cache() -> None:
    """Forget everything indexed so far.

    Skill directories are files on disk, but the index is held for a
    moment to avoid rescanning on every turn. After an edit, that moment
    is exactly when the operator wants to see the change, so the console
    exposes a way to drop it.
    """
    global _cache_all, _cache_all_ts, _cache_all_roots, _cache_bundles, _cache_bundles_ts, _cache_bundles_roots, _cache_routing, _cache_routing_ts, _cache_routing_roots
    _cache_all = None
    _cache_all_ts = 0.0
    _cache_all_roots = None
    _cache_bundles = None
    _cache_bundles_ts = 0.0
    _cache_bundles_roots = None
    _cache_routing = None
    _cache_routing_ts = 0.0
    _cache_routing_roots = None


def list_skills() -> list[Skill]:
    """Every skill, alphabetically."""
    return [found for _, found in sorted(load_all().items())]


def get(name: str) -> Skill | None:
    """One skill by name. Case-insensitive."""
    return load_all().get((name or "").strip().lower())


def _table_rows(text: str) -> list[list[str]]:
    """Body rows of a markdown table, cells stripped.

    Separator rows (``|---|---|``) are dropped because a table of forty
    skills otherwise yields a "skill" named ``---``.
    """
    rows = []
    for line in text.split("\n"):
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if not cells:
            continue
        if all(set(cell) <= set("-: ") and cell for cell in cells):
            continue
        rows.append(cells)
    return rows


def _backticked(cell: str) -> list[str]:
    return re.findall(r"`([^`]+)`", cell)


_cache_bundles: dict[str, list[str]] | None = None
_cache_bundles_ts: float = 0.0
_cache_bundles_roots: tuple[str, ...] | None = None


def load_bundles() -> dict[str, list[str]]:
    """Bundles from any BUNDLES.md in the roots, newest root last.

    A bundle is an *ordered* list of skill names; the order is the
    workflow. Returns {} when no root documents any.
    """
    global _cache_bundles, _cache_bundles_ts, _cache_bundles_roots
    import time

    current_roots = tuple(str(r) for r in roots())
    now = time.monotonic()
    if _cache_bundles is not None and _cache_bundles_roots == current_roots and (now - _cache_bundles_ts) < _CACHE_TTL:
        return dict(_cache_bundles)
    bundles: dict[str, list[str]] = {}
    for root in roots():
        if not root.is_dir():
            continue
        text = _read(root / "BUNDLES.md")
        if not text:
            continue
        for cells in _table_rows(text):
            if len(cells) < 2:
                continue
            name = cells[0].strip().strip("`").lower()
            skills = _backticked(cells[1])
            if name and skills:
                bundles[name] = skills
    _cache_bundles = dict(bundles)
    _cache_bundles_ts = now
    _cache_bundles_roots = current_roots
    return bundles


def get_bundle(name: str) -> list[str] | None:
    return load_bundles().get((name or "").strip().lower())


_cache_routing: dict[str, dict[str, str]] | None = None
_cache_routing_ts: float = 0.0
_cache_routing_roots: tuple[str, ...] | None = None


def routing_table() -> dict[str, dict[str, str]]:
    """The INDEX.md catalog: name -> {function, chained}.

    Used to answer "which skill do I want" without reading every file.
    Empty when no root publishes an index. Cached briefly.
    """
    global _cache_routing, _cache_routing_ts, _cache_routing_roots
    import time

    current_roots = tuple(str(r) for r in roots())
    now = time.monotonic()
    if _cache_routing is not None and _cache_routing_roots == current_roots and (now - _cache_routing_ts) < _CACHE_TTL:
        return dict(_cache_routing)
    table: dict[str, dict[str, str]] = {}
    for root in roots():
        if not root.is_dir():
            continue
        text = _read(root / "INDEX.md")
        if not text:
            continue
        for cells in _table_rows(text):
            if len(cells) < 2:
                continue
            names = _backticked(cells[0])
            if not names:
                continue
            entry = {"function": cells[1]}
            if len(cells) > 2:
                entry["chained"] = ", ".join(_backticked(cells[2]))
            table.setdefault(names[0].lower(), entry)
    _cache_routing = dict(table)
    _cache_routing_ts = now
    _cache_routing_roots = current_roots
    return table


_STOPWORDS = frozenset(
    "a an the to for of and or with without in on at by from into "
    "is are be been it its this that these those i you we they my our "
    "do does did make makes making use using want need please".split()
)


def _tokens(text: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9]+", (text or "").lower())
        if token not in _STOPWORDS and len(token) > 1
    }


def _stem(token: str) -> str:
    """Fold common inflections onto one root.

    Not a real stemmer - just enough that "tests", "testing" and "tested"
    all land on "test", which is all a router needs.
    """
    if len(token) > 4:
        for suffix in ("ing", "ers", "ies", "es", "ed", "s"):
            if token.endswith(suffix):
                return token[: -len(suffix)]
    return token


def _stems(text: str) -> set[str]:
    return {_stem(token) for token in _tokens(text)}


# Below this length a shared prefix is most of some other word's
# accidental beginning ("use"/"user"), so only exact matches count.
_PREFIX_FLOOR = 4


def _hits(wanted: set[str], haystack: set[str]) -> list[str]:
    """Which of ``wanted`` occur in ``haystack``.

    A stem may also match by prefix, so "fail" meets both "failure" and
    "failing". The operator writes the gerund and the index writes the
    noun and they are the same request; without this the router scores
    them as unrelated and finds nothing.
    """
    found: list[str] = []
    for token in wanted:
        for candidate in haystack:
            if token == candidate:
                found.append(token)
                break
            if min(len(token), len(candidate)) >= _PREFIX_FLOOR and (
                candidate.startswith(token) or token.startswith(candidate)
            ):
                found.append(token)
                break
    return found


def _search_text(skill: Skill, index: dict[str, dict[str, str]]) -> str:
    """What a query is matched against: the name, the one-liner, the index."""
    return skill.description + " " + index.get(skill.name.lower(), {}).get("function", "")


def _idf(known: list[Skill], index: dict[str, dict[str, str]]) -> dict[str, float]:
    """Rarity weight per stem, inversely proportional to how many skills use it.

    "test" appears in half the catalog and says almost nothing about which
    skill was meant; "reproduce" appears in two and says nearly everything.
    Scoring every matched word the same let whichever skill had the longest
    description win on incidental overlap, which put "analytics" above
    "debug" for the request "reproduce a failing test".
    """
    counts: dict[str, int] = {}
    for skill in known:
        for token in _stems(_search_text(skill, index)) | _stems(skill.name):
            counts[token] = counts.get(token, 0) + 1
    return {token: 1.0 / count for token, count in counts.items()}


def _query_weight(wanted: set[str], weights: dict[str, float]) -> float:
    """Total weight of a query, so a score can be read as a fraction of it."""
    return sum(weights.get(token, 1.0) for token in wanted) or 1.0


def route(query: str, limit: int = 5) -> list[tuple[Skill, float]]:
    """Match a request to skills. Returns (skill, score), best first.

    Scored on the name and the one-line function from the index rather
    than the whole procedure: the point is to pick a skill, not to read
    it. A word in the name counts double, because asking for "tdd"
    should find tdd and not every skill that mentions tests.
    """
    wanted = _stems(query)
    if not wanted:
        return []
    index = routing_table()
    # A skill marked not user-invocable is one the operator did not ask
    # to be offered, so it is never attached on the router's own guess.
    # It stays fully reachable by name.
    known = [s for s in list_skills() if s.user_invocable]
    if not known:
        return []
    weights = _idf(known, index)
    scored: list[tuple[Skill, float]] = []
    for skill in known:
        names = _stems(skill.name)
        haystack = names | _stems(_search_text(skill, index))
        hits = _hits(wanted, haystack)
        if not hits:
            continue
        score = sum(weights.get(token, 1.0) for token in hits)
        score += 2 * sum(weights.get(token, 1.0) for token in _hits(wanted, names))
        scored.append((skill, score))
    scored.sort(key=lambda pair: (-pair[1], pair[0].name))
    return scored[:limit]


# A skill is offered unasked only when it answers at least this much of
# what was said, and is this far ahead of the runner-up. Below either,
# the request is too vague or too contested to guess, and attaching the
# wrong procedure quietly is worse than attaching nothing.
_CONFIDENCE = 0.34
_MARGIN = 1.25


def match_bundle(query: str) -> str | None:
    """The bundle whose name the request speaks, if any.

    Name only, deliberately. Matching on the members was tried first and
    is hopeless: a bundle holds five or six skills which between them
    mention work, read, write, test and create, so almost every request
    scored a full match against something - asking for one small file to
    be created offered the entire ship-feature sequence. Weighting by
    rarity did not rescue it either, because two common words ("write a
    test") cover 100% of a short request while four rare ones cover only
    40% of a genuinely matching one.

    The name is the only part the operator actually says, so it is the
    only part worth trusting.
    """
    wanted = _stems(query)
    if not wanted:
        return None
    best_name: str | None = None
    best_score = 0
    for bundle in load_bundles():
        bundle_stems = _stems(bundle.replace("-", " "))
        name_hits = _hits(wanted, bundle_stems)
        # Every word of a compound name has to be spoken: "fix this" is
        # not a request for fix-bug, and neither is "the bug".
        needed = len(bundle_stems) if len(bundle_stems) <= 2 else 2
        if len(name_hits) < needed:
            continue
        if len(name_hits) > best_score:
            best_name, best_score = bundle, len(name_hits)
    return best_name


_GENERIC_NO_AUTO = frozenset({"explain", "project", "workspace", "codebase", "repo", "overview", "summarise", "summarize", "what", "does", "about", "code", "this"})


def recommend(query: str) -> tuple[Skill | None, str | None]:
    """What to attach for ``query`` without being asked: (skill, bundle).

    Either may be None. Both are computed from the same request, because
    they answer different questions: the skill is what to follow now,
    the bundle is the longer sequence this request might be step one of.
    """
    # Generic workspace queries like "Explain this project" must not auto-attach help
    wanted = _stems(query)
    if wanted and wanted.issubset(_GENERIC_NO_AUTO | {"thi", "what"}):
        return None, match_bundle(query)
    # Don't hijack "explain this project" / "what this project does" with help skill
    if wanted and "project" in wanted and len(wanted) <= 3:
        return None, match_bundle(query)
    ranked = route(query, limit=6)
    best: Skill | None = None
    if ranked:
        top, top_score = ranked[0]
        # Never auto-attach generic help for vague queries
        if top.name.lower() == "help" and len(wanted) <= 2:
            return None, match_bundle(query)
        runner = ranked[1][1] if len(ranked) > 1 else 0.0
        weights = _idf(list_skills(), routing_table())
        total = _query_weight(wanted, weights)
        if top_score / total >= _CONFIDENCE and top_score >= runner * _MARGIN:
            best = top
    return best, match_bundle(query)


def find(query: str, limit: int = 5) -> list[Skill]:
    """Skills matching free text: routed first, then any name containing it."""
    hits = [skill for skill, _ in route(query, limit=limit)]
    if hits:
        return hits
    needle = (query or "").strip().lower()
    if not needle:
        return []
    return [
        skill
        for skill in list_skills()
        if needle in skill.name.lower() or needle in skill.description.lower()
    ][:limit]
