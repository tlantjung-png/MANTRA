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

Plus BulkReadSkippedCountTest, which locks the bulk-read header arithmetic:
cap-skipped files are counted as matched minus shown minus errors.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from urllib.error import HTTPError


class D1RedirectSchemeTest(unittest.TestCase):
    """A redirect to file://, ftp:// or data:// is refused before fetching."""

    def _handler(self):
        from core.tools.web import _PinningHandler as _Base

        class _PinningHandler(_Base):
            # Network-free instance: the redirect checks below never open
            # a socket, and any accidental http_open fails loudly here.
            def http_open(self, req):
                raise AssertionError("no network in redirect tests")

            def https_open(self, req):
                raise AssertionError("no network in redirect tests")

        return _PinningHandler()

    def test_file_ftp_data_redirects_raise_http_error(self):
        handler = self._handler()
        for target in (
            "file:///C:/secret.txt",
            "ftp://169.254.169.254/",
            "data:text/plain,hello",
        ):
            with self.assertRaises(HTTPError) as ctx:
                handler.redirect_request(None, None, 302, "Found", {}, target)
            self.assertIn("blocked", str(ctx.exception))

    def test_http_redirect_still_routes_through_the_parent(self):
        handler = self._handler()

        class _Req:
            def __init__(self, url):
                self.full_url = url
                self.origin_req_host = "example.com"
                self.headers = {}
                self.unredirected_hdrs = {}

            def get_method(self):
                return "GET"

        class _FP:
            def read(self, n=-1):
                return b""

        new_req = handler.redirect_request(
            _Req("https://example.com/old"),
            _FP(),
            301,
            "Moved",
            {"Location": "https://example.com/new"},
            "https://example.com/new",
        )
        self.assertIsNotNone(new_req)
        self.assertEqual(new_req.full_url, "https://example.com/new")


class D2KillTreeTest(unittest.TestCase):
    """kill_shell by task_id kills the whole tree and finalizes the task."""

    def test_task_id_kill_terminates_the_process_and_marks_done(self):
        import core.tools.commands as commands
        from core.tools.commands import KillShellTool

        prefix = tempfile.mkdtemp(prefix="mantra-fix-d2-")
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            cwd=prefix,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            commands._TASKS["tsk_d2_test"] = {
                "task_id": "tsk_d2_test",
                "command": "sleep",
                "log_path": os.path.join(prefix, "task.log"),
                "pid": child.pid,
                "process": child,
                "start_time": time.time(),
                "timeout": 120.0,
                "done": False,
            }
            sandbox = __import__("core.sandbox", fromlist=["LocalSandbox"]).LocalSandbox(prefix)
            out = KillShellTool().execute(sandbox, task_id="tsk_d2_test")
            self.assertIn("OK: killed task", out)
            for _ in range(50):
                if child.poll() is not None:
                    break
                time.sleep(0.1)
            # A dead child is the point: poll() must return an exit code,
            # never None. (poll() staying None would mean it survived.)
            self.assertIsNotNone(child.poll(), "direct child survived the kill")
            self.assertNotEqual(child.returncode, 0)
            self.assertTrue(commands._TASKS["tsk_d2_test"]["done"])
            log_path = commands._TASKS["tsk_d2_test"]["log_path"] or os.path.join(prefix, "task.log")
            with open(log_path, encoding="utf-8", errors="replace") as handle:
                log = handle.read()
            self.assertIn("interrupted by operator", log)
        finally:
            commands._TASKS.pop("tsk_d2_test", None)
            if child.poll() is None:
                child.kill()
                child.communicate(timeout=5)


class D5EditBlocklistTest(unittest.TestCase):
    """edit_file refuses harness/device paths before touching them."""

    def test_edit_refuses_blocked_paths(self):
        from core.sandbox import LocalSandbox
        from core.tools.files import EditFileTool

        ws = tempfile.mkdtemp(prefix="mantra-fix-d5-")
        sandbox = LocalSandbox(ws)
        sandbox.setup({})
        tool = EditFileTool()
        out = tool.execute(sandbox, "\\\\?\\C:\\x", old_string="a", new_string="b")
        self.assertIn("device namespace", out)
        out = tool.execute(sandbox, "CON", old_string="a", new_string="b")
        self.assertIn("device names are blocked", out)
        out = tool.execute(sandbox, "/dev/zero", old_string="a", new_string="b")
        self.assertIn("refusing to edit device path", out)

    def test_edit_of_a_normal_file_still_reaches_the_ledger_gate(self):
        from core.sandbox import LocalSandbox
        from core.tools.files import EditFileTool
        from core.tools.ledger import EditLedger

        ws = tempfile.mkdtemp(prefix="mantra-fix-d5b-")
        sandbox = LocalSandbox(ws)
        sandbox.setup({})
        tool = EditFileTool()
        tool.ledger = EditLedger()
        sandbox.write_file("ok.py", "x = 1\n")
        out = tool.execute(sandbox, "ok.py", old_string="1", new_string="2")
        self.assertIn("read_file before editing", out)


