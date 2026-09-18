"""Tests for /export: conversation export to Markdown or JSON.

Locks three contracts:

* the default export lands in the workspace as Markdown and the
  export's name is printed;
* the format follows the extension: .json keeps roles and raw content;
* paths outside the allowed directories are refused (same allow-list
  as session saves).
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_TESTS_DIR)
for _path in (os.path.join(_PROJECT_ROOT, "."), _PROJECT_ROOT, _TESTS_DIR):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import core.console as console
from core.console import dispatch
from _helpers import make_session


class _TempSettings:
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        import unittest.mock as mock

        self.env = mock.patch.dict(
            os.environ,
            {"MANTRA_SETTINGS": os.path.join(self.tmp.name, "config.json")},
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(self.tmp.cleanup)


class ExportTest(_TempSettings, unittest.TestCase):
    def setUp(self):
        _TempSettings.setUp(self)
        self.workspace = tempfile.mkdtemp(prefix="mantra-export-")
        self.session = make_session(self.workspace, [])
        # A short conversation: one user turn, one agent turn.
        self.session.context.replace_body([
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "fix the header"},
            {"role": "assistant", "content": "done, header fixed"},
        ])

    def _export(self, arg=""):
        buf = io.StringIO()
        with redirect_stdout(buf):
            dispatch(self.session, f"/export {arg}".strip())
        return buf.getvalue()

    def test_default_export_is_markdown_in_the_workspace(self):
        out = self._export()
        self.assertIn("exported 2 messages", out)
        # The printed path must be the file that was written.
        path = out.strip().split()[-1]
        self.assertTrue(os.path.isfile(path), path)
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("# MANTRA conversation export", text)
        self.assertIn("## You", text)
        self.assertIn("fix the header", text)
        self.assertIn("## Agent", text)
        self.assertIn("done, header fixed", text)
        # Cleanup: the export lands in this test's own scratch workspace.
        os.remove(path)

    def test_json_extension_exports_structured_roles(self):
        target = os.path.join(self.workspace, "chat.json")
        out = self._export(target)
        self.assertIn("exported 2 messages", out)
        with open(target, encoding="utf-8") as handle:
            data = json.load(handle)
        self.assertEqual(data["messages"][0]["role"], "user")
        self.assertEqual(data["messages"][0]["content"], "fix the header")
        self.assertEqual(data["messages"][1]["role"], "assistant")
        self.assertEqual(data["workspace"], self.workspace)

    def test_export_refuses_paths_outside_allowed_dirs(self):
        out = self._export("C:\\definitely-not-allowed\\chat.md")
        self.assertIn("refusing to export", out)
        self.assertFalse(os.path.exists("C:\\definitely-not-allowed\\chat.md"))

    def test_export_with_no_conversation_says_so(self):
        self.session.context.replace_body([
            {"role": "system", "content": "sys"},
        ])
        out = self._export()
        self.assertIn("nothing to export", out)

    def test_export_is_offered_in_help_and_registry(self):
        self.assertIn("/export", console.HELP_TEXT)
        self.assertTrue(any(cmd == "/export" for cmd, _ in console.SLASH_COMMANDS))


if __name__ == "__main__":
    unittest.main()
