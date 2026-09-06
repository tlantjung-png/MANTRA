"""Tests for the one-step BYOK flow (``/connect``) and the startup screen.

``/connect`` is the only setup a user needs: give a URL and a key, and
the endpoint lists its own models into a menu. There is no provider
table to learn and no second command - the endpoint, the key and the
model list are saved to one hand-editable file. Every test here
redirects storage to a temporary directory and mocks HTTP, so nothing
touches the real ``~/.mantra`` or the network.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_TESTS_DIR)
for _path in (os.path.join(_PROJECT_ROOT, "."), _PROJECT_ROOT, _TESTS_DIR):
    if _path not in sys.path:
        sys.path.insert(0, _path)
import core.console as console
from core.console import (
    SLASH_COMMANDS,
    _connect,
    _derive_key_env,
    _derive_name,
    _needs_first_run,
)
from core.agent.keys import has_stored, resolve
from core.agent.settings import endpoints, settings_path
from _helpers import make_session


class TempStorage:
    """Point both storage files at a scratch directory for the test.

    Keys and settings are deliberately separate: the settings file is
    meant to be opened in an editor, so it must never contain a key.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(
            os.environ,
            {
                "MANTRA_CREDENTIALS": os.path.join(self.tmp.name, "creds.json"),
                "MANTRA_SETTINGS": os.path.join(self.tmp.name, "config.json"),
            },
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(self.tmp.cleanup)


class _Resp:
    """A minimal stand-in for what ``urlopen`` returns."""

    def __init__(self, payload) -> None:
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


# A realistic /models response, including noise the fetcher must filter.
CATALOGUE = {
    "data": [
        {"id": "my-model-a"},
        {"id": "my-model-b"},
        {"id": "text-embedding-3-large"},
        {"id": "whisper-1"},
    ]
}


# ---- Name and env-var derivation ----------------------------------------


class DerivationTest(unittest.TestCase):
    """Short handles for endpoints, so the operator types nothing extra."""

    def test_strips_api_prefix_and_tld(self):
        self.assertEqual(_derive_name("https://api.openai.com/v1"), "openai")

    def test_strips_www(self):
        self.assertEqual(_derive_name("https://www.example.com/v1"), "example")

    def test_ignores_path(self):
        self.assertEqual(_derive_name("https://openrouter.ai/api/v1"), "openrouter")

    def test_ignores_port(self):
        self.assertEqual(_derive_name("http://localhost:11434/v1"), "localhost")

    def test_host_without_scheme(self):
        # A scheme-ful host resolves to its first label.
        self.assertEqual(_derive_name("https://llm.internal/v1"), "llm")

    def test_unparseable_falls_back(self):
        self.assertEqual(_derive_name("not a url at all"), "endpoint")

    def test_key_env_from_name(self):
        self.assertEqual(_derive_key_env("openai"), "OPENAI_API_KEY")
        self.assertEqual(_derive_key_env("openrouter"), "OPENROUTER_API_KEY")

    def test_key_env_is_upper_and_underscored(self):
        self.assertRegex(_derive_key_env("my-box"), r"^[A-Z0-9_]+$")


# ---- --endpoint override -------------------------------------------------


class EndpointOverrideTest(TempStorage, unittest.TestCase):
    """``--endpoint URL`` must switch the key env too.

    The config default ``api_key_env`` is ``OPENAI_API_KEY``, which is
    always truthy: naively ``or``-ing a derivation onto it never fires,
    so overriding the endpoint kept sending the wrong provider's key.
    The override must prefer the saved endpoint's env var, then derive
    from the hostname.
    """

    def test_override_uses_saved_endpoint_key_env(self):
        from core.agent.settings import add_endpoint

        add_endpoint("inceptionlabs", "https://api.inceptionlabs.ai/v1", "INCEPTIONLABS_API_KEY")
        config = {"llm": {"api_key_env": "OPENAI_API_KEY"}}
        console._apply_endpoint_override(config, "https://api.inceptionlabs.ai/v1")
        self.assertEqual(config["llm"]["base_url"], "https://api.inceptionlabs.ai/v1")
        self.assertEqual(config["llm"]["api_key_env"], "INCEPTIONLABS_API_KEY")

    def test_override_derives_env_when_endpoint_unknown(self):
        config = {"llm": {"api_key_env": "OPENAI_API_KEY"}}
        console._apply_endpoint_override(config, "https://example.com/v1")
        self.assertEqual(config["llm"]["base_url"], "https://example.com/v1")
        self.assertEqual(config["llm"]["api_key_env"], "EXAMPLE_API_KEY")


# ---- /connect ------------------------------------------------------------


class ConnectTest(TempStorage, unittest.TestCase):
    """The whole setup in one exchange."""

    def setUp(self):
        TempStorage.setUp(self)
        self.workspace = tempfile.mkdtemp(prefix="mantra-connect-ws-")
        self.addCleanup(shutil.rmtree, self.workspace, ignore_errors=True)
        self.session = make_session(self.workspace, [])

    def _run(self, *choices: str, url: str = "https://llm.internal/v1",
             key: str = "sk-test-1234", effort: str | None = None):
        """Drive ``_connect`` with scripted menu selections.

        HTTP is stubbed (the flow fetches a catalogue), ``_menu`` is
        stubbed instead of ``input`` (the real flow is cursor-driven),
        and the scripted answers are padded with an effort-menu pick.
        """
        buf = io.StringIO()
        scripted = list(choices) + [effort]
        patchers = [
            mock.patch.object(console, "_menu", side_effect=scripted),
            mock.patch("urllib.request.urlopen", return_value=_Resp(CATALOGUE)),
        ]
        for p in patchers:
            p.start()
            self.addCleanup(p.stop)
        with redirect_stdout(buf):
            ok = _connect(self.session, [url, key])
        return ok, buf.getvalue()

    def test_stores_the_key(self):
        ok, out = self._run("my-model-a")
        self.assertTrue(ok)
        env = self.session.config["llm"]["api_key_env"]
        self.assertTrue(has_stored(env))
        self.assertEqual(resolve(env), "sk-test-1234")

    def test_fetches_and_applies_chosen_model(self):
        ok, out = self._run("my-model-a")
        self.assertTrue(ok)
        # The embedding and whisper noise must not reach the menu the
        # operator chooses from.
        self.assertEqual(self.session.config["llm"]["model"], "my-model-a")
        self.assertEqual(
            self.session.known_models, ["my-model-a", "my-model-b"]
        )

    def test_large_catalogue_offers_a_model_choice_first(self):
        # OpenRouter-style: hundreds of models must not dump straight
        # into the menu; the operator picks how to find a model first.
        from core.agent.settings import add_endpoint

        add_endpoint("mine", "https://big.test/v1", "", [])
        self.assertTrue(self.session.use_endpoint("mine"))
        with mock.patch.object(console, "fetch_models",
                               return_value=[f"model-{i}" for i in range(80)]), \
             mock.patch.object(console, "_menu",
                               side_effect=[console.SHOW_FIRST_MODELS, "model-3", None]) as menu:
            with redirect_stdout(io.StringIO()):
                ok = console._choose_model(self.session)
        self.assertTrue(ok)
        first_options = [o.value for o in menu.call_args_list[0][0][2]]
        self.assertIn(console.SHOW_ALL_MODELS, first_options)
        self.assertIn(console.SHOW_FIRST_MODELS, first_options)
        self.assertEqual(self.session.config["llm"]["model"], "model-3")

    def test_the_menu_is_offered_every_model_the_endpoint_serves(self):
        with mock.patch.object(console, "_menu", return_value=None) as menu:
            with mock.patch("urllib.request.urlopen", return_value=_Resp(CATALOGUE)):
                _connect(self.session, ["https://llm.internal/v1", "sk-1"])
        title, options = menu.call_args[0][1], menu.call_args[0][2]
        self.assertIn("llm.internal", title)
        # The catalogue, plus the escape hatch for a model the endpoint
        # does not advertise.
        self.assertEqual(
            [o.value for o in options],
            ["my-model-a", "my-model-b", console.TYPE_A_MODEL],
        )

    def test_cancelling_keeps_current_model(self):
        before = self.session.config["llm"]["model"]
        ok, out = self._run(None)
        self.assertFalse(ok)
        self.assertEqual(self.session.config["llm"]["model"], before)

    def test_endpoint_is_switched(self):
        self._run("my-model-a")
        self.assertEqual(
            self.session.config["llm"]["base_url"], "https://llm.internal/v1"
        )

    def test_endpoint_is_remembered_for_next_time(self):
        self._run("my-model-a")
        saved = endpoints()
        self.assertIn("llm", saved)
        self.assertEqual(saved["llm"]["base_url"], "https://llm.internal/v1")

    def test_the_saved_file_is_the_one_the_user_can_edit(self):
        self._run("my-model-a")
        # The message the operator sees has to name the real path,
        # otherwise "you can edit this by hand" is not actionable.
        self.assertTrue(settings_path().is_file())
        on_disk = json.loads(settings_path().read_text(encoding="utf-8"))
        self.assertIn("llm", on_disk["endpoints"])

    def test_discovered_models_are_saved_for_offline_use(self):
        self._run("my-model-a")
        self.assertEqual(endpoints()["llm"]["models"], ["my-model-a", "my-model-b"])

    def test_bare_host_gets_a_scheme(self):
        ok, out = self._run("my-model-a", url="llm.internal/v1")
        self.assertTrue(ok)
        self.assertEqual(
            self.session.config["llm"]["base_url"], "https://llm.internal/v1"
        )

    def test_trailing_slash_is_trimmed(self):
        self._run("my-model-a", url="https://llm.internal/v1/")
        self.assertEqual(
            self.session.config["llm"]["base_url"], "https://llm.internal/v1"
        )

    def test_existing_key_is_not_re_asked(self):
        # A key already in the environment must not be prompted for, and
        # must not be overwritten by an empty answer.
        env = mock.patch.dict(os.environ, {"LLM_API_KEY": "sk-already-set"})
        env.start()
        self.addCleanup(env.stop)
        ok, out = self._run("my-model-a", url="https://llm.internal/v1", key="")
        self.assertTrue(ok)
        self.assertNotIn("key stored", out)

    def test_no_key_skips_fetch(self):
        # With no key and no way to ask (non-tty style empty read), the
        # flow must stop cleanly rather than firing an unauthenticated
        # request that comes back as a confusing 401.
        buf = io.StringIO()
        with mock.patch("core.console._ask_secret", return_value=""):
            with redirect_stdout(buf):
                ok = _connect(self.session, ["https://llm.internal/v1"])
        self.assertFalse(ok)
        self.assertIn("no key given", buf.getvalue())

    def test_no_key_points_at_the_settings_file(self):
        # Telling someone to fix it without saying where is not help.
        buf = io.StringIO()
        with mock.patch("core.console._ask_secret", return_value=""):
            with redirect_stdout(buf):
                _connect(self.session, ["https://llm.internal/v1"])
        self.assertIn(str(settings_path()), buf.getvalue())

    def test_rejects_garbage_url(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            ok = _connect(self.session, ["ftp://nope", "sk-1"])
        self.assertFalse(ok)
        self.assertIn("http://", buf.getvalue())

    def test_url_without_a_host_is_refused(self):
        # A well-formed scheme with nothing behind it slips past the URL
        # check, so the hostname is verified separately.
        buf = io.StringIO()
        with redirect_stdout(buf):
            ok = _connect(self.session, ["http:///v1", "sk-1"])
        self.assertFalse(ok)
        self.assertIn("hostname", buf.getvalue())

    def test_localhost_needs_no_key(self):
        # A local endpoint must not demand a credential it will not use.
        ok, out = self._run("my-model-a", url="http://localhost:11434/v1", key="")
        self.assertTrue(ok)
        self.assertNotIn("key stored", out)


# ---- Replacing a key -----------------------------------------------------


class KeyReplacementTest(TempStorage, unittest.TestCase):
    """A rejected key must be fixable without editing files by hand.

    Regression: once any key was in the store, re-running /connect
    skipped the key prompt, so a mistyped key became permanent.
    """

    def setUp(self):
        TempStorage.setUp(self)
        self.workspace = tempfile.mkdtemp(prefix="mantra-key-ws-")
        self.addCleanup(shutil.rmtree, self.workspace, ignore_errors=True)
        self.session = make_session(self.workspace, [])
        self.session.config["llm"]["base_url"] = "https://llm.internal/v1"
        self.session.config["llm"]["api_key_env"] = _derive_key_env(
            _derive_name("https://llm.internal/v1")
        )
        from core.agent.keys import store as store_key
        from core.agent.settings import add_endpoint

        store_key(self.session.config["llm"]["api_key_env"], "sk-wrong-0000")
        add_endpoint(
            "llm",
            "https://llm.internal/v1",
            self.session.config["llm"]["api_key_env"],
        )

    def _replace(self, new_key: str):
        return _connect_key_with(self.session, new_key)

    def test_connect_key_overwrites_a_stored_key(self):
        self.assertTrue(self._replace("sk-right-9999"))
        env = self.session.config["llm"]["api_key_env"]
        self.assertEqual(resolve(env), "sk-right-9999")

    def test_connect_key_replaces_even_though_one_is_stored(self):
        # The old guard skipped the prompt whenever has_stored() was
        # true, which is the lockout.
        env = self.session.config["llm"]["api_key_env"]
        self.assertTrue(has_stored(env))
        self.assertTrue(self._replace("sk-right-9999"))
        self.assertNotEqual(resolve(env), "sk-wrong-0000")

    def test_a_blank_answer_cancels_and_keeps_the_old_key(self):
        env = self.session.config["llm"]["api_key_env"]
        self.assertFalse(self._replace(""))
        self.assertEqual(resolve(env), "sk-wrong-0000")

    def test_the_old_value_is_shown_masked_before_replacing(self):
        from core.agent.keys import mask

        buf = io.StringIO()
        with mock.patch("core.console._read_secret", return_value="sk-right-9999"):
            with redirect_stdout(buf):
                console._replace_key(self.session)
        self.assertIn(mask("sk-wrong-0000"), buf.getvalue())

    def test_a_rejected_key_is_offered_as_a_fix_when_nothing_is_listed(self):
        # The same lockout, reached from the other end: discovery fails
        # with a 401 and the picker has nothing to show.
        from core.agent.exceptions import LLMError

        options_seen = {}

        def fake_menu(session, title, options, **kwargs):
            # Capture the menu contents without triggering a selection.
            options_seen["values"] = [o.value for o in options]
            return None

        with mock.patch.object(console, "_menu", side_effect=fake_menu):
            with mock.patch.object(
                console,
                "fetch_models",
                side_effect=LLMError("the endpoint refused the key (HTTP 401)."),
            ):
                console._choose_model(self.session)
        self.assertIn(console.RE_ENTER_KEY, options_seen["values"])

    def test_an_unreachable_endpoint_offers_typing_a_name(self):
        # A gateway with no catalogue is not an auth problem, so the
        # remedy offered has to be a different one.
        from core.agent.exceptions import LLMError

        options_seen = {}

        def fake_menu(session, title, options, **kwargs):
            # Capture the menu contents without triggering a selection.
            options_seen["values"] = [o.value for o in options]
            return None

        with mock.patch.object(console, "_menu", side_effect=fake_menu):
            with mock.patch.object(
                console, "fetch_models", side_effect=LLMError("could not reach it")
            ):
                console._choose_model(self.session)
        self.assertNotIn(console.RE_ENTER_KEY, options_seen["values"])
        self.assertIn(console.TYPE_A_MODEL, options_seen["values"])

    def test_an_explicit_key_argument_always_wins(self):
        # /connect <url> <key> re-run with a corrected key must take.
        with mock.patch("urllib.request.urlopen", return_value=_Resp(CATALOGUE)):
            with mock.patch.object(console, "_menu", side_effect=["my-model-a", None]):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    console._connect(
                        self.session, ["https://llm.internal/v1", "sk-fixed-4321"]
                    )
        env = self.session.config["llm"]["api_key_env"]
        self.assertEqual(resolve(env), "sk-fixed-4321")

    def test_direct_key_form_honors_custom_api_key_env(self):
        # /connect key <name> <key> must store under the endpoint's own
        # api_key_env, not the env derived from the short name - otherwise
        # the key lands in a variable the resolver never reads (an orphan).
        from core.agent.keys import stored_keys
        from core.agent.settings import add_endpoint

        add_endpoint(
            "inceptionlabs", "https://api.inceptionlabs.ai/v1", "MY_CUSTOM_ENV"
        )
        buf = io.StringIO()
        with redirect_stdout(buf):
            ok = console._connect(
                self.session, ["key", "inceptionlabs", "sk-custom-1"]
            )
        self.assertTrue(ok)
        self.assertIn("key stored for inceptionlabs", buf.getvalue())
        self.assertEqual(resolve("MY_CUSTOM_ENV"), "sk-custom-1")
        # Nothing under the derived env name: no orphan credential.
        self.assertNotIn("INCEPTIONLABS_API_KEY", stored_keys())

    def test_direct_key_form_falls_back_to_derived_env_for_unknown_name(self):
        # A name with no saved endpoint still works (derived env), so the
        # one-liner is usable before /connect is ever run.
        buf = io.StringIO()
        with redirect_stdout(buf):
            ok = console._connect(self.session, ["key", "mybox", "sk-box-2"])
        self.assertTrue(ok)
        self.assertEqual(resolve(_derive_key_env("mybox")), "sk-box-2")


class CredentialStoreTest(TempStorage, unittest.TestCase):
    """Credential writes stay atomic; no insecure direct-write fallback."""

    def test_store_writes_atomically_without_leftover_temp(self):
        from core.agent.keys import credentials_path, store, stored_keys

        store("A", "1")
        store("B", "2")
        self.assertEqual(stored_keys(), {"A": "1", "B": "2"})
        leftovers = [
            p for p in os.listdir(credentials_path().parent) if p.endswith(".tmp")
        ]
        self.assertEqual(leftovers, [])

    def test_failed_atomic_replace_raises_and_writes_nothing(self):
        """When the atomic replace fails, store() must raise instead of
        falling back to a direct write to the target path, which could
        follow a planted symlink and silently weaken the guarantee."""
        from core.agent.keys import credentials_path, store

        target = credentials_path()
        target.write_text('{"keys": {"old": "keep"}}', encoding="utf-8")
        with mock.patch("pathlib.Path.replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                store("NEW_KEY", "sk-new")
        # No fallback write happened; the previous content survives.
        on_disk = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(on_disk["keys"], {"old": "keep"})
        self.assertNotIn("NEW_KEY", on_disk["keys"])


def _connect_key_with(session, new_key: str) -> bool:
    """Run the key replacement with a scripted answer."""
    with mock.patch("core.console._read_secret", return_value=new_key):
        buf = io.StringIO()
        with redirect_stdout(buf):
            return console._replace_key(session)


# ---- First-run detection -------------------------------------------------


class FirstRunTest(TempStorage, unittest.TestCase):
    """Startup should guide a fresh operator and leave a working one alone."""

    def setUp(self):
        TempStorage.setUp(self)
        self.workspace = tempfile.mkdtemp(prefix="mantra-firstrun-")
        self.addCleanup(shutil.rmtree, self.workspace, ignore_errors=True)
        self.session = make_session(self.workspace, [])

    def test_no_endpoint_needs_setup(self):
        self.session.config["llm"]["base_url"] = ""
        self.assertTrue(_needs_first_run(self.session))

    def test_configured_key_means_connected(self):
        llm = self.session.config["llm"]
        llm["base_url"] = "https://api.openai.com/v1"
        llm["api_key_env"] = "OPENAI_API_KEY"
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-x"}):
            self.assertFalse(_needs_first_run(self.session))

    def test_stored_key_means_connected(self):
        from core.agent.keys import store as store_key

        store_key("OPENAI_API_KEY", "sk-stored")
        llm = self.session.config["llm"]
        llm["base_url"] = "https://api.openai.com/v1"
        llm["api_key_env"] = "OPENAI_API_KEY"
        # Drop the env var so only the stored key can satisfy the check.
        # The dict is snapshotted rather than cleared: wiping the
        # environment also removes HOME, which Path.home() needs.
        with mock.patch.dict(os.environ):
            os.environ.pop("OPENAI_API_KEY", None)
            self.assertFalse(_needs_first_run(self.session))

    def test_missing_key_needs_setup(self):
        llm = self.session.config["llm"]
        llm["base_url"] = "https://api.openai.com/v1"
        llm["api_key_env"] = "OPENAI_API_KEY"
        with mock.patch.dict(os.environ):
            os.environ.pop("OPENAI_API_KEY", None)
            self.assertTrue(_needs_first_run(self.session))

    def test_localhost_never_needs_setup(self):
        llm = self.session.config["llm"]
        llm["base_url"] = "http://localhost:11434/v1"
        llm["api_key_env"] = "OPENAI_API_KEY"
        self.assertFalse(_needs_first_run(self.session))


# ---- Wiring --------------------------------------------------------------


class WiringTest(unittest.TestCase):
    """The command is reachable from everywhere the operator might try."""

    def test_model_in_slash_commands(self):
        self.assertTrue(any(c == "/model" for c, _ in SLASH_COMMANDS))

    def test_model_in_help_text(self):
        self.assertIn("/model", console.HELP_TEXT)

    def test_dispatch_routes_model(self):
        from core.console import dispatch

        workspace = tempfile.mkdtemp(prefix="mantra-dispatch-")
        self.addCleanup(shutil.rmtree, workspace, ignore_errors=True)
        session = make_session(workspace, [])
        with mock.patch("core.console._model_command", return_value=True) as fake:
            self.assertTrue(dispatch(session, "/model"))
            fake.assert_called_once_with(session, [])

    def test_connect_is_no_longer_a_command(self):
        # /connect was merged into /model and removed: the bare name is
        # an unknown command now, never routed to the model manager.
        from core.console import dispatch

        workspace = tempfile.mkdtemp(prefix="mantra-dispatch-")
        self.addCleanup(shutil.rmtree, workspace, ignore_errors=True)
        session = make_session(workspace, [])
        buf = io.StringIO()
        with mock.patch("core.console._model_command", return_value=True) as fake:
            with redirect_stdout(buf):
                self.assertTrue(dispatch(session, "/connect"))
            fake.assert_not_called()
        self.assertIn("unknown command", buf.getvalue())

    def test_dispatch_passes_url_and_key_under_model(self):
        from core.console import dispatch

        workspace = tempfile.mkdtemp(prefix="mantra-dispatch-")
        self.addCleanup(shutil.rmtree, workspace, ignore_errors=True)
        session = make_session(workspace, [])
        with mock.patch("core.console._connect", return_value=True) as fake:
            dispatch(session, "/model https://x.test/v1 sk-1")
            fake.assert_called_once_with(session, ["https://x.test/v1", "sk-1"])

    def test_old_connect_aliases_are_no_longer_commands(self):
        """/connect, /setup, /login, /endpoint were merged away."""
        from core.console import dispatch

        for alias in ("/connect", "/setup", "/login", "/endpoint", "/endpoints"):
            with self.subTest(alias=alias):
                workspace = tempfile.mkdtemp(prefix="mantra-dispatch-")
                self.addCleanup(shutil.rmtree, workspace, ignore_errors=True)
                session = make_session(workspace, [])
                buf = io.StringIO()
                with mock.patch("core.console._model_command", return_value=True) as fake:
                    with redirect_stdout(buf):
                        self.assertTrue(dispatch(session, alias))
                    fake.assert_not_called()
                self.assertIn("unknown command", buf.getvalue())

    def test_model_list_shows_the_file(self):
        from core.console import dispatch

        workspace = tempfile.mkdtemp(prefix="mantra-dispatch-")
        self.addCleanup(shutil.rmtree, workspace, ignore_errors=True)
        session = make_session(workspace, [])
        buf = io.StringIO()
        with redirect_stdout(buf):
            dispatch(session, "/model list")
        self.assertIn(str(settings_path()), buf.getvalue())

    def test_model_remove_delegates(self):
        from core.console import dispatch

        workspace = tempfile.mkdtemp(prefix="mantra-dispatch-")
        self.addCleanup(shutil.rmtree, workspace, ignore_errors=True)
        session = make_session(workspace, [])
        with mock.patch("core.console._connect_remove", return_value=None) as fake:
            dispatch(session, "/model remove mybox")
            fake.assert_called_once_with(session, "mybox")


if __name__ == "__main__":
    unittest.main()