class D6DiffparseContentTest(unittest.TestCase):
    """Added lines starting with ++ and removed lines starting with -- survive."""

    def test_increment_operator_lines_are_kept(self):
        from core.diffparse import parse_diff

        files = parse_diff(
            "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1,2 +1,2 @@\n---x\n+++i;\n"
        )
        lines = [ln for f in files for h in f.hunks for ln in h.lines]
        self.assertIn("del", [ln.kind for ln in lines])
        self.assertIn("add", [ln.kind for ln in lines])
        self.assertIn("--x", [ln.text for ln in lines])
        self.assertIn("++i;", [ln.text for ln in lines])

    def test_spaced_headers_are_still_skipped(self):
        from core.diffparse import parse_diff

        files = parse_diff(
            "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1,1 +1,1 @@\n old\n"
        )
        kinds = [ln.kind for f in files for h in f.hunks for ln in h.lines]
        self.assertEqual(kinds, ["ctx"])


class D17ListDirGuardTest(unittest.TestCase):
    """Root-less list_dir refuses ".." and absolute paths before the shell."""

    class _Rootless:
        def exec(self, command, timeout=120.0):
            raise AssertionError("shell must not run for " + command)

    def test_parent_and_absolute_paths_are_refused(self):
        from core.tools.files import ListDirTool

        for path in ("..", "../etc", "C:\\Windows", "/etc", "sub/.."):
            msg = ListDirTool().execute(self._Rootless(), path)
            self.assertTrue(msg.startswith("ERROR"), msg=path)
            self.assertIn("inside the workspace", msg)

    def test_a_plain_name_still_uses_the_shell_fallback(self):
        from core.tools.files import ListDirTool
        from core.types import ExecResult

        class _RootlessExec:
            def __init__(self):
                self.calls = []

            def exec(self, command, timeout=120.0):
                self.calls.append(command)
                return ExecResult(exit_code=0, stdout="a.txt\n", stderr="")

        sandbox = _RootlessExec()
        out = ListDirTool().execute(sandbox, "src")
        self.assertEqual(len(sandbox.calls), 1)
        self.assertIn("a.txt", out)


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
        self.assertFalse(
            self._traversal('git diff --no-color -- flappy.py | findstr /C:"on_flap" /C:"^[-+]"')
        )


class D24ByteBudgetResumeOffsetTest(unittest.TestCase):
    """After a byte-budget cut, the suggested resume offset counts only
    the lines actually shown, so re-reading from it continues cleanly."""

    def test_resume_offset_advances_past_the_byte_cut(self):
        from core.sandbox import LocalSandbox
        from core.tools.files import ReadFileTool

        ws = tempfile.mkdtemp(prefix="mantra-fix-d24-")
        sandbox = LocalSandbox(ws)
        sandbox.setup({})
        content = "".join(
            "line%04d" % i + " " + "x" * 600 + "\n" for i in range(300)
        )
        sandbox.write_file("big.txt", content)
        out = ReadFileTool().execute(sandbox, "big.txt", offset=0, limit=2000)
        self.assertIn("byte cap 128KB hit", out)
        self.assertIn("showing ", out)
        shown = int(out.split("showing ")[1].split(" ")[0])
        resume_match = re.search(r"retry with offset=(\d+)", out)
        self.assertIsNotNone(resume_match)
        resume = int(resume_match.group(1))
        self.assertEqual(resume, shown)
        # Re-reading from the suggested offset continues cleanly: the
        # first line shown there is the one that follows the cut.
        out2 = ReadFileTool().execute(sandbox, "big.txt", offset=resume, limit=2000)
        self.assertIn("line%04d" % resume, out2)

    def test_byte_cut_recounts_lines_not_the_raw_window(self):
        from core.sandbox import LocalSandbox
        from core.tools.files import ReadFileTool

        ws = tempfile.mkdtemp(prefix="mantra-fix-d24b-")
        sandbox = LocalSandbox(ws)
        sandbox.setup({})
        content = "".join(
            "line%03d" % i + " " + "x" * 1890 + "\n" for i in range(200)
        )
        sandbox.write_file("mid_cut.txt", content)
        out = ReadFileTool().execute(sandbox, "mid_cut.txt", offset=0, limit=2000)
        self.assertIn("byte cap 128KB hit", out)
        self.assertIn("showing ", out)
        shown = int(out.split("showing ")[1].split(" ")[0])
        resume_match = re.search(r"retry with offset=(\d+)", out)
        self.assertIsNotNone(resume_match)
        resume = int(resume_match.group(1))
        # The offset counts lines shown (each ~1.9KB, so far fewer than
        # 200 lines fit), never the byte count that fit the budget.
        self.assertEqual(resume, shown)
        self.assertLess(shown, 200)


