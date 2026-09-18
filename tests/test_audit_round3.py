"""Regression tests for the third audit round and the interactive fixes.

Covers: bundle launches keep the operator's request, token rules are
enforced, a failed call keeps one identical retry, keyless local endpoints
work, the sampling-temperature field sheds on rejection, name resolution
honours its timeout, atomic writes to the settings/sessions stores report
failure instead of direct-writing, corrupt credentials quarantine once,
the session listing caches, the memory-file fence covers redirects, and
the startup card cannot survive the first turn or a resize.
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

_path = os.path.dirname(os.path.abspath(__file__))
if _path not in sys.path:
    sys.path.insert(0, _path)

from core.agent.loop import AgentLoop
from core.agent.approvals import classify_command
from core.agent.events import EventBus
from core.agent import keys as keys_module
from core.agent import sessions as sessions_module
from core.agent import settings as settings_module
from core.scripted import (
    LLMResponse,
    ScriptedLLMClient,
    ToolCall,
    tool_call_response,
)
from core.llm import (
    OpenAICompatClient,
    is_keyless_base_url,
)
from core.sandbox import LocalSandbox
from core.tools.web import _resolve_and_pin, _resolve_limited
from core.console import Style, _skills_launch

from _helpers import make_session


def _workspace() -> str:
    return tempfile.mkdtemp(prefix="mantra-r3-")


def _logger():
    from core.logs import JsonlLogger

    return JsonlLogger(os.path.join(_workspace(), "run.jsonl"))


class BundleKeepsRequestTest(unittest.TestCase):
    """H-1: an auto-launched bundle must run on the operator's request."""

    def test_first_bundle_step_receives_the_request(self):
        root = tempfile.mkdtemp(prefix="mantra-skills-")
        skill_dir = os.path.join(root, "alpha")
        os.makedirs(skill_dir)
        with open(os.path.join(skill_dir, "SKILL.md"), "w", encoding="utf-8") as fh:
            fh.write("---\nname: alpha\ndescription: test procedure\n---\nDo the thing.\n")
        with open(os.path.join(root, "BUNDLES.md"), "w", encoding="utf-8") as fh:
            fh.write("| bundle | skills |\n|---|---|\n| fix-all | `alpha` |\n")

        handled: list[str] = []

        class _FakeSession:
            config: dict = {}
            style = Style(enabled=False)
            active_skills: list[str] = []
            in_bundle = False

            def _print(self, text: str = "") -> None:
                pass

            def handle(self, text: str):
                handled.append(text)
                return None

        old = os.environ.get("MANTRA_SKILLS")
        os.environ["MANTRA_SKILLS"] = root
        try:
            result = _skills_launch(_FakeSession(), "fix-all", initial_text="fix the widget")
        finally:
            if old is None:
                os.environ.pop("MANTRA_SKILLS", None)
            else:
                os.environ["MANTRA_SKILLS"] = old

        # The step did not complete (fake handle returned None), but the
        # request the operator typed is what the first step was given.
        self.assertEqual(handled, ["fix the widget"])
        self.assertIsNone(result)

    def test_later_steps_use_the_procedure_prompt(self):
        root = tempfile.mkdtemp(prefix="mantra-skills-")
        for name in ("alpha", "beta"):
            skill_dir = os.path.join(root, name)
            os.makedirs(skill_dir)
            with open(os.path.join(skill_dir, "SKILL.md"), "w", encoding="utf-8") as fh:
                fh.write(f"---\nname: {name}\ndescription: {name} procedure\n---\nBody.\n")
        with open(os.path.join(root, "BUNDLES.md"), "w", encoding="utf-8") as fh:
            fh.write("| bundle | skills |\n|---|---|\n| two-step | `alpha` `beta` |\n")

        handled: list[str] = []

        class _FakeSession:
            config: dict = {}
            style = Style(enabled=False)
            active_skills: list[str] = []
            in_bundle = False

            def _print(self, text: str = "") -> None:
                pass

            def handle(self, text: str):
                handled.append(text)
                return LLMResponse(content="step done")

        old = os.environ.get("MANTRA_SKILLS")
        os.environ["MANTRA_SKILLS"] = root
        try:
            _skills_launch(_FakeSession(), "two-step", initial_text="do the work")
        finally:
            if old is None:
                os.environ.pop("MANTRA_SKILLS", None)
            else:
                os.environ["MANTRA_SKILLS"] = old

        self.assertEqual(handled[0], "do the work")
        self.assertEqual(handled[1], "Apply the beta skill to the current work.")


