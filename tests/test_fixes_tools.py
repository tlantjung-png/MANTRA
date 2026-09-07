"""Regression tests for the tool-execution zone defect remediation round.

Each test pins one defect from audit-tools.md section B so the fix cannot
silently regress:

- D1  web_fetch rejects redirect targets whose scheme is not http/https
- D2  kill_shell by task_id kills the whole process tree and finalizes
- D5  edit_file applies the harness/device path blocklist before reading
- D6  diffparse keeps added "++i" and removed "--x" content lines
- D17 list_dir refuses ".." and absolute paths on root-less sandboxes
- D18 read_file rejects float offset/limit instead of truncating
- D19 the traversal screen catches cd.. and quoted wrapper payloads
- D22 CommandEvaluator rejects timeouts outside (0, 600]
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import unittest
from urllib.error import HTTPError


class D1RedirectSchemeTest(unittest.TestCase):
    """A redirect to file://, ftp:// or data:// is refused before fetching."""

    def _handler(self):
        from core.tools.web import _PinningHandler

        return _PinningHandler()

    def test_file_ftp_data_redirects_raise_http_error(self):
        handler = self._handler()
        for bad in (
            "file:///C:/secret.txt",
            "ftp://169.254.169.254/",
            "data:text/plain,hello",
        ):
            with self.assertRaises(HTTPError) as ctx:
                handler.redirect_request(None, None, 302, "Found", {}, bad)
            self.assertIn("blocked", str(ctx.exception.reason))

    def test_http_redirect_still_routes_through_the_parent(self):
        handler = self._handler()

        class _Req:
            def get_method(self):
                return "GET"
            headers = {}
            type = "https"
            host = "example.com"
            origin_req_host = "example.com"
            full_url = "https://example.com/old"

        # A plain http redirect must reach the parent's validation, not
        # be refused by the scheme gate.
        req = _Req()
        result = handler.redirect_request(req, None, 301, "Moved", {}, "https://example.com/new")
        self.assertIsNotNone(result)
        self.assertEqual(result.full_url, "https://example.com/new")


class D2KillTreeTest(unittest.TestCase):
    """kill_shell by task_id kills the whole tree and finalizes the task."""

    def test_task_id_kill_terminates_the_process_and_marks_done(self):
        from core.tools import commands as command_module
        from core.tools.commands import KillShellTool, _TASKS, _TASKS_LOCK

        ws = tempfile.mkdtemp(prefix="mantra-fix-d2-")
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            cwd=ws,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **command_module._POPEN_GROUP_KWARGS,
        )
        tid = "tsk_d2_test"
        log = os.path.join(ws, "task.log")
        with _TASKS_LOCK:
            _TASKS[tid] = {
                "task_id": tid,
                "command": "sleep",
                "log_path": log,
                "pid": proc.pid,
                "process": proc,
                "start_time": time.monotonic(),
                "timeout": 30,
                "done": False,
            }
        try:
            out = KillShellTool().execute(__import__("core.sandbox", fromlist=["LocalSandbox"]).LocalSandbox(ws), task_id=tid)
            self.assertIn("OK: killed task", out)
            self.assertIsNotNone(proc.poll(), "direct child survived the kill")
            with _TASKS_LOCK:
                entry = _TASKS[tid]
            self.assertTrue(entry["done"])
            self.assertIn("interrupted by operator", entry.get("stderr") or "")
        finally:
            if proc.poll() is None:
                command_module._kill_task_tree(proc)
            with _TASKS_LOCK:
                _TASKS.pop(tid, None)


class D5EditBlocklistTest(unittest.TestCase):
    """edit_file refuses harness/device paths before touching them."""

    def test_edit_refuses_blocked_paths(self):
        from core.sandbox import LocalSandbox
        from core.tools.files import EditFileTool

        ws = tempfile.mkdtemp(prefix="mantra-fix-d5-")
        sandbox = LocalSandbox(ws)
        sandbox.setup({})
        editor = EditFileTool()
        out = editor.execute(sandbox, "\\\\?\\C:\\x", "a", "b")
        self.assertIn("device namespace", out)
        out = editor.execute(sandbox, "CON", "a", "b")
        self.assertIn("device names are blocked", out)
        out = editor.execute(sandbox, "/dev/zero", "a", "b")
        self.assertIn("refusing to edit device path", out)

    def test_edit_of_a_normal_file_still_reaches_the_ledger_gate(self):
        from core.sandbox import LocalSandbox
        from core.tools.files import EditFileTool
        from core.tools.ledger import EditLedger

        ws = tempfile.mkdtemp(prefix="mantra-fix-d5b-")
        sandbox = LocalSandbox(ws)
        sandbox.setup({})
        sandbox.write_file("ok.py", "x = 1\n")
        editor = EditFileTool()
        editor.ledger = EditLedger()
        out = editor.execute(sandbox, "ok.py", "1", "2")
        self.assertIn("read_file before editing", out)


