"""Tests for the OKF-adapted memory slice: dedupe/supersede on write,
lifecycle metadata filtering, and request-relevant injection."""

import os
import tempfile
import unittest

os.environ.setdefault("MANTRA_SETTINGS", tempfile.mkdtemp())

from core.agent.knowledge import (  # noqa: E402
    active_entries,
    append_memory,
    parse_entries,
    plan_memory_write,
    read_active_memory_tail,
    relevant_memory,
    rewrite_memory,
)

MEM = """
- 2026-09-01 10:00 | t1 | done: fixed the build pipeline caching bug | status=active
- 2026-09-02 11:00 | t2 | done: reviewed the auth flow design | status=active
- 2026-09-03 12:00 | t3 | error: flaky conpty harness needs a live terminal | status=active
"""


class PlanMemoryWriteTest(unittest.TestCase):
    def test_near_duplicate_is_skipped(self):
        new = "- 2026-09-06 12:00 | t9 | done: reviewed the auth flow design and approved it | status=active"
        action, updated = plan_memory_write(MEM, new)
        self.assertEqual(action, "skip")
        self.assertEqual(updated, MEM)

    def test_same_topic_new_content_supersedes(self):
        new = "- 2026-09-06 12:00 | t9 | done: rewrote the auth flow with PKCE | status=active"
        action, updated = plan_memory_write(MEM, new)
        self.assertEqual(action, "supersede")
        self.assertIn("reviewed the auth flow design | status=active | status=superseded", updated)

    def test_fresh_topic_appends(self):
        new = "- 2026-09-06 12:00 | t9 | done: set up redis caching layer | status=active"
        action, updated = plan_memory_write(MEM, new)
        self.assertEqual(action, "append")

    def test_empty_or_metadata_only_input_appends(self):
        self.assertEqual(plan_memory_write("", "new"), ("append", ""))
        self.assertEqual(plan_memory_write(MEM, "- 2026-09-06 | t9 | done: ok | status=active"), ("append", MEM))


class EntryMetadataTest(unittest.TestCase):
    def test_superseded_and_stale_entries_are_filtered(self):
        text = MEM + "\n- 2026-09-04 12:00 | t4 | done: old plan | status=superseded\n- 2026-09-05 12:00 | t5 | done: old note | status=active | stale=2020-01-01\n"
        parsed = parse_entries(text)
        self.assertEqual(len(parsed), 5)
        active = active_entries(text)
        self.assertEqual(len(active), 3)  # the three base entries
        self.assertNotIn("old plan", "\n".join(e["line"] for e in active))
        self.assertNotIn("old note", "\n".join(e["line"] for e in active))

    def test_read_active_memory_tail_keeps_only_in_force_entries(self):
        text = MEM + "\n- 2026-09-04 12:00 | t4 | done: old | status=superseded\n"
        p = tempfile.mktemp(suffix=".md")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(text)
        self.addCleanup(os.unlink, p)
        body = read_active_memory_tail(p)
        self.assertIn("auth flow", body)
        self.assertNotIn("old | status=superseded", body)


class RelevantMemoryTest(unittest.TestCase):
    def _file(self):
        p = tempfile.mktemp(suffix=".md")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(MEM)
        self.addCleanup(os.unlink, p)
        return p

    def test_returns_matched_older_entries(self):
        p = self._file()
        body = relevant_memory(p, "tell me about the auth flow decision")
        self.assertIn("auth flow", body)
        self.assertNotIn("conpty", body)

    def test_no_match_returns_empty(self):
        p = self._file()
        self.assertEqual(relevant_memory(p, "totally unrelated topic zzz"), "")

    def test_no_query_or_missing_file_returns_empty(self):
        self.assertEqual(relevant_memory(None, "x"), "")
        self.assertEqual(relevant_memory("Z:/none.md", "x"), "")
        self.assertEqual(relevant_memory("Z:/none.md", ""), "")


class WriteIntegrationTest(unittest.TestCase):
    def _file(self):
        p = tempfile.mktemp(suffix=".md")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(MEM)
        self.addCleanup(os.unlink, p)
        return p

    def test_supersede_lands_on_disk(self):
        p = self._file()
        new = "- 2026-09-06 12:00 | t9 | done: rewrote the auth flow with PKCE | status=active"
        action, updated = plan_memory_write(open(p, encoding="utf-8").read(), new)
        self.assertTrue(rewrite_memory(p, updated, new_entry=new))
        with open(p, encoding="utf-8") as fh:
            disk = fh.read()
        self.assertIn("status=superseded", disk)
        self.assertIn("PKCE", disk)

    def test_plain_append_still_works(self):
        p = tempfile.mktemp(suffix=".md")
        self.addCleanup(os.unlink, p)
        self.assertTrue(append_memory(p, "- 2026-09-06 | t9 | done: hello | status=active"))
        with open(p, encoding="utf-8") as fh:
            self.assertIn("hello", fh.read())


if __name__ == "__main__":
    unittest.main()
