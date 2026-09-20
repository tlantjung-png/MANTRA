"""Regression tests for the remediated audit findings.

Each test here fails on the pre-remediation code and passes after it:

* B1 - command output written to the spill log and the background-task log
  is redacted, not just the command header;
* B2 - the fetch pre-check screens literal address forms only, leaving the
  pinned connection as the single authority on name resolution, and that
  pinned path fails closed for a private or unresolvable answer;
* B3 - context eviction survives a non-mapping message in the body;
* B7 - the skill index functions hand back copies on the miss path too, so
  a caller cannot corrupt the cache.
"""

from __future__ import annotations

import os
import re
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_TESTS_DIR)
for _path in (os.path.join(_PROJECT_ROOT, "."), _PROJECT_ROOT, _TESTS_DIR):
    if _path not in sys.path:
        sys.path.insert(0, _path)


# A credential-shaped value the shared redactor recognises: 12 characters
# after "token=" is well past the six-character floor for short values
# that follow a sensitive key name.
_SECRET = "C" * 12
_SECRET_LINE = "token=" + _SECRET


def _workspace(test: unittest.TestCase) -> str:
    path = tempfile.mkdtemp(prefix="mantra-regress-")
    test.addCleanup(shutil.rmtree, path, True)
    return path


class CommandLogRedactionTest(unittest.TestCase):
    """B1: every byte written to a log file goes through the redactor."""

    def _sandbox(self):
        from core.sandbox import LocalSandbox

        return LocalSandbox(_workspace(self))

    def test_full_output_log_redacts_the_payload(self):
        from core.tools.commands import RunCommandTool

        sandbox = self._sandbox()
        script = f"print({_SECRET_LINE!r}); print('x' * 20000)"
        command = f'"{sys.executable}" -c "{script}"'
        out = RunCommandTool().execute(sandbox, command, timeout=60.0)

        match = re.search(r"log: (.+)$", out, re.MULTILINE)
        self.assertIsNotNone(match, f"no spill log written: {out[:400]}")
        with open(match.group(1).strip(), "r", encoding="utf-8") as handle:
            log_text = handle.read()

        # The payload reached the log (so this is not a false pass from an
        # empty file), but not in its original form.
        self.assertGreater(log_text.count("x"), 1000, "payload missing from the log")
        self.assertIn("[REDACTED]", log_text)
        self.assertNotIn(_SECRET, log_text)

    def test_background_task_log_redacts_the_payload(self):
        from core.tools.commands import _TASKS, _TASKS_LOCK, RunCommandTool

        sandbox = self._sandbox()
        command = f'"{sys.executable}" -c "print({_SECRET_LINE!r})"'
        out = RunCommandTool().execute(sandbox, command, timeout=30.0, background=True)

        match = re.search(r"task_id=(\S+)", out)
        self.assertIsNotNone(match, f"no task id in: {out[:400]}")
        task_id = match.group(1)
        log_path = os.path.join(sandbox.root, ".mantra", "logs", f"task_{task_id}.log")
        self.assertTrue(os.path.exists(log_path), f"task log missing at {log_path}")

        # The pump thread writes asynchronously; wait for the task to be
        # marked done so the whole payload is on disk before reading.
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            with _TASKS_LOCK:
                task = dict(_TASKS.get(task_id) or {})
            if task.get("done"):
                break
            time.sleep(0.05)
        self.assertTrue(task.get("done"), "background task never finished")

        with open(log_path, "r", encoding="utf-8", errors="replace") as handle:
            log_text = handle.read()
        self.assertIn("[REDACTED]", log_text)
        self.assertNotIn(_SECRET, log_text)


class HostResolutionAuthorityTest(unittest.TestCase):
    """B2: one gate decides name resolution, and it fails closed."""

    def test_literal_private_forms_are_blocked_without_resolution(self):
        from core.tools import web

        for url in ("http://127.0.0.1/", "http://10.0.0.1/", "http://[::1]/"):
            self.assertIsNotNone(web._check_url_allowed(url), url)

    def test_pre_check_does_not_resolve_names(self):
        from core.tools import web

        with mock.patch.object(web, "_resolve_limited") as resolver:
            self.assertIsNone(web._check_url_allowed("http://example.test/"))
        resolver.assert_not_called()
        # A literal is still refused, with resolution never consulted.
        self.assertIsNotNone(web._check_url_allowed("http://127.0.0.1/"))

    def test_pinned_connection_refuses_a_private_answer(self):
        from core.tools import web

        private = [(2, 1, 6, "", ("192.168.1.10", 0))]
        with mock.patch.object(web, "_resolve_limited", return_value=private):
            self.assertIsNone(web._resolve_and_pin("rebind.example"))

    def test_pinned_connection_refuses_an_unresolvable_host(self):
        from core.tools import web

        with mock.patch.object(web, "_resolve_limited", return_value=None):
            self.assertIsNone(web._resolve_and_pin("nowhere.invalid"))

    def test_pinned_connection_returns_a_public_answer(self):
        from core.tools import web

        public = [(2, 1, 6, "", ("93.184.216.34", 0))]
        with mock.patch.object(web, "_resolve_limited", return_value=public):
            self.assertEqual(web._resolve_and_pin("example.test"), "93.184.216.34")