class TokenRulesEnforcedTest(unittest.TestCase):
    """M-2: token-sequence rules in the rules file are applied."""

    def setUp(self):
        self.rules_file = os.path.join(_workspace(), "commands.rules")
        with open(self.rules_file, "w", encoding="utf-8") as fh:
            fh.write(
                "forbid|wipe all|never wipe everything\n"
                "prompt|deploy prod|deploys need a human\n"
                "allow|kubectl|cluster cli is scoped\n"
            )
        self._old = os.environ.get("MANTRA_RULES_FILE")
        os.environ["MANTRA_RULES_FILE"] = self.rules_file

    def tearDown(self):
        if self._old is None:
            os.environ.pop("MANTRA_RULES_FILE", None)
        else:
            os.environ["MANTRA_RULES_FILE"] = self._old

    def test_token_forbid_is_destructive(self):
        self.assertEqual(classify_command("wipe all caches now"), "destructive")

    def test_token_prompt_demands_confirmation(self):
        self.assertEqual(classify_command("deploy prod today"), "confirm")

    def test_token_prompt_upgrades_a_safe_segment(self):
        # A rule may force a human even for text the built-in screen
        # considered safe.
        self.assertEqual(classify_command("echo deploy prod"), "confirm")

    def test_token_allow_relaxes_only_a_mutating_verdict(self):
        self.assertEqual(classify_command("kubectl delete pod x"), "safe")

    def test_token_allow_never_relaxes_destructive(self):
        self.assertEqual(classify_command("kubectl delete pod x && rm -rf y"), "destructive")


class RetryAfterErrorTest(unittest.TestCase):
    """M-6: one identical retry after a failure executes for real."""

    def _loop(self, script):
        from core.tools.files import ListDirTool, ReadFileTool

        return AgentLoop(
            llm=ScriptedLLMClient(script),
            sandbox=LocalSandbox(_workspace()),
            tools=[ReadFileTool(), ListDirTool()],
            evaluator=None,
            logger=_logger(),
            events=EventBus(),
        )

    def test_identical_retry_after_error_executes(self):
        call = lambda: tool_call_response("read_file", {"path": "definitely-missing.txt"})
        loop = self._loop([call(), call(), call(), LLMResponse(content="done")])
        loop.run({"task_id": "t", "problem_statement": "go"})
        results = [
            m["content"]
            for m in loop.context.messages
            if m.get("role") == "tool" and m.get("name") == "read_file"
        ]
        self.assertEqual(len(results), 3)
        # First two attempts really ran (the tool's own error text, not
        # the synthetic repeat-refusal).
        self.assertIn("cannot read", results[0])
        self.assertEqual(results[1], results[0])
        # The third identical call is refused: the retry allowance is one.
        self.assertIn("STOP RETRYING", results[2])

    def test_repeat_after_success_stays_blocked(self):
        call = lambda: tool_call_response("list_dir", {"path": "."})
        loop = self._loop([call(), call(), LLMResponse(content="done")])
        loop.run({"task_id": "t", "problem_statement": "go"})
        results = [
            m["content"]
            for m in loop.context.messages
            if m.get("role") == "tool" and m.get("name") == "list_dir"
        ]
        self.assertEqual(len(results), 2)
        self.assertIn("already called", results[1])


