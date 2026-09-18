"""Tests for /skills - discovering and running skill bundles.

Every test points ``MANTRA_SKILLS`` at a fixture directory, so the
suite passes whether or not the operator has a skills tree installed.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

# Bootstrap imports so the suite runs from a checkout without installation.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "."))

import core.agent.skills as skills
from core.console import ConsoleSession, Style, _skills

FIXTURE_SKILL = """---
name: {name}
description: {description}
version: 1.0.0
user-invocable: true
---

# {title}

## Procedure

1. Do the thing.
2. Verify the thing.
"""

INDEX = """# Skill Catalog

## Verify And Review

| Skill | Function | Chained with |
|---|---|---|
| `tdd` | Drive behavior-first test, implementation, and refactoring. | `debug`. |
| `debug` | Reproduce and fix a failure from its root cause. | `tdd`. |
"""

BUNDLES = """# Skill Bundles

| Bundle | Skills in order | Use |
|---|---|---|
| fix-bug | `debug`, `tdd`, `verify` | Reproduce, fix, verify. |
"""


def _write(path: str, text: str) -> None:
    full = os.path.join(path)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


class ParseTest(unittest.TestCase):
    def test_frontmatter_is_split_from_the_body(self):
        meta, body = skills.parse_frontmatter(
            "---\nname: x\ndescription: does x\n---\n\nthe body\n"
        )
        self.assertEqual(meta["name"], "x")
        self.assertIn("the body", body)
        self.assertNotIn("name:", body)

    def test_a_file_with_no_frontmatter_is_all_body(self):
        meta, body = skills.parse_frontmatter("# Just a heading\n\ntext")
        self.assertEqual(meta, {})
        self.assertIn("Just a heading", body)

    def test_an_unterminated_block_is_not_frontmatter(self):
        # A leading --- that never closes is a horizontal rule, and
        # treating it as frontmatter would swallow the whole file.
        meta, body = skills.parse_frontmatter("---\nname: x\n\nstill body")
        self.assertEqual(meta, {})
        self.assertIn("still body", body)

    def test_values_are_unquoted(self):
        meta, _ = skills.parse_frontmatter('---\nname: "quoted"\n---\nbody')
        self.assertEqual(meta["name"], "quoted")

    def test_keys_are_lowercased(self):
        meta, _ = skills.parse_frontmatter("---\nUser-Invocable: false\n---\nb")
        self.assertIn("user-invocable", meta)

    def test_booleans_are_read(self):
        self.assertFalse(skills._as_bool("false"))
        self.assertFalse(skills._as_bool("No"))
        self.assertTrue(skills._as_bool("true"))
        # Unrecognized strings count as true (fail-open fallback).
        self.assertTrue(skills._as_bool("something else"))

    def test_a_table_drops_its_separator_row(self):
        rows = skills._table_rows("| a | b |\n|---|---|\n| 1 | 2 |")
        self.assertEqual(rows, [["a", "b"], ["1", "2"]])

    def test_backticked_names_are_extracted(self):
        self.assertEqual(skills._backticked("`one`, `two`"), ["one", "two"])


class SkillsFixtureTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.addCleanup(os.environ.pop, skills._OVERRIDE_ENV, None)
        self.root = os.path.join(self.tmp, "skills")
        os.environ[skills._OVERRIDE_ENV] = self.root

        for name, description in (
            ("tdd", "Drive behavior-first test, implementation, and refactoring."),
            ("debug", "Reproduce and fix a failure from its root cause."),
            ("verify", "Confirm the work is actually done."),
        ):
            _write(
                os.path.join(self.root, name, "SKILL.md"),
                FIXTURE_SKILL.format(name=name, description=description, title=name.upper()),
            )
        # A skill that bundles its own script, which is what makes a
        # skill more than a prompt.
        _write(os.path.join(self.root, "debug", "scripts", "trace.ps1"), "echo hi")
        # Directories that are not skills must be ignored silently.
        _write(os.path.join(self.root, "INDEX.md"), INDEX)
        _write(os.path.join(self.root, "BUNDLES.md"), BUNDLES)
        os.makedirs(os.path.join(self.root, "not-a-skill"), exist_ok=True)

    def test_every_skill_is_found(self):
        self.assertEqual(
            sorted(s.name for s in skills.list_skills()), ["debug", "tdd", "verify"]
        )

    def test_non_skill_directories_are_ignored(self):
        self.assertIsNone(skills.get("not-a-skill"))
        self.assertIsNone(skills.get("INDEX"))

    def test_frontmatter_is_read(self):
        found = skills.get("tdd")
        self.assertEqual(found.version, "1.0.0")
        self.assertTrue(found.user_invocable)
        self.assertIn("Drive behavior-first", found.description)

    def test_lookup_is_case_insensitive(self):
        self.assertIsNotNone(skills.get("TDD"))

    def test_the_body_is_the_procedure(self):
        body = skills.get("tdd").body
        self.assertIn("## Procedure", body)
        self.assertNotIn("name: tdd", body)

    def test_bundled_resources_are_listed(self):
        self.assertEqual(skills.get("debug").resources, ["scripts/trace.ps1"])

    def test_a_skill_with_no_resources_has_none(self):
        self.assertEqual(skills.get("tdd").resources, [])

    def test_bodies_are_read_lazily(self):
        # Indexing should not hold every file, so an un-read skill has
        # no body cached until something asks.
        found = skills.get("verify")
        found._body = None
        self.assertIsNone(found._body)
        self.assertIn("Procedure", found.body)

    def test_the_routing_table_is_parsed(self):
        table = skills.routing_table()
        self.assertEqual(table["tdd"]["function"],
                         "Drive behavior-first test, implementation, and refactoring.")
        self.assertEqual(table["debug"]["chained"], "tdd")

    def test_bundles_are_parsed_in_order(self):
        self.assertEqual(skills.get_bundle("fix-bug"), ["debug", "tdd", "verify"])

    def test_an_unknown_bundle_is_none(self):
        self.assertIsNone(skills.get_bundle("nope"))

    def test_an_earlier_root_shadows_a_later_one(self):
        other = os.path.join(self.tmp, "other")
        _write(os.path.join(other, "tdd", "SKILL.md"),
               FIXTURE_SKILL.format(name="tdd", description="the override", title="T"))
        os.environ[skills._OVERRIDE_ENV] = self.root + ";" + other
        self.assertIn("Drive behavior-first", skills.get("tdd").description)

    def test_a_missing_root_is_skipped(self):
        os.environ[skills._OVERRIDE_ENV] = os.path.join(self.tmp, "nope") + ";" + self.root
        self.assertEqual(len(skills.list_skills()), 3)


class RoutingTest(SkillsFixtureTest):
    def test_a_name_match_wins(self):
        self.assertEqual(skills.route("tdd")[0][0].name, "tdd")

    def test_a_descriptive_query_finds_the_skill(self):
        self.assertEqual(skills.route("reproduce a failure")[0][0].name, "debug")

    def test_results_are_ordered_best_first(self):
        scores = [score for _, score in skills.route("fix the failure")]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_an_empty_query_returns_nothing(self):
        self.assertEqual(skills.route(""), [])

    def test_a_query_of_only_stopwords_returns_nothing(self):
        self.assertEqual(skills.route("the a of"), [])

    def test_find_falls_back_to_substring_matching(self):
        hits = skills.find("verif")
        self.assertEqual([h.name for h in hits][0], "verify")

    def test_find_returns_nothing_for_no_match(self):
        self.assertEqual(skills.find("zzzzz"), [])


class CommandTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        for var in ("MANTRA_SETTINGS", skills._OVERRIDE_ENV):
            self.addCleanup(os.environ.pop, var, None)
        os.environ["MANTRA_SETTINGS"] = os.path.join(self.tmp, "config.json")
        self.root = os.path.join(self.tmp, "skills")
        os.environ[skills._OVERRIDE_ENV] = self.root

        for name, description in (
            ("tdd", "Drive behavior-first test, implementation, and refactoring."),
            ("debug", "Reproduce and fix a failure from its root cause."),
            ("verify", "Confirm the work is actually done."),
        ):
            _write(
                os.path.join(self.root, name, "SKILL.md"),
                FIXTURE_SKILL.format(name=name, description=description, title=name.upper()),
            )
        _write(os.path.join(self.root, "INDEX.md"), INDEX)
        _write(os.path.join(self.root, "BUNDLES.md"), BUNDLES)

        from core.config import merge_defaults
        from core.scripted import ScriptedLLMClient

        self.session = ConsoleSession(
            config=merge_defaults({}),
            workspace=self.tmp,
            style=Style(enabled=False),
            llm=ScriptedLLMClient([]),
            ask=lambda prompt: "y",  # every approval auto-answered yes
        )
        self.printed: list[str] = []
        self.session._print = self.printed.append

    def _shown(self) -> str:
        return "\n".join(str(line) for line in self.printed)

    def test_listing_shows_every_skill(self):
        _skills(self.session, "")
        shown = self._shown()
        for name in ("tdd", "debug", "verify"):
            self.assertIn(name, shown)

    def test_listing_uses_the_index_function_when_there_is_one(self):
        _skills(self.session, "")
        self.assertIn("behavior-first", self._shown())

    def test_a_bare_name_shows_the_skill(self):
        # Bare name now attaches in one step (easier manual), show still works via "show"
        _skills(self.session, "tdd")
        self.assertIn("attached 'tdd'", self._shown())
        self.assertIn("tdd", self.session.active_skills)

    def test_a_name_with_a_file_reference_runs_once_then_detaches(self):
        # "/skills code-review @flappy.py" must run the skill ONCE on the
        # reference and detach - not attach it for every turn.
        with mock.patch.object(self.session, "handle", return_value=None):
            _skills(self.session, "tdd @flappy.py")
        shown = self._shown()
        self.assertIn("running 'tdd' once on: @flappy.py", shown)
        self.assertIn("ran once and is detached", shown)
        self.assertNotIn("attached 'tdd'", shown)
        self.assertNotIn("skills for:", shown)
        self.assertEqual(self.session.active_skills, [])
        self.assertEqual(self.session.auto_attached, [])

    def test_use_with_a_file_reference_runs_once(self):
        with mock.patch.object(self.session, "handle", return_value=None):
            _skills(self.session, "use tdd @flappy.py")
        shown = self._shown()
        self.assertIn("running 'tdd' once on: @flappy.py", shown)
        self.assertNotIn("attached 'tdd'", shown)
        self.assertEqual(self.session.active_skills, [])

    def test_show_prints_the_procedure(self):
        _skills(self.session, "show tdd")
        self.assertIn("## Procedure", self._shown())

    def test_show_an_unknown_skill_suggests_alternatives(self):
        _skills(self.session, "show tddx")
        self.assertIn("no skill named", self._shown())

    def test_attaching_a_skill_puts_it_in_the_prompt(self):
        _skills(self.session, "use tdd")
        self.assertIn("## Skill in force: tdd", self.session._effective_system_prompt())
        self.assertIn("## Procedure", self.session._effective_system_prompt())

    def test_attaching_twice_does_not_duplicate_it(self):
        _skills(self.session, "use tdd")
        _skills(self.session, "use tdd")
        self.assertEqual(self.session.active_skills, ["tdd"])

    def test_an_attached_skill_is_marked_in_the_listing(self):
        _skills(self.session, "use tdd")
        self.printed.clear()
        _skills(self.session, "")
        self.assertIn("* tdd", self._shown())

    def test_clearing_detaches(self):
        _skills(self.session, "use tdd")
        _skills(self.session, "clear")
        self.assertEqual(self.session.active_skills, [])
        self.assertNotIn("Skill in force", self.session._effective_system_prompt())

    def test_clearing_with_nothing_attached_says_so(self):
        _skills(self.session, "clear")
        self.assertIn("no skills attached", self._shown())

    def test_attaching_an_unknown_skill_is_refused(self):
        _skills(self.session, "use nope")
        self.assertEqual(self.session.active_skills, [])

    def test_use_without_a_name_is_a_usage_message(self):
        _skills(self.session, "use")
        self.assertIn("usage", self._shown())

    def test_bundles_are_listed_in_order(self):
        _skills(self.session, "bundles")
        self.assertIn("debug > tdd > verify", self._shown())

    def test_find_routes_a_request(self):
        _skills(self.session, "find reproduce a failure")
        self.assertIn("debug", self._shown())

    def test_find_without_a_query_is_a_usage_message(self):
        _skills(self.session, "find")
        self.assertIn("usage", self._shown())

    def test_launch_runs_the_bundle_in_order(self):
        seen: list[str] = []
        with mock.patch.object(self.session, "handle",
                               side_effect=lambda text: seen.append(text) or object()):
            _skills(self.session, "launch fix-bug")
        self.assertEqual(len(seen), 3)
        self.assertIn("debug", seen[0])
        self.assertIn("tdd", seen[1])
        self.assertIn("verify", seen[2])

    def test_launch_attaches_each_skill_for_its_step(self):
        # Read the attachment from inside the step itself: the prompt is only
        # consulted by the real agent loop, and handle() is stubbed out here,
        # so watching _effective_system_prompt would observe nothing at all.
        attached: list[str] = []
        with mock.patch.object(
            self.session, "handle",
            side_effect=lambda text: attached.extend(self.session.active_skills) or object(),
        ):
            _skills(self.session, "launch fix-bug")
        self.assertEqual(attached, ["debug", "tdd", "verify"])

    def test_launch_restores_what_was_attached_before(self):
        _skills(self.session, "use verify")
        with mock.patch.object(self.session, "handle", return_value=object()):
            _skills(self.session, "launch fix-bug")
        self.assertEqual(self.session.active_skills, ["verify"])

    def test_launch_restores_even_when_stopped(self):
        _skills(self.session, "use verify")
        with mock.patch.object(self.session, "handle", return_value=None):
            _skills(self.session, "launch fix-bug")
        self.assertEqual(self.session.active_skills, ["verify"])

    def test_launch_stops_when_a_step_fails(self):
        with mock.patch.object(self.session, "handle", return_value=None) as handled:
            _skills(self.session, "launch fix-bug")
        self.assertEqual(handled.call_count, 1)
        self.assertIn("stopped", self._shown())

    def test_ctrl_c_stops_the_bundle(self):
        with mock.patch.object(self.session, "handle", side_effect=KeyboardInterrupt):
            _skills(self.session, "launch fix-bug")
        self.assertIn("bundle stopped", self._shown())

    def test_launch_an_unknown_bundle_says_so(self):
        _skills(self.session, "launch nope")
        self.assertIn("no bundle named", self._shown())

    def test_launch_refuses_a_bundle_naming_a_missing_skill(self):
        _write(os.path.join(self.root, "BUNDLES.md"),
               "| broken | `debug`, `ghost` | nope. |")
        _skills(self.session, "launch broken")
        self.assertIn("not installed", self._shown())

    def test_no_roots_configured_says_where_it_looked(self):
        os.environ[skills._OVERRIDE_ENV] = os.path.join(self.tmp, "empty")
        _skills(self.session, "")
        self.assertIn("no skills found", self._shown())
        self.assertIn(skills._OVERRIDE_ENV, self._shown())

    def test_skills_is_in_the_command_table(self):
        from core.console import SLASH_COMMANDS

        self.assertIn("/skills", [c for c, _ in SLASH_COMMANDS])

    def test_skills_is_in_the_help_text(self):
        from core.console import HELP_TEXT

        self.assertIn("/skills", HELP_TEXT)

    def test_dispatch_routes_to_the_skills_handler(self):
        from core.console import dispatch

        with mock.patch("core.console._skills") as handled:
            dispatch(self.session, "/skills show tdd")
        handled.assert_called_once_with(self.session, "show tdd")


class StemTest(unittest.TestCase):
    """The router has to survive the operator's grammar."""

    def test_plurals_and_gerunds_fold_together(self):
        self.assertEqual(skills._stem("tests"), "test")
        self.assertEqual(skills._stem("testing"), "test")
        self.assertEqual(skills._stem("tested"), "test")

    def test_short_words_are_left_alone(self):
        # "use" must not lose its e, or it stops being a word.
        self.assertEqual(skills._stem("use"), "use")

    def test_a_stem_matches_by_prefix(self):
        # The operator writes the gerund, the index writes the noun.
        self.assertEqual(skills._hits({"fail"}, {"failure"}), ["fail"])
        self.assertEqual(skills._hits({"failing"}, {"fail"}), ["failing"])

    def test_prefix_matching_needs_a_real_prefix(self):
        # Below four characters a shared start is coincidence, not a match.
        self.assertEqual(skills._hits({"use"}, {"user"}), [])

    def test_exact_matches_still_count_when_short(self):
        self.assertEqual(skills._hits({"api"}, {"api"}), ["api"])