class D22EvaluatorTimeoutTest(unittest.TestCase):
    """CommandEvaluator surfaces bad timeouts at construction."""

    def test_out_of_range_and_non_numeric_timeouts_raise(self):
        from core.evaluators import CommandEvaluator

        for bad in (0, -1, 601, 700, "abc", None):
            with self.assertRaises(ValueError):
                CommandEvaluator(test_cmd="echo hi", timeout=bad)

    def test_valid_timeouts_are_accepted(self):
        from core.evaluators import CommandEvaluator

        self.assertEqual(CommandEvaluator(test_cmd="echo hi", timeout=60.0).timeout, 60.0)
        self.assertEqual(CommandEvaluator(test_cmd="echo hi").timeout, 600.0)


class BulkReadSkippedCountTest(unittest.TestCase):
    """Cap-skipped files are counted as matched minus shown minus errors.

    The bulk header reports READ <shown>/<matched> and, when the aggregate
    100KB cap breaks the read loop, adds "(+N more, aggregate cap 100KB)".
    N was once len(files) - len(out_parts), which counted error-excluded
    files as cap-skipped; these tests pin the corrected accounting.
    """

    def _tool(self):
        from core.tools.files import ReadFileTool

        return ReadFileTool()

    def _sandbox(self, prefix: str):
        import shutil

        from core.sandbox import LocalSandbox

        ws = tempfile.mkdtemp(prefix=prefix)
        self.addCleanup(shutil.rmtree, ws, ignore_errors=True)
        sandbox = LocalSandbox(ws)
        sandbox.setup({})
        return ws, sandbox

    def test_no_errors_left_of_the_break_counts_every_unshown_file(self):
        _ws, sandbox = self._sandbox("mantra-bulk-1-")
        # ~12.2KB rendered per file: 8 chunks fit the 100KB aggregate cap,
        # the 9th breaks it. 12 files matched -> skipped = 12 - 8 - 0 = 4.
        line = "x" * 75 + "\n"
        for i in range(12):
            sandbox.write_file(f"m{i}.txt", line * 160)
        out = self._tool().execute(sandbox, "m*.txt")
        self.assertTrue(out.startswith("READ "))
        header = out.splitlines()[0]
        shown = int(header.split("READ ")[1].split("/")[0])
        matched = int(header.split("/")[1].split(" ")[0])
        skipped = int(header.split("(+")[1].split(" more")[0])
        self.assertEqual(shown, 8)
        self.assertEqual(matched, 12)
        self.assertEqual(skipped, 4)
        self.assertEqual(shown + skipped, matched)
        self.assertLess(len(out), 120_000)  # aggregate cap held

    def test_errored_files_are_not_counted_as_cap_skipped(self):
        from core.sandbox import LocalSandbox

        ws, _sandbox = self._sandbox("mantra-bulk-2-")

        class _BrokenSandbox(LocalSandbox):
            # The first file in sort order is unreadable, so the read loop
            # records an error before the cap can break.
            def read_file(self, path):
                if path == "m0.txt":
                    raise OSError("simulated read failure")
                return super().read_file(path)

        sandbox = _BrokenSandbox(ws)
        sandbox.setup({})
        line = "x" * 75 + "\n"
        for i in range(12):
            sandbox.write_file(f"m{i}.txt", line * 160)
        out = self._tool().execute(sandbox, "m*.txt")
        header = out.splitlines()[0]
        shown = int(header.split("READ ")[1].split("/")[0])
        matched = int(header.split("/")[1].split(" ")[0])
        self.assertEqual(shown, 8)  # m1..m8 rendered before the cap broke
        self.assertEqual(matched, 12)
        # Old formula: 12 - 8 shown = 4 cap-skipped (wrong: m0 was an
        # error, not a cap skip). Correct: 12 - 8 - 1 error = 3.
        self.assertIn("(+3 more", header)
        self.assertNotIn("(+4 more", header)
        # Errors are counted in the arithmetic but never appended to the
        # bulk body (only the header notes the shortfall), so the header
        # above is the observable contract.
        self.assertLess(len(out), 120_000)

    def test_all_files_shown_reports_no_skipped_suffix(self):
        _ws, sandbox = self._sandbox("mantra-bulk-3-")
        for i in range(6):
            sandbox.write_file(f"s{i}.txt", "tiny\n")
        out = self._tool().execute(sandbox, "s*.txt")
        self.assertTrue(out.startswith("READ 6/6"))
        self.assertNotIn("(+", out)
        self.assertLess(len(out), 120_000)


if __name__ == "__main__":
    unittest.main()