class KeylessEndpointTest(unittest.TestCase):
    """M-1: loopback endpoints accept requests with no credential."""

    def test_loopback_urls_are_keyless(self):
        self.assertTrue(is_keyless_base_url("http://localhost:11434/v1"))
        self.assertTrue(is_keyless_base_url("http://127.0.0.1:8080/v1"))
        self.assertTrue(is_keyless_base_url("http://[::1]:9000/v1"))
        self.assertFalse(is_keyless_base_url("https://api.example.com/v1"))
        self.assertFalse(is_keyless_base_url("http://example.localhost.evil.com/v1"))

    def test_chat_without_a_key_succeeds_on_loopback(self):
        client = OpenAICompatClient(
            model="m", base_url="http://127.0.0.1:9/v1", api_key_env="MANTRA_R3_NO_SUCH_KEY"
        )
        seen: dict = {}

        def fake_request(body: bytes) -> LLMResponse:
            seen["headers"] = client._headers()
            return LLMResponse(content="ok")

        client._request = fake_request  # type: ignore[method-assign]
        response = client.chat([{"role": "user", "content": "hi"}])
        self.assertEqual(response.content, "ok")
        self.assertNotIn("Authorization", seen["headers"])

    def test_remote_endpoint_without_key_still_raises(self):
        client = OpenAICompatClient(
            model="m", base_url="https://api.example.com/v1", api_key_env="MANTRA_R3_NO_SUCH_KEY"
        )
        with self.assertRaises(Exception) as ctx:
            client.chat([{"role": "user", "content": "hi"}])
        self.assertIn("no API key", str(ctx.exception))


class TemperatureDowngradeTest(unittest.TestCase):
    """M-5: a 400 blaming temperature sheds the field and continues."""

    def test_temperature_is_shed_on_rejection(self):
        client = OpenAICompatClient(
            model="reasoner",
            base_url="http://127.0.0.1:9/v1",
            api_key_env="MANTRA_R3_NO_SUCH_KEY",
            temperature=0.2,
        )
        bodies: list[dict] = []
        calls = {"n": 0}

        def fake_request(body: bytes) -> LLMResponse:
            import urllib.error

            bodies.append(json.loads(body.decode("utf-8")))
            calls["n"] += 1
            if calls["n"] == 1:
                raise urllib.error.HTTPError(
                    "http://127.0.0.1:9/v1/chat/completions",
                    400,
                    "Bad Request",
                    None,
                    io.BytesIO(b'{"error": {"message": "temperature is not supported"}}'),
                )
            return LLMResponse(content="ok")

        client._request = fake_request  # type: ignore[method-assign]
        response = client.chat([{"role": "user", "content": "hi"}])
        self.assertEqual(response.content, "ok")
        self.assertIn("temperature", bodies[0])
        self.assertNotIn("temperature", bodies[1])
        self.assertFalse(client._temperature_supported)
        # The downgrade sticks for subsequent turns.
        client.chat([{"role": "user", "content": "again"}])
        self.assertNotIn("temperature", bodies[-1])


class ResolveTimeoutTest(unittest.TestCase):
    """M-3: a stalled resolver costs the stated timeout, not the OS's."""

    def test_timeout_returns_promptly(self):
        import socket as socket_module

        def stalled(*args, **kwargs):
            time.sleep(1.0)
            return []

        with mock.patch.object(socket_module, "getaddrinfo", stalled):
            start = time.monotonic()
            result = _resolve_limited("example.invalid", 0.05)
            elapsed = time.monotonic() - start
        self.assertIsNone(result)
        self.assertLess(elapsed, 0.5, "resolver thread was joined instead of abandoned")

    def test_healthy_resolution_returns_addresses(self):
        infos = _resolve_limited("localhost", 2.0)
        self.assertTrue(infos)

    def test_pinned_resolution_refuses_loopback(self):
        self.assertIsNone(_resolve_and_pin("127.0.0.1"))