class RecommendTest(SkillsFixtureTest):
    """Routing has to know when to keep its hands off."""

    def test_a_plain_request_finds_its_skill(self):
        found, _ = skills.recommend("reproduce a failure")
        self.assertIsNotNone(found)
        self.assertEqual(found.name, "debug")

    def test_a_named_skill_wins_outright(self):
        found, _ = skills.recommend("tdd")
        self.assertIsNotNone(found)
        self.assertEqual(found.name, "tdd")

    def test_an_unrelated_prompt_gets_nothing(self):
        self.assertEqual(skills.recommend("what time is it"), (None, None))

    def test_noise_alone_is_not_an_attachment(self):
        self.assertIsNone(skills.recommend("zzzz")[0])

    def test_a_bundle_is_matched_by_its_own_name(self):
        self.assertEqual(skills.match_bundle("fix the bug"), "fix-bug")

    def test_a_bundle_is_not_matched_by_its_members_alone(self):
        # Every one of fix-bug's skills answers this, and it still must not
        # fire: a bundle is several turns, and member overlap is broad
        # enough to match almost anything. Only the name counts.
        self.assertIsNone(
            skills.match_bundle("reproduce a failure, drive it with a test, confirm it")
        )

    def test_a_compound_name_needs_every_word_spoken(self):
        # Half of fix-bug is not a request for fix-bug.
        self.assertIsNone(skills.match_bundle("fix this"))
        self.assertIsNone(skills.match_bundle("the bug in the parser"))

    def test_an_unrelated_prompt_matches_no_bundle(self):
        self.assertIsNone(skills.match_bundle("what time is it"))

    def test_a_common_word_alone_does_not_pick_a_bundle(self):
        self.assertIsNone(skills.match_bundle("write a test"))

    def test_a_trivial_request_does_not_pick_a_bundle(self):
        # Regression: this matched six-member ship-feature when bundles
        # were scored on their members.
        self.assertIsNone(skills.match_bundle("create hello.txt and read it back"))

    def test_rare_words_outrank_common_ones(self):
        # Weighting by rarity is what stops the longest description winning
        # on incidental overlap.
        hit = skills.route("reproduce a failure")[0]
        self.assertEqual(hit[0].name, "debug")


