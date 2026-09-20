"""Tests for bring-your-own-key plumbing, the settings file, and reasoning.

Every test redirects the storage paths to a temporary directory and
replaces ``urlopen`` with a recorder, so nothing here should ever read
or write the real ``~/.mantra`` directory or touch the network.

The endpoints a user adds live in one hand-editable file,
``~/.mantra/config.json``; there are no built-in providers any more, so
that file is the whole list. Several tests here write it *by hand* on
purpose - if a test can only add an endpoint through ``/connect``, the
promise that the file is editable breaks without anyone noticing.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from unittest import mock

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_TESTS_DIR)
# Bootstrap imports so the suite runs from a checkout without installation.
for _path in (os.path.join(_PROJECT_ROOT, "."), _PROJECT_ROOT, _TESTS_DIR):
    if _path not in sys.path:
        sys.path.insert(0, _path)
import core.console as console  # noqa: E402
import core.llm as openai_module  # noqa: E402
from core.config import REASONING_EFFORTS, merge_defaults  # noqa: E402
from core.console import provider_needs_key  # noqa: E402
from core.agent.models import fetch_models, is_reasoning_model  # noqa: E402
from _helpers import make_session  # noqa: E402
from core.agent.keys import (  # noqa: E402
    credentials_path,
    has_stored,
    mask,
    remove as remove_key,
    resolve,
    store as store_key,
    stored_keys,
)
from core.agent.settings import (  # noqa: E402
    active,
    add_endpoint,
    endpoint_name_for_url,
    endpoints,
    get_endpoint,
    models_for,
    remove_endpoint,
    set_active,
    set_models,
    settings_path,
    validate_endpoint,
)
from core.registry import build_llm  # noqa: E402


class TempStorage:
    """Point both storage files at a scratch directory for the test.

    Credentials and settings are separate files on purpose: keys are
    never written into the file a user is expected to open in an editor.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(
            os.environ,
            {
                "MANTRA_CREDENTIALS": os.path.join(self.tmp.name, "credentials.json"),
                "MANTRA_SETTINGS": os.path.join(self.tmp.name, "config.json"),
            },
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(self.tmp.cleanup)

    def tearDown(self):
        self.tmp.cleanup()


class KeyStoreTest(TempStorage, unittest.TestCase):
    def test_paths_respect_the_override(self):
        self.assertIn(self.tmp.name, str(credentials_path()))
        self.assertIn(self.tmp.name, str(settings_path()))

    def test_round_trip(self):
        self.assertIsNone(resolve("SOME_KEY"))
        store_key("SOME_KEY", "sk-abcdefgh1234")
        self.assertEqual(resolve("SOME_KEY"), "sk-abcdefgh1234")
        self.assertTrue(has_stored("SOME_KEY"))
        self.assertTrue(remove_key("SOME_KEY"))
        self.assertIsNone(resolve("SOME_KEY"))

    def test_removing_an_absent_key_reports_false(self):
        self.assertFalse(remove_key("NEVER_STORED"))

    def test_environment_beats_the_stored_value(self):
        store_key("SOME_KEY", "stored-value")
        with mock.patch.dict(os.environ, {"SOME_KEY": "from-env"}):
            self.assertEqual(resolve("SOME_KEY"), "from-env")

    def test_blank_environment_falls_back_to_stored(self):
        store_key("SOME_KEY", "stored-value")
        with mock.patch.dict(os.environ, {"SOME_KEY": "   "}):
            self.assertEqual(resolve("SOME_KEY"), "stored-value")

    def test_unnamed_key_is_refused(self):
        with self.assertRaises(ValueError):
            store_key("   ", "value")

    def test_multiple_keys_coexist(self):
        store_key("KEY_A", "aaa")
        store_key("KEY_B", "bbb")
        self.assertEqual(stored_keys()["KEY_A"], "aaa")
        self.assertEqual(stored_keys()["KEY_B"], "bbb")
        remove_key("KEY_A")
        self.assertNotIn("KEY_A", stored_keys())
        self.assertIn("KEY_B", stored_keys())

    def test_no_key_name_means_no_key(self):
        self.assertIsNone(resolve(""))
        self.assertIsNone(resolve(None))

    def test_mask_never_reveals_the_middle(self):
        self.assertEqual(mask("sk-abcdefgh1234"), "sk-a…1234")
        self.assertNotIn("abcdefgh", mask("sk-abcdefgh1234"))

    def test_mask_handles_short_and_empty(self):
        self.assertEqual(mask(""), "(none)")
        self.assertEqual(mask(None), "(none)")
        self.assertEqual(mask("tiny"), "****")

    def test_corrupt_file_does_not_raise(self):
        with open(credentials_path(), "w", encoding="utf-8") as handle:
            handle.write("{ this is not json")
        self.assertEqual(stored_keys(), {})
        self.assertIsNone(resolve("ANY_KEY"))

    def test_file_is_owner_only_on_posix(self):
        if os.name != "posix":
            self.skipTest("POSIX permission bits only")
        store_key("SOME_KEY", "value")
        mode = os.stat(credentials_path()).st_mode & 0o777
        # Owner read/write only: the stored value must not be world-readable.
        self.assertEqual(mode, 0o600)


class EndpointStoreTest(TempStorage, unittest.TestCase):
    """The endpoint list is one hand-editable file, and nothing else."""

    def test_starts_empty_because_there_are_no_builtins(self):
        # The whole point of removing the built-in table: a fresh install
        # knows nothing until the user says so.
        self.assertEqual(endpoints(), {})

    def test_add_then_read_back(self):
        add_endpoint("mybox", "https://llm.internal/v1", "MYBOX_KEY", ["m1"], "mine")
        entry = get_endpoint("mybox")
        self.assertEqual(entry["base_url"], "https://llm.internal/v1")
        self.assertEqual(entry["api_key_env"], "MYBOX_KEY")
        self.assertEqual(entry["models"], ["m1"])
        self.assertEqual(entry["note"], "mine")

    def test_name_is_normalised_to_lowercase(self):
        add_endpoint("MyBox", "https://x/v1")
        self.assertIn("mybox", endpoints())

    def test_trailing_slash_is_trimmed(self):
        add_endpoint("x", "https://x/v1/")
        self.assertEqual(get_endpoint("x")["base_url"], "https://x/v1")

    def test_remove_reports_whether_it_did_anything(self):
        add_endpoint("mine", "https://x/v1")
        self.assertTrue(remove_endpoint("mine"))
        self.assertFalse(remove_endpoint("mine"))

    def test_validate_rejects_bad_urls(self):
        self.assertIsNotNone(validate_endpoint({}))
        self.assertIsNotNone(validate_endpoint({"base_url": ""}))
        self.assertIsNotNone(validate_endpoint({"base_url": "ftp://x/v1"}))
        self.assertIsNone(validate_endpoint({"base_url": "http://localhost:11434/v1"}))

    def test_add_rejects_a_bad_url(self):
        with self.assertRaises(ValueError):
            add_endpoint("bad", "not-a-url")

    def test_add_requires_a_name(self):
        with self.assertRaises(ValueError):
            add_endpoint("   ", "https://x/v1")

    def test_hand_typed_models_survive_a_reconnect(self):
        # Somebody who typed a model by hand must not lose it when
        # /connect runs again with nothing discovered.
        add_endpoint("mybox", "https://llm.internal/v1", models=["typed-by-hand"])
        add_endpoint("mybox", "https://llm.internal/v1")
        self.assertEqual(models_for("mybox"), ["typed-by-hand"])

    def test_set_models_replaces_the_list(self):
        add_endpoint("mybox", "https://llm.internal/v1", models=["old"])
        set_models("mybox", ["new-a", "new-b"])
        self.assertEqual(models_for("mybox"), ["new-a", "new-b"])

    def test_set_models_ignores_an_unknown_endpoint(self):
        set_models("never-added", ["x"])
        self.assertEqual(endpoints(), {})

    def test_url_resolves_back_to_its_name(self):
        add_endpoint("mybox", "https://llm.internal/v1")
        self.assertEqual(endpoint_name_for_url("https://llm.internal/v1"), "mybox")
        # Slash and case differences must not make it look unknown.
        self.assertEqual(endpoint_name_for_url("https://llm.internal/v1/"), "mybox")
        self.assertEqual(endpoint_name_for_url("https://LLM.Internal/v1"), "mybox")
        self.assertIsNone(endpoint_name_for_url("https://elsewhere/v1"))

    def test_active_choice_round_trips(self):
        set_active(endpoint="mybox", model="m1", reasoning_effort="high")
        self.assertEqual(
            active(), {"endpoint": "mybox", "model": "m1", "reasoning_effort": "high"}
        )

    def test_active_fields_are_left_alone_when_not_given(self):
        set_active(endpoint="mybox", model="m1", reasoning_effort="high")
        set_active(model="m2")
        self.assertEqual(active()["endpoint"], "mybox")
        self.assertEqual(active()["model"], "m2")
        self.assertEqual(active()["reasoning_effort"], "high")

    def test_none_effort_means_off_not_unset(self):
        # None is a real value here - "do not send the field" - so it
        # must be storable even though it is also the blank default.
        set_active(reasoning_effort="high")
        set_active(reasoning_effort=None)
        self.assertIsNone(active()["reasoning_effort"])

    def test_removing_the_active_endpoint_clears_the_choice(self):
        add_endpoint("mybox", "https://llm.internal/v1")
        set_active(endpoint="mybox", model="m1")
        remove_endpoint("mybox")
        self.assertEqual(active()["endpoint"], "")
        self.assertEqual(active()["model"], "")

    def test_keyless_local_endpoints_are_not_nagged(self):
        self.assertFalse(provider_needs_key("http://localhost:11434/v1", "ANY"))
        self.assertFalse(provider_needs_key("https://x/v1", ""))
        self.assertTrue(provider_needs_key("https://x/v1", "ANY"))


class HandEditTest(TempStorage, unittest.TestCase):
    """Editing the file by hand must work, without running /connect.

    These write JSON directly with no help from ``core.settings``, which
    is the only way to be sure the loader is reading what a human would
    actually type.
    """

    def _write(self, text: str) -> None:
        with open(settings_path(), "w", encoding="utf-8") as handle:
            handle.write(text)

    def test_a_hand_written_endpoint_is_picked_up(self):
        self._write(json.dumps({
            "version": 1,
            "endpoints": {
                "mybox": {
                    "base_url": "https://llm.internal/v1",
                    "api_key_env": "MYBOX_KEY",
                    "models": ["hand-model"],
                }
            },
            "active": {"endpoint": "mybox", "model": "hand-model"},
        }))
        self.assertEqual(models_for("mybox"), ["hand-model"])
        self.assertEqual(active()["model"], "hand-model")

    def test_a_missing_section_is_filled_in(self):
        # Whoever writes this by hand will leave fields out; the file
        # must still load rather than raising on startup.
        self._write('{"endpoints": {"bare": {"base_url": "https://bare/v1"}}}')
        self.assertEqual(get_endpoint("bare")["api_key_env"], "")
        self.assertEqual(get_endpoint("bare")["models"], [])
        self.assertEqual(active()["endpoint"], "")

    def test_broken_json_loads_as_empty_rather_than_crashing(self):
        self._write("{ this is not json")
        self.assertEqual(endpoints(), {})
        self.assertEqual(active()["endpoint"], "")

    def test_entries_without_a_url_are_skipped(self):
        self._write(json.dumps({"endpoints": {"good": {"base_url": "https://g/v1"},
                                              "junk": {"note": "no url"}}}))
        self.assertEqual(list(endpoints()), ["good"])

    def test_a_non_object_file_is_ignored(self):
        self._write('"just a string"')
        self.assertEqual(endpoints(), {})

    def test_saved_output_is_written_for_humans(self):
        # Indented and newline-terminated: this file exists to be read.
        add_endpoint("mybox", "https://llm.internal/v1")
        text = open(settings_path(), encoding="utf-8").read()
        self.assertIn("\n", text)
        self.assertIn('"base_url"', text)
        self.assertTrue(text.endswith("\n"))

    def test_keys_are_never_written_into_the_settings_file(self):
        store_key("MYBOX_KEY", "sk-super-secret")
        add_endpoint("mybox", "https://llm.internal/v1", "MYBOX_KEY")
        text = open(settings_path(), encoding="utf-8").read()
        self.assertNotIn("sk-super-secret", text)


class _Recorder:
    """Captures request bodies and can replay scripted failures."""

    def __init__(self, failures=()):
        self.bodies: list[dict] = []
        self.headers: list[dict] = []
        self.failures = list(failures)

    def __call__(self, request, timeout=None):
        self.bodies.append(json.loads(request.data.decode()))
        self.headers.append(dict(request.headers))
        if self.failures:
            status, detail = self.failures.pop(0)
            payload = json.dumps({"error": {"message": detail}}).encode()
            raise urllib.error.HTTPError(
                "https://x/v1", status, "err", {}, io.BytesIO(payload)
            )
        return _Response()


class _Response:
    def read(self):
        return json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class ReasoningEffortTest(unittest.TestCase):
    def setUp(self):
        self.recorder = _Recorder()
        self.patches = [
            mock.patch.object(urllib.request, "urlopen", self.recorder),
            mock.patch.object(openai_module.time, "sleep"),
        ]
        for patcher in self.patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def _chat(self, **overrides):
        cfg = {
            "provider": "openai",
            "model": "m",
            "base_url": "https://x/v1",
            "api_key_env": "TEST_KEY",
        }
        cfg.update(overrides)
        with mock.patch.dict(os.environ, {"TEST_KEY": "k"}):
            build_llm(cfg).chat([{"role": "user", "content": "hi"}])
        return self.recorder.bodies[-1]

    def test_absent_by_default(self):
        self.assertNotIn("reasoning_effort", self._chat())

    def test_none_sends_nothing(self):
        self.assertNotIn("reasoning_effort", self._chat(reasoning_effort=None))

    def test_each_level_is_forwarded(self):
        for level in REASONING_EFFORTS:
            with self.subTest(level=level):
                self.assertEqual(self._chat(reasoning_effort=level)["reasoning_effort"], level)

    def test_server_that_rejects_it_is_obeyed(self):
        # A local server may 400 on the unknown field; the request must
        # succeed on retry with the field dropped, not fail the turn.
        self.recorder = _Recorder(failures=[(400, "Unknown parameter: reasoning_effort")])
        # Swap setUp's urlopen patch for the failing recorder mid-test.
        self.patches[0].stop()
        self.patches[0] = mock.patch.object(urllib.request, "urlopen", self.recorder)
        self.patches[0].start()

        body = self._chat(reasoning_effort="high")
        self.assertNotIn("reasoning_effort", body)
        self.assertEqual(len(self.recorder.bodies), 2, "should have retried once")
        # The first attempt did send it; only the retry omits it.
        self.assertEqual(self.recorder.bodies[0]["reasoning_effort"], "high")

    def test_reasoning_models_get_the_completion_budget(self):
        # A 400 naming max_completion_tokens forces the retry that
        # renames the token field for the rest of the session.
        self.recorder = _Recorder(
            failures=[(400, "Unsupported parameter: 'max_tokens' is not supported "
                           "with this model. Use 'max_completion_tokens' instead.")]
        )
        self.patches[0].stop()
        self.patches[0] = mock.patch.object(urllib.request, "urlopen", self.recorder)
        self.patches[0].start()

        body = self._chat(reasoning_effort="high")
        self.assertNotIn("max_tokens", body)
        self.assertIn("max_completion_tokens", body)
        self.assertEqual(body["reasoning_effort"], "high")

    def test_invalid_level_is_rejected_by_config(self):
        from core.config import ConfigError

        with self.assertRaises(ConfigError):
            merge_defaults({"llm": {"reasoning_effort": "turbo"}})

    def test_every_valid_level_passes_config(self):
        for level in REASONING_EFFORTS:
            with self.subTest(level=level):
                merged = merge_defaults({"llm": {"reasoning_effort": level}})
                self.assertEqual(merged["llm"]["reasoning_effort"], level)

    def test_a_session_cannot_rewrite_the_module_defaults(self):
        # merge_defaults used to shallow-copy, leaving the nested sections
        # shared with DEFAULTS. One session picking a model then rewrote the
        # defaults for every config loaded after it in the same process.
        one = merge_defaults({})
        one["llm"]["model"] = "some-other-model"
        one["skills"]["auto"] = False
        one["tools"].append("a-tool-only-this-session-has")

        two = merge_defaults({})
        self.assertNotEqual(two["llm"]["model"], "some-other-model")
        self.assertTrue(two["skills"]["auto"])
        self.assertNotIn("a-tool-only-this-session-has", two["tools"])

    def test_the_module_defaults_survive_a_session(self):
        from core.config import DEFAULTS

        one = merge_defaults({})
        one["llm"]["model"] = "some-other-model"
        self.assertEqual(DEFAULTS["llm"]["model"], "gpt-4o")


class ModelDiscoveryTest(TempStorage, unittest.TestCase):
    """Fetching a catalogue, and picking from it."""

    def _serve(self, payload, status=200):
        body = json.dumps(payload).encode()
        response = _Response()
        response.read = lambda: body
        return mock.patch.object(
            urllib.request, "urlopen", lambda req, timeout=None: response
        )

    def test_ids_are_extracted_and_sorted(self):
        payload = {"object": "list", "data": [{"id": "b"}, {"id": "a"}]}
        with self._serve(payload):
            self.assertEqual(fetch_models("https://x/v1"), ["a", "b"])

    def test_alternate_shapes_are_tolerated(self):
        with self._serve({"data": [{"name": "n1"}, {"model": "n2"}]}):
            self.assertEqual(fetch_models("https://x/v1"), ["n1", "n2"])
        with self._serve({"data": ["plain"]}):
            self.assertEqual(fetch_models("https://x/v1"), ["plain"])
        with self._serve([{"id": "bare-list"}]):
            self.assertEqual(fetch_models("https://x/v1"), ["bare-list"])

    def test_non_chat_entries_are_left_out(self):
        payload = {"data": [{"id": "text-embedding-3-large"}, {"id": "gpt-4o"}]}
        with self._serve(payload):
            self.assertEqual(fetch_models("https://x/v1"), ["gpt-4o"])

    def test_models_that_cannot_chat_are_left_out(self):
        # A large account advertises far more than it can chat with, and
        # those rows are not neutral - they are wrong answers.
        noise = (
            "gpt-4o-realtime-preview", "gpt-4o-audio-preview", "gpt-4o-transcribe",
            "gpt-image-1", "sora-2", "omni-moderation-latest", "text-embedding-3-small",
            "gpt-3.5-turbo-instruct", "computer-use-preview", "tts-1-hd",
        )
        payload = {"data": [{"id": name} for name in noise] + [{"id": "gpt-4o"}]}
        with self._serve(payload):
            self.assertEqual(fetch_models("https://x/v1"), ["gpt-4o"])

    def test_dated_snapshots_rank_below_the_name_they_are_a_copy_of(self):
        # gpt-4o-2024-11-20 sorted above gpt-4o alphabetically, which
        # pushed the name everyone knows down the list.
        payload = {
            "data": [
                {"id": "gpt-4o-2024-11-20"},
                {"id": "gpt-4o"},
                {"id": "claude-3-5-sonnet-20241022"},
                {"id": "claude-3-5-sonnet"},
            ]
        }
        with self._serve(payload):
            self.assertEqual(
                fetch_models("https://x/v1"),
                ["claude-3-5-sonnet", "gpt-4o", "claude-3-5-sonnet-20241022",
                 "gpt-4o-2024-11-20"],
            )

    def test_families_stay_together(self):
        # Ranking exists to bury snapshots, not to scramble the list.
        payload = {"data": [{"id": n} for n in ("o3-mini", "gpt-4o", "a-model")]}
        with self._serve(payload):
            self.assertEqual(fetch_models("https://x/v1"), ["a-model", "gpt-4o", "o3-mini"])

    def test_trailing_slash_is_not_doubled(self):
        seen = {}

        def capture(request, timeout=None):
            seen["url"] = request.full_url
            return _Response()

        with mock.patch.object(urllib.request, "urlopen", capture):
            fetch_models("https://x/v1/")
        self.assertEqual(seen["url"], "https://x/v1/models")

    def test_a_stored_key_is_sent(self):
        store_key("DISCOVERY_KEY", "sk-discovery-123456")
        sent = {}

        def capture(request, timeout=None):
            sent["auth"] = request.headers.get("Authorization", "")
            return _Response()

        with mock.patch.object(urllib.request, "urlopen", capture):
            fetch_models("https://x/v1", "DISCOVERY_KEY")
        self.assertEqual(sent["auth"], "Bearer sk-discovery-123456")

    def test_bad_key_gets_actionable_advice(self):
        from core.agent.exceptions import LLMError

        failure = urllib.error.HTTPError(
            "https://x/v1", 401, "err", {}, io.BytesIO(b'{"error":"nope"}')
        )
        with mock.patch.object(urllib.request, "urlopen", side_effect=failure):
            with self.assertRaises(LLMError) as caught:
                fetch_models("https://x/v1", "DISCOVERY_KEY")
        self.assertIn("refused the key", str(caught.exception))
        self.assertIn("/model", str(caught.exception))

    def test_missing_catalogue_suggests_typing_it(self):
        from core.agent.exceptions import LLMError

        failure = urllib.error.HTTPError(
            "https://x/v1", 404, "err", {}, io.BytesIO(b"not found")
        )
        with mock.patch.object(urllib.request, "urlopen", side_effect=failure):
            with self.assertRaises(LLMError) as caught:
                fetch_models("https://x/v1")
        self.assertIn("/model", str(caught.exception))

    def test_unreachable_host_is_reported_plainly(self):
        from core.agent.exceptions import LLMError

        with mock.patch.object(
            urllib.request, "urlopen", side_effect=TimeoutError("timed out")
        ):
            with self.assertRaises(LLMError) as caught:
                fetch_models("https://x/v1")
        self.assertIn("could not reach", str(caught.exception))

    def test_unrecognised_shape_yields_no_models(self):
        # An endpoint answering with something we do not understand is
        # not an error worth failing the command over; the caller simply
        # reports an empty catalogue and suggests typing a name.
        with self._serve({"unexpected": True}):
            self.assertEqual(fetch_models("https://x/v1"), [])


class ReasoningDetectionTest(unittest.TestCase):
    def test_reasoning_families_are_recognised(self):
        for name in ("o3-mini", "o4-mini", "gpt-5", "deepseek-r1", "QwQ-32B",
                     "something-thinking", "o1-preview"):
            with self.subTest(name=name):
                self.assertTrue(is_reasoning_model(name))

    def test_ordinary_models_are_not(self):
        for name in ("gpt-4o", "gpt-4o-mini", "llama-3.1-70b", "claude-3-5-sonnet", ""):
            with self.subTest(name=name):
                self.assertFalse(is_reasoning_model(name))


class ModelMenuTest(TempStorage, unittest.TestCase):
    """Picking from the model menu should settle the effort too.

    Reasoning is a property of the chosen model, not a separate setting,
    so one menu pass has to produce both. The menu itself is exercised
    in the terminal-application layer (``core.tui.overlays.MenuOverlay``);
    here it is stubbed so these tests cover the wiring around it - what
    gets listed, and what the choice does.
    """

    def setUp(self):
        super().setUp()
        self.workspace = tempfile.mkdtemp(prefix="mantra-pick-")
        self.addCleanup(lambda: shutil.rmtree(self.workspace, ignore_errors=True))
        self.session = make_session(self.workspace, [])
        # merge_defaults has no base_url, and the menu needs one.
        self.session.config["llm"]["base_url"] = "https://x/v1"

    def _pick(self, models, answers, effort=None):
        """Run the menu against a fixed catalogue with scripted choices.

        ``console._menu`` is the seam: it is the only thing that talks to
        the terminal, so stubbing it keeps the tests off the tty while
        still covering the calls on either side. Every model asks twice
        - model, then effort - so a bare ``_pick([...], ["gpt-4o"])``
        reads as "choose gpt-4o, leave effort off".
        """
        scripted = list(answers) + [effort]
        with mock.patch.object(console, "fetch_models", return_value=models), mock.patch.object(
            console, "_menu", side_effect=scripted
        ):
            ok = console._choose_model(self.session)
        return ok, self.session.config["llm"]

    def test_choice_sets_the_model(self):
        ok, llm = self._pick(["gpt-4o", "gpt-4o-mini"], ["gpt-4o-mini"])
        self.assertTrue(ok)
        self.assertEqual(llm["model"], "gpt-4o-mini")

    def test_ordinary_model_clears_any_stale_effort(self):
        self.session.config["llm"]["reasoning_effort"] = "high"
        ok, llm = self._pick(["gpt-4o"], ["gpt-4o"])
        self.assertEqual(llm["model"], "gpt-4o")
        self.assertIsNone(llm["reasoning_effort"])

    def test_reasoning_model_is_offered_an_effort(self):
        ok, llm = self._pick(["gpt-4o", "o3-mini"], ["o3-mini", "high"])
        self.assertEqual(llm["model"], "o3-mini")
        self.assertEqual(llm["reasoning_effort"], "high")

    def test_effort_off_sends_nothing(self):
        # "off" is a deliberate choice, not an unanswered prompt, so it
        # must clear the field rather than fall back to a default.
        self.session.config["llm"]["reasoning_effort"] = "high"
        ok, llm = self._pick(["o3-mini"], ["o3-mini", "off"])
        self.assertIsNone(llm["reasoning_effort"])

    def test_every_model_is_offered_an_effort(self):
        # Effort used to be gated on the name looking like a reasoning
        # model, which made the choice vanish for most catalogues. It is
        # part of picking a model now, so it is always asked - and for a
        # model that ignores the field, "off" is the right answer rather
        # than a missing one.
        with mock.patch.object(console, "fetch_models", return_value=["gpt-4o"]), \
             mock.patch.object(console, "_menu", side_effect=["gpt-4o", "high"]) as menu:
            console._choose_model(self.session)
        self.assertEqual(menu.call_count, 2, "effort is offered for any model")
        self.assertEqual(self.session.config["llm"]["reasoning_effort"], "high")

    def test_the_effort_menu_opens_on_the_level_already_in_force(self):
        # Re-opening the menu and losing the current level would mean
        # silently changing something the operator never touched.
        self.session.config["llm"]["reasoning_effort"] = "high"
        with mock.patch.object(console, "fetch_models", return_value=["o3-mini"]), \
             mock.patch.object(console, "_menu", side_effect=["o3-mini", "high"]) as menu:
            console._choose_model(self.session)
        effort_call = menu.call_args_list[1]
        self.assertEqual(effort_call.kwargs["cursor"], 4)  # off, minimal, low, medium, high

    def test_the_effort_menu_marks_the_current_level(self):
        self.session.config["llm"]["reasoning_effort"] = "low"
        with mock.patch.object(console, "fetch_models", return_value=["o3-mini"]), \
             mock.patch.object(console, "_menu", side_effect=["o3-mini", "low"]) as menu:
            console._choose_model(self.session)
        options = menu.call_args_list[1][0][2]
        marked = [o.value for o in options if "current" in o.hint]
        self.assertEqual(marked, ["low"])

    def test_cancel_leaves_the_model_alone(self):
        before = self.session.config["llm"]["model"]
        ok, llm = self._pick(["gpt-4o"], [None])
        self.assertFalse(ok)
        self.assertEqual(llm["model"], before)

    def test_discovery_populates_the_completer_cache(self):
        self._pick(["gpt-4o", "gpt-4o-mini"], ["gpt-4o"])
        self.assertEqual(self.session.known_models, ["gpt-4o", "gpt-4o-mini"])

    def test_the_catalogue_is_remembered_for_the_endpoint(self):
        add_endpoint("x", "https://x/v1")
        self.session.config["llm"]["base_url"] = "https://x/v1"
        self._pick(["gpt-4o"], ["gpt-4o"])
        self.assertEqual(models_for("x"), ["gpt-4o"])

    def test_an_unreachable_endpoint_falls_back_to_the_saved_list(self):
        # A dead endpoint must not block setup: the models recorded in
        # the settings file are still a perfectly good menu.
        from core.agent.exceptions import LLMError

        add_endpoint("x", "https://x/v1", models=["saved-model"])
        with mock.patch.object(
            console, "fetch_models", side_effect=LLMError("nope")
        ), mock.patch.object(console, "_menu", return_value="saved-model"):
            ok = console._choose_model(self.session)
        self.assertTrue(ok)
        self.assertEqual(self.session.config["llm"]["model"], "saved-model")

    def test_nothing_known_at_all_is_reported_not_silent(self):
        from io import StringIO
        from contextlib import redirect_stdout

        self.session.config["llm"]["base_url"] = "https://x/v1"
        buf = StringIO()
        with mock.patch.object(console, "fetch_models", return_value=[]), \
             mock.patch.object(console, "_menu", return_value=None):
            with redirect_stdout(buf):
                ok = console._choose_model(self.session)
        self.assertFalse(ok)
        self.assertIn("no models to choose from", buf.getvalue())
        # The advice has to name the file, since that is where a model
        # gets added when /connect cannot reach the endpoint.
        self.assertIn(str(settings_path()), buf.getvalue())

    def test_without_an_endpoint_there_is_no_menu(self):
        from io import StringIO
        from contextlib import redirect_stdout

        self.session.config["llm"]["base_url"] = ""
        buf = StringIO()
        with mock.patch.object(console, "fetch_models") as fetch:
            with redirect_stdout(buf):
                ok = console._choose_model(self.session)
        self.assertFalse(ok)
        fetch.assert_not_called()
        self.assertIn("/model", buf.getvalue())

    def test_the_pairing_is_remembered_for_next_time(self):
        self._pick(["o3-mini"], ["o3-mini", "low"])
        self.assertEqual(active()["model"], "o3-mini")
        self.assertEqual(active()["reasoning_effort"], "low")


class ModelCompletionTest(unittest.TestCase):
    def test_model_token_completes_from_the_catalogue(self):
        class _Session:
            known_models = ["gpt-4o", "gpt-4o-mini", "o3-mini"]

        class _Sandbox:
            root = "."

        _Session.sandbox = _Sandbox()
        from core.console import ConsoleCompleter

        completer = ConsoleCompleter(_Session())
        result = completer.complete("/model gpt-4o", 12)
        self.assertIsNotNone(result)
        self.assertIn("gpt-4o", result.items)
        self.assertIn("gpt-4o-mini", result.items)

    def test_reasoning_models_are_labelled(self):
        class _Session:
            known_models = ["o3-mini"]

        class _Sandbox:
            root = "."

        _Session.sandbox = _Sandbox()
        from core.console import ConsoleCompleter

        result = ConsoleCompleter(_Session()).complete("/model o3", 9)
        self.assertIn("reasons", result.labels[0])

    def test_no_catalogue_means_no_suggestions(self):
        class _Session:
            known_models = []

        class _Sandbox:
            root = "."

        _Session.sandbox = _Sandbox()
        from core.console import ConsoleCompleter

        self.assertIsNone(ConsoleCompleter(_Session()).complete("/model x", 8))

    def test_cursor_past_the_end_is_clamped(self):
        class _Session:
            known_models = []

        class _Sandbox:
            root = "."

        _Session.sandbox = _Sandbox()
        from core.console import ConsoleCompleter

        # A cursor past the end is clamped and returns None, not an error.
        self.assertIsNone(ConsoleCompleter(_Session()).complete("/model x", 99))