class ContextEvictionRobustnessTest(unittest.TestCase):
    """B3: a non-mapping message must not break eviction."""

    def _manager(self, **kwargs):
        from core.agent.context import ContextManager

        manager = ContextManager(**kwargs)
        manager.append({"role": "user", "content": "first"})
        manager.append({"role": "assistant", "content": "second"})
        manager.append({"role": "user", "content": "third"})
        manager.append({"role": "assistant", "content": "fourth"})
        return manager

    def test_truncation_ignores_a_non_mapping_last_message(self):
        manager = self._manager(max_messages=4, max_chars=1_000_000)
        manager.messages.append("a foreign body")
        # Over the message limit, so the eviction loop runs with the
        # foreign element as the newest entry: it must not raise.
        manager._truncate()
        self.assertLessEqual(len(manager.messages), 4)

    def test_dropping_a_turn_ignores_non_mapping_entries(self):
        manager = self._manager(max_messages=4, max_chars=1_000_000)
        manager.messages.append({"role": "tool", "content": None})
        manager.messages.append(12345)
        # Returns whether it dropped anything; the point is that it returns.
        self.assertIsInstance(manager._drop_oldest_turn(), bool)

    def test_replace_body_with_raw_values_does_not_raise(self):
        manager = self._manager(max_messages=4, max_chars=1_000_000)
        manager.replace_body(
            [
                {"role": "user", "content": "ok"},
                {"role": "assistant", "content": "ok"},
                {"role": "user", "content": "ok"},
                "raw",
            ]
        )
        manager.enforce_budget()


_FIXTURE_SKILL = """---
name: {name}
description: does {name}
version: 1.0.0
user-invocable: true
---

# {title}
"""

_FIXTURE_INDEX = """# Skill Catalog

| Skill | Function | Chained with |
|---|---|---|
| `tdd` | Drive behavior-first tests. | `debug`. |
| `debug` | Reproduce and fix a failure. | `tdd`. |
"""

_FIXTURE_BUNDLES = """# Skill Bundles

| Bundle | Skills in order | Use |
|---|---|---|
| fix-bug | `debug`, `tdd` | Reproduce, fix, verify. |
"""


class SkillCacheAliasingTest(unittest.TestCase):
    """B7: the miss path must return a copy, like the hit path."""

    def setUp(self):
        import core.agent.skills as skills

        self.skills = skills
        self.tmp = tempfile.mkdtemp(prefix="mantra-skills-regress-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.addCleanup(os.environ.pop, skills._OVERRIDE_ENV, None)
        root = os.path.join(self.tmp, "skills")
        os.environ[skills._OVERRIDE_ENV] = root
        for name in ("tdd", "debug"):
            path = os.path.join(root, name, "SKILL.md")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(_FIXTURE_SKILL.format(name=name, title=name.upper()))
        with open(os.path.join(root, "INDEX.md"), "w", encoding="utf-8", newline="\n") as handle:
            handle.write(_FIXTURE_INDEX)
        with open(os.path.join(root, "BUNDLES.md"), "w", encoding="utf-8", newline="\n") as handle:
            handle.write(_FIXTURE_BUNDLES)

    def _miss_then_hit(self, load):
        self.skills.invalidate_cache()
        first = load()  # cache miss: this build must not be the cache itself
        self.assertTrue(first, "fixture produced no index entries")
        return first

    def test_load_all_returns_a_copy_on_a_miss(self):
        first = self._miss_then_hit(self.skills.load_all)
        first["injected"] = None
        self.assertNotIn("injected", self.skills.load_all())

    def test_load_bundles_returns_a_copy_on_a_miss(self):
        first = self._miss_then_hit(self.skills.load_bundles)
        first["injected"] = ["nope"]
        self.assertNotIn("injected", self.skills.load_bundles())

    def test_routing_table_returns_a_copy_on_a_miss(self):
        first = self._miss_then_hit(self.skills.routing_table)
        first["injected"] = {"function": "nope"}
        self.assertNotIn("injected", self.skills.routing_table())


if __name__ == "__main__":
    unittest.main()