class AutoRouteTest(SkillsFixtureTest):
    """Skills attaching themselves to a plain prompt."""

    def setUp(self):
        super().setUp()
        for var in ("MANTRA_SETTINGS",):
            self.addCleanup(os.environ.pop, var, None)
        self.settings_file = os.path.join(self.tmp, "config.json")
        os.environ["MANTRA_SETTINGS"] = self.settings_file
        from core.config import merge_defaults
        from core.scripted import ScriptedLLMClient

        self.session = ConsoleSession(
            config=merge_defaults({}),
            workspace=self.tmp,
            style=Style(enabled=False),
            llm=ScriptedLLMClient([]),
            ask=lambda prompt: "y",  # every approval auto-answered yes
        )
        self.printed: list[str] = []
        self.session._print = self.printed.append
        self.session._note = self.printed.append

    def _shown(self) -> str:
        return "\n".join(str(line) for line in self.printed)

    def test_a_matching_prompt_attaches_the_skill(self):
        self.session.auto_route("reproduce a failure")
        self.assertEqual(self.session.active_skills, ["debug"])

    def test_the_skill_is_remembered_as_auto_attached(self):
        self.session.auto_route("reproduce a failure")
        self.assertEqual(self.session.auto_attached, ["debug"])

    def test_the_operator_is_told_what_was_attached(self):
        self.session.auto_route("reproduce a failure")
        self.assertIn("auto-attached", self._shown())
        self.assertIn("debug", self._shown())

    def test_an_unrelated_prompt_attaches_nothing(self):
        self.session.auto_route("what time is it")
        self.assertEqual(self.session.active_skills, [])
        self.assertEqual(self._shown(), "")

    def test_a_deliberate_choice_is_not_overridden(self):
        self.session.active_skills = ["tdd"]
        self.session.auto_route("reproduce a failure")
        self.assertEqual(self.session.active_skills, ["tdd"])

    def test_a_bundle_step_is_left_alone(self):
        # The step already knows which skill it wants.
        self.session.in_bundle = True
        self.session.auto_route("reproduce a failure")
        self.assertEqual(self.session.active_skills, [])

    def test_a_matching_bundle_is_offered_not_launched(self):
        self.session.config["skills"]["auto_bundle"] = False
        self.assertIsNone(self.session.auto_route("fix the bug"))
        self.assertIn("/skills launch fix-bug", self._shown())

    def test_a_bundle_is_launched_when_that_is_asked_for(self):
        self.session.config["skills"]["auto_bundle"] = True
        self.assertEqual(self.session.auto_route("fix the bug"), "fix-bug")

    def test_launching_a_bundle_drops_the_skill_it_first_picked(self):
        # The bundle attaches its own skill per step; a leftover would
        # ride along through all of them.
        self.session.config["skills"]["auto_bundle"] = True
        self.session.auto_route("fix the bug")
        self.assertEqual(self.session.active_skills, [])
        self.assertEqual(self.session.auto_attached, [])

    def test_routing_off_attaches_nothing(self):
        self.session.config["skills"]["auto"] = False
        self.session.auto_route("reproduce a failure")
        self.assertEqual(self.session.active_skills, [])

    def test_a_stored_preference_beats_the_config_file(self):
        from core.agent import settings as settings_module

        settings_module.set_skills_prefs(auto=False)
        self.session.config["skills"]["auto"] = True
        self.session.auto_route("reproduce a failure")
        self.assertEqual(self.session.active_skills, [])

    def test_no_skills_at_all_costs_nothing(self):
        os.environ[skills._OVERRIDE_ENV] = os.path.join(self.tmp, "empty")
        self.assertEqual(self.session.auto_route("reproduce a failure"), None)
        self.assertEqual(self.session.active_skills, [])

    def test_detaching_leaves_a_hand_picked_skill_alone(self):
        self.session.active_skills = ["tdd"]
        self.session.auto_attached = ["debug"]
        self.session.active_skills.append("debug")
        self.session._detach_auto()
        self.assertEqual(self.session.active_skills, ["tdd"])

    def test_detaching_twice_is_harmless(self):
        self.session.active_skills = ["tdd"]
        self.session.auto_attached = []
        self.session._detach_auto()
        self.assertEqual(self.session.active_skills, ["tdd"])

    def test_a_turn_detaches_what_it_attached(self):
        # Routing reads one request; leaving its guess attached would
        # saddle every later turn with a procedure nobody asked for.
        with mock.patch.object(self.session, "_install_sigint"):
            with mock.patch("core.console.AgentLoop") as loop:
                loop.return_value.run.return_value = None
                self.session.handle("reproduce a failure")
        self.assertEqual(self.session.active_skills, [])
        self.assertEqual(self.session.auto_attached, [])