class D6DiffparseContentTest(unittest.TestCase):
    """Added lines starting with ++ and removed lines starting with -- survive."""

    def test_increment_operator_lines_are_kept(self):
        from core.diffparse import parse_diff

        diff = (
            "diff --git a/x.py b/x.py\n"
            "--- a/x.py\n"
            "+++ b/x.py\n"
            "@@ -1,2 +1,2 @@\n"
            "---x\n"
            "+++i;\n"
        )
        files = parse_diff(diff)
        self.assertEqual(len(files), 1)
        hunk = files[0].hunks[0]
        self.assertEqual([l.kind for l in hunk.lines], ["del", "add"])
        self.assertEqual(hunk.lines[0].text, "--x")
        self.assertEqual(hunk.lines[1].text, "++i;")

    def test_spaced_headers_are_still_skipped(self):
        from core.diffparse import parse_diff

        diff = (
            "diff --git a/x.py b/x.py\n"
            "--- a/x.py\n"
            "+++ b/x.py\n"
            "@@ -1,1 +1,1 @@\n"
            " old\n"
        )
        files = parse_diff(diff)
        self.assertEqual(len(files), 1)
        self.assertEqual([l.kind for l in files[0].hunks[0].lines], ["ctx"])


class D17ListDirGuardTest(unittest.TestCase):
    """Root-less list_dir refuses ".." and absolute paths before the shell."""

    class _Rootless:
        root = None

        def exec(self, command, timeout=120.0):
            raise AssertionError(f"shell must not run for {command!r}")

    def test_parent_and_absolute_paths_are_refused(self):
        from core.tools.files import ListDirTool

        tool = ListDirTool()
        for bad in ("..", "../etc", "C:\\Windows", "/etc", "sub/.."):
            out = tool.execute(self._Rootless(), bad)
            self.assertTrue(out.startswith("ERROR"), msg=bad)
            self.assertIn("inside the workspace", out)

    def test_a_plain_name_still_uses_the_shell_fallback(self):
        from core.tools.files import ListDirTool

        class _RootlessExec:
            root = None

            def __init__(self):
                self.calls = []

            def exec(self, command, timeout=120.0):
                self.calls.append(command)
                from core.types import ExecResult

                return ExecResult(exit_code=0, stdout="a.txt\n", stderr="")

        fake = _RootlessExec()
        out = ListDirTool().execute(fake, "src")
        self.assertEqual(out, "a.txt\n")
        self.assertEqual(len(fake.calls), 1)


class D18FloatWindowTest(unittest.TestCase):
    """Float offset/limit are rejected, not silently truncated."""

    def test_float_offset_and_limit_are_rejected(self):
        from core.sandbox import LocalSandbox
        from core.tools.files import ReadFileTool

        ws = tempfile.mkdtemp(prefix="mantra-fix-d18-")
        sandbox = LocalSandbox(ws)
        sandbox.setup({})
        sandbox.write_file("a.txt", "line1\nline2\n")
        tool = ReadFileTool()
        out = tool.execute(sandbox, "a.txt", offset=1.5)
        self.assertIn("offset must be an integer", out)
        out = tool.execute(sandbox, "a.txt", limit=2.5)
        self.assertIn("limit must be an integer", out)

    def test_integral_ints_still_work(self):
        from core.sandbox import LocalSandbox
        from core.tools.files import ReadFileTool

        ws = tempfile.mkdtemp(prefix="mantra-fix-d18b-")
        sandbox = LocalSandbox(ws)
        sandbox.setup({})
        sandbox.write_file("a.txt", "l1\nl2\nl3\n")
        tool = ReadFileTool()
        out = tool.execute(sandbox, "a.txt", offset=1, limit=2)
        self.assertIn("l2", out)
        self.assertIn("l3", out)