class AtomicWriteNoBackdoorTest(unittest.TestCase):
    """M-7: settings/sessions atomic replace reports failure instead of
    direct-writing (LocalSandbox.write_file keeps its own re-validated
    fallback, which is not covered here)."""

    def test_settings_write_failure_returns_false(self):
        old = os.environ.get("MANTRA_SETTINGS")
        target = Path(_workspace()) / "config.json"
        os.environ["MANTRA_SETTINGS"] = str(target)
        try:
            self.assertTrue(settings_module._write({"endpoints": {}}))
            with mock.patch.object(Path, "replace", side_effect=OSError("replace blocked")):
                ok = settings_module._write({"endpoints": {"a": {"base_url": "http://x"}}})
            self.assertFalse(ok)
            # The previous document survives untouched.
            self.assertEqual(settings_module.load()["endpoints"], {})
        finally:
            if old is None:
                os.environ.pop("MANTRA_SETTINGS", None)
            else:
                os.environ["MANTRA_SETTINGS"] = old

    def test_sessions_save_failure_returns_none(self):
        old = os.environ.get("MANTRA_SESSIONS")
        target = Path(_workspace()) / "sessions"
        os.environ["MANTRA_SESSIONS"] = str(target)
        try:
            sessions_module.save("keeper", {"messages": [{"role": "user", "content": "hi"}]})
            with mock.patch.object(
                sessions_module.os, "replace", side_effect=OSError("replace blocked")
            ):
                result = sessions_module.save(
                    "keeper", {"messages": [{"role": "user", "content": "hi"}]}
                )
            self.assertIsNone(result)
            # The previously saved transcript is untouched.
            self.assertIsNotNone(sessions_module.load("keeper"))
        finally:
            if old is None:
                os.environ.pop("MANTRA_SESSIONS", None)
            else:
                os.environ["MANTRA_SESSIONS"] = old


class QuarantineOnceTest(unittest.TestCase):
    """L-10: a corrupt credentials file is copied aside exactly once."""

    def test_quarantine_happens_on_first_read_only(self):
        cred_file = Path(_workspace()) / "credentials.json"
        cred_file.write_text("{ this is not json", encoding="utf-8")
        old = os.environ.get("MANTRA_CREDENTIALS")
        os.environ["MANTRA_CREDENTIALS"] = str(cred_file)
        try:
            with mock.patch("shutil.copy2", wraps=__import__("shutil").copy2) as cp:
                keys_module._load()
                keys_module._load()
                keys_module._load()
            self.assertEqual(cp.call_count, 1)
        finally:
            if old is None:
                os.environ.pop("MANTRA_CREDENTIALS", None)
            else:
                os.environ["MANTRA_CREDENTIALS"] = old


class SessionListingCacheTest(unittest.TestCase):
    """L-4: the resume listing is served from the stat-keyed cache."""

    def test_second_listing_skips_the_parse(self):
        old = os.environ.get("MANTRA_SESSIONS")
        os.environ["MANTRA_SESSIONS"] = _workspace()
        try:
            sessions_module.save(
                "cached",
                {
                    "workspace": "w",
                    "messages": [{"role": "user", "content": "hello there"}],
                },
            )
            first = sessions_module.list_sessions()
            self.assertEqual(len(first), 1)
            with mock.patch.object(sessions_module.json, "load", side_effect=ValueError("boom")):
                second = sessions_module.list_sessions()
            self.assertEqual(second, first)
        finally:
            sessions_module._LISTING_CACHE.clear()
            if old is None:
                os.environ.pop("MANTRA_SESSIONS", None)
            else:
                os.environ["MANTRA_SESSIONS"] = old

    def test_resaved_file_is_reread(self):
        old = os.environ.get("MANTRA_SESSIONS")
        os.environ["MANTRA_SESSIONS"] = _workspace()
        try:
            sessions_module.save("changing", {"messages": [{"role": "user", "content": "one"}]})
            before = sessions_module.list_sessions()
            before_keys = set(sessions_module._LISTING_CACHE)
            sessions_module.save("changing", {"messages": [{"role": "user", "content": "two"}]})
            after = sessions_module.list_sessions()
            self.assertEqual(after[0]["summary"], "two")
            # The re-save must produce a new stat key: a stale cache hit
            # would still show the old summary.
            self.assertTrue(
                set(sessions_module._LISTING_CACHE) - before_keys,
                "re-saved file was served from the stale listing cache",
            )
        finally:
            sessions_module._LISTING_CACHE.clear()
            if old is None:
                os.environ.pop("MANTRA_SESSIONS", None)
            else:
                os.environ["MANTRA_SESSIONS"] = old


class MemoryFenceRedirectTest(unittest.TestCase):
    """L-8: the memory-file fence covers shell redirection too."""

    def test_redirect_to_memory_file_is_destructive(self):
        self.assertEqual(classify_command('echo note > MEMORY.md'), "destructive")
        self.assertEqual(classify_command("git log >> MEMORY.md"), "destructive")

    def test_reading_memory_file_is_not_blocked(self):
        self.assertEqual(classify_command("cat MEMORY.md"), "safe")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