class AutoCommandTest(SkillsFixtureTest):
    """/skills auto - turning the behaviour on and off."""

    def setUp(self):
        super().setUp()
        self.addCleanup(os.environ.pop, "MANTRA_SETTINGS", None)
        os.environ["MANTRA_SETTINGS"] = os.path.join(self.tmp, "config.json")
        from core.config import merge_defaults
        from core.scripted import ScriptedLLMClient

        self.session = ConsoleSession(
            config=merge_defaults({}),
            workspace=self.tmp,
            style=Style(enabled=False),
            llm=ScriptedLLMClient([]),
            ask=lambda prompt: "y",  # every approval auto-answered yes
        )
        self.printed: list[str] = []
        self.session._print = self.printed.append

    def _shown(self) -> str:
        return "\n".join(str(line) for line in self.printed)

    def test_the_status_is_shown_bare(self):
        _skills(self.session, "auto")
        self.assertIn("auto", self._shown())
        self.assertIn("on", self._shown())

    def test_turning_it_off_is_recorded(self):
        from core.agent import settings as settings_module

        _skills(self.session, "auto off")
        self.assertFalse(settings_module.skills_prefs()["auto"])

    def test_turning_it_back_on_is_recorded(self):
        from core.agent import settings as settings_module

        _skills(self.session, "auto off")
        _skills(self.session, "auto on")
        self.assertTrue(settings_module.skills_prefs()["auto"])

    def test_bundles_are_off_until_asked_for(self):
        from core.agent import settings as settings_module

        _skills(self.session, "auto bundle on")
        self.assertTrue(settings_module.skills_prefs()["auto_bundle"])

    def test_turning_bundles_off_again(self):
        from core.agent import settings as settings_module

        _skills(self.session, "auto bundle on")
        _skills(self.session, "auto bundle off")
        self.assertFalse(settings_module.skills_prefs()["auto_bundle"])

    def test_an_unknown_argument_is_a_usage_message(self):
        _skills(self.session, "auto maybe")
        self.assertIn("usage", self._shown())

    def test_bundle_needs_a_direction(self):
        _skills(self.session, "auto bundle")
        self.assertIn("usage", self._shown())

    def test_switching_off_drops_an_auto_attachment(self):
        self.session.active_skills = ["debug"]
        self.session.auto_attached = ["debug"]
        _skills(self.session, "auto off")
        self.assertEqual(self.session.active_skills, [])