class D19TraversalScreenTest(unittest.TestCase):
    """The traversal screen catches cd.. and quoted wrapper payloads."""

    @staticmethod
    def _traversal(cmd: str) -> bool:
        from core.sandbox import _contains_traversal

        return _contains_traversal(cmd)

    def test_cd_dot_dot_without_separator_is_blocked(self):
        self.assertTrue(self._traversal("cd..\\..\\"))
        self.assertTrue(self._traversal("cd.."))
        self.assertTrue(self._traversal("cmd /c cd.."))

    def test_quoted_wrapper_payloads_are_scanned(self):
        self.assertTrue(self._traversal('cmd /c "cd .. && type C:\\Windows\\win.ini"'))
        self.assertTrue(self._traversal('powershell -c "Get-Content C:\\Windows\\win.ini"'))
        self.assertTrue(self._traversal('pwsh -Command "cd .."'))

    def test_plain_quoted_text_stays_allowed(self):
        self.assertFalse(self._traversal('echo "a .. b"'))
        self.assertFalse(self._traversal('git commit -m "fixed cd.. bug"'))
        self.assertFalse(self._traversal('git diff --no-color -- flappy.py | findstr /C:"on_flap" /C:"^[-+]"'))


class D24ByteBudgetResumeOffsetTest(unittest.TestCase):
    """After a byte-budget cut, the suggested resume offset counts only
    the lines actually shown, so re-reading from it continues cleanly."""

    def test_resume_offset_advances_past_the_byte_cut(self):
        from core.sandbox import LocalSandbox
        from core.tools.files import ReadFileTool

        ws = tempfile.mkdtemp(prefix="mantra-fix-d24-")
        sandbox = LocalSandbox(ws)
        sandbox.setup({})
        # 600 lines x ~500 bytes each: ~300KB total, under the sandbox
        # write cap but far over the 128KB byte budget, so the window
        # read must cut mid-window.
        content = "\n".join(f"line{i:04d} " + "x" * 480 for i in range(600))
        sandbox.write_file("big.txt", content)
        tool = ReadFileTool()
        out = tool.execute(sandbox, "big.txt", offset=0, limit=2000)
        self.assertIn("byte cap 128KB hit", out)
        # The header must suggest a resume offset equal to the number of
        # lines actually shown, not the requested window size.
        shown = int(out.split("showing ", 1)[1].split(" ", 1)[0])
        self.assertLess(shown, 600)
        self.assertIn(f"retry with offset={shown}", out)
        # Re-reading from that offset returns the next lines, not a repeat
        # of the truncated tail and not a gap.
        resumed = tool.execute(sandbox, "big.txt", offset=shown, limit=10)
        self.assertIn("line%04d" % shown, resumed)
        self.assertIn("line%04d" % min(shown + 9, 599), resumed)

    def test_byte_cut_recounts_lines_not_the_raw_window(self):
        from core.sandbox import LocalSandbox
        from core.tools.files import ReadFileTool

        ws = tempfile.mkdtemp(prefix="mantra-fix-d24b-")
        sandbox = LocalSandbox(ws)
        sandbox.setup({})
        # 200 lines x ~1900 chars: under the 2000-char per-line clamp but
        # ~380KB total, so the byte budget cuts mid-line. The resume
        # offset must be the *recounted* shown line count, not the
        # requested limit.
        content = "\n".join(f"line{i:03d} " + "x" * 1890 for i in range(200))
        sandbox.write_file("mid_cut.txt", content)
        tool = ReadFileTool()
        out = tool.execute(sandbox, "mid_cut.txt", offset=0, limit=2000)
        self.assertIn("byte cap 128KB hit", out)
        shown = int(out.split("showing ", 1)[1].split(" ", 1)[0])
        self.assertLess(shown, 200)
        self.assertIn(f"retry with offset={shown}", out)
        # The recounted offset must match the lines actually present in
        # the returned body (a partial last line is dropped, not counted).
        body = out.split("\n", 1)[1]
        self.assertEqual(shown, len(body.splitlines()))
        # Re-reading from the suggested offset continues without a gap.
        resumed = tool.execute(sandbox, "mid_cut.txt", offset=shown, limit=5)
        self.assertIn("line%03d" % shown, resumed)


class D22EvaluatorTimeoutTest(unittest.TestCase):
    """CommandEvaluator surfaces bad timeouts at construction."""

    def test_out_of_range_and_non_numeric_timeouts_raise(self):
        from core.evaluators import CommandEvaluator

        for bad in (0, -1, 601, 700, "abc", None):
            with self.assertRaises(ValueError):
                CommandEvaluator(test_cmd="echo hi", timeout=bad)

    def test_valid_timeouts_are_accepted(self):
        from core.evaluators import CommandEvaluator

        ev = CommandEvaluator(test_cmd="echo hi", timeout=60)
        self.assertEqual(ev.timeout, 60.0)
        self.assertEqual(CommandEvaluator(test_cmd="echo hi").timeout, 600.0)


if __name__ == "__main__":
    unittest.main()