class RootDiscoveryTest(unittest.TestCase):
    """Which skills directories are indexed, and in what order.

    roots() must ship the repo library first (so the bundled copies are
    what the console uses), then the personal ~/.mantra/skills tree as a
    supplement, and an explicit MANTRA_SKILLS override must still win
    outright (tests rely on it).
    """

    def setUp(self):
        self.addCleanup(os.environ.pop, skills._OVERRIDE_ENV, None)

    @mock.patch.object(skills.Path, "home")
    def test_repo_library_is_indexed_first(self, home_mock):
        os.environ.pop(skills._OVERRIDE_ENV, None)
        home = os.path.join(tempfile.mkdtemp(), "home")
        home_mock.return_value = skills.Path(home)
        roots = skills.roots()
        self.assertGreaterEqual(len(roots), 2)
        # Repository-shipped library is first.
        repo = skills.Path(__file__).resolve().parents[1] / "skills"
        self.assertEqual(str(roots[0]), str(repo))
        # The personal tree comes after it.
        self.assertEqual(str(roots[1]), os.path.join(home, ".mantra", "skills"))

    def test_override_wins_outright(self):
        custom = os.path.join(tempfile.mkdtemp(), "custom")
        os.environ[skills._OVERRIDE_ENV] = custom
        self.assertEqual([str(r) for r in skills.roots()], [custom])


if __name__ == "__main__":
    unittest.main()
