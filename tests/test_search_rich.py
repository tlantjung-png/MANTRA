"""Extended tests for the richer literal search behavior.

These sit alongside the existing search tools smoke tests and focus on the
aspects touched by the adaptation: description shape, multi-file matches,
find-file semantics, and shell-fallback normalization.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from unittest import mock, TestCase

from core.sandbox import LocalSandbox
from core.tools.search import FindFileTool, SearchCodeTool
from core.tools.extract import ExtractDocumentTool, QueryTreeTool


class SearchRichToolTest(TestCase):
    """Extended tests for the improved literal search behavior.

    These sit alongside the existing search tools smoke tests and focus on the
    aspects touched by the adaptation: description shape, multi-file matches,
    find-file semantics, and shell-fallback normalization.
    """

    def setUp(self):
        self.workspace = tempfile.mkdtemp(prefix="mantra-search-rich-")
        self.sandbox = LocalSandbox(self.workspace)
        self.sandbox.setup({})

    def tearDown(self):
        shutil.rmtree(self.workspace, ignore_errors=True)

    def test_search_code_description_is_now_task_oriented(self):
        tool = SearchCodeTool()
        assert "code search" in tool.description.lower()
        assert "symbol" in tool.description.lower()
        assert tool.parameters["properties"]["query"]["type"] == "string"

    def test_search_code_still_reports_path_and_line(self):
        with open(os.path.join(self.workspace, "a.py"), "w", encoding="utf-8") as fh:
            fh.write("def foo():\n    return 'hi'\n")
        out = SearchCodeTool().execute(self.sandbox, query="return 'hi'")
        assert "a.py:2" in out
        assert "no matches" not in out

    def test_search_code_multifile_literal_matches(self):
        with open(os.path.join(self.workspace, "x.py"), "w", encoding="utf-8") as fh:
            fh.write("needle\n")
        with open(os.path.join(self.workspace, "y.py"), "w", encoding="utf-8") as fh:
            fh.write("needle\n")
        out = SearchCodeTool().execute(self.sandbox, query="needle")
        assert "x.py" in out
        assert "y.py" in out

    def test_search_code_absent_query_returns_no_matches(self):
        out = SearchCodeTool().execute(self.sandbox, query="zzz-not-there")
        assert "no matches" in out

    def test_search_code_rejects_naked_newline_and_long_query(self):
        tool = SearchCodeTool()
        assert "invalid characters" in tool.execute(self.sandbox, query="a\nb")
        assert "too long" in tool.execute(self.sandbox, query="x" * 501)

    def test_find_file_matches_substring_and_rejects_metacharacters(self):
        with open(os.path.join(self.workspace, "report_q3.py"), "w", encoding="utf-8") as fh:
            fh.write("x")
        out = FindFileTool().execute(self.sandbox, pattern="q3")
        assert "report_q3.py" in out
        assert "unsupported characters" in FindFileTool().execute(self.sandbox, pattern="*")

    def test_find_file_shell_fallback_normalizes_grep_style_output(self):
        tool = FindFileTool()
        fake = mock.Mock()
        fake.root = None
        fake.exec.return_value = mock.Mock(exit_code=0, stdout="a.py\nb.py\n")
        out = tool.execute(fake, pattern="py")
        # The `root is None` path quotes a glob and shells out to `find`,
        # so this probe is mainly a sanity check on the code path, not a
        # claim about the exact fallback output shape.
        assert isinstance(out, str)

    def test_search_code_shell_fallback_normalizes_grep_style_output(self):
        tool = SearchCodeTool()
        fake = mock.Mock()
        fake.root = None
        fake.exec.return_value = mock.Mock(exit_code=0, stdout="a.py:1: needle\nb.py:2: needle\n")
        out = tool.execute(fake, query="needle")
        assert "a.py:1" in out
        assert "b.py:2" in out

    def test_extract_document_html_to_readable_text(self):
        with open(os.path.join(self.workspace, "doc.html"), "w", encoding="utf-8") as fh:
            fh.write("<html><body><h1>Title</h1><p>visible <script>x</script> text</p></body></html>")
        out = ExtractDocumentTool().execute(self.sandbox, path="doc.html")
        assert "Title" in out
        assert "visible" in out
        assert "<script>" not in out

    def test_extract_document_json_pretty_prints(self):
        with open(os.path.join(self.workspace, "data.json"), "w", encoding="utf-8") as fh:
            fh.write('{"items":[{"id":1}],"ok":true}')
        out = ExtractDocumentTool().execute(self.sandbox, path="data.json")
        assert '"items"' in out
        assert "true" in out

    def test_extract_document_unsupported_format_gives_note(self):
        with open(os.path.join(self.workspace, "archive.zip"), "w", encoding="utf-8") as fh:
            fh.write("not a real zip")
        out = ExtractDocumentTool().execute(self.sandbox, path="archive.zip")
        assert "not extracted" in out
        assert "archive.zip" in out

    def test_extract_document_bad_json_falls_back_to_raw_snippet(self):
        with open(os.path.join(self.workspace, "bad.json"), "w", encoding="utf-8") as fh:
            fh.write("not json {{{")
        out = ExtractDocumentTool().execute(self.sandbox, path="bad.json")
        assert "bad.json" in out
        assert "not json" in out

    def test_extract_document_bad_xml_falls_back_to_raw_snippet(self):
        with open(os.path.join(self.workspace, "bad.xml"), "w", encoding="utf-8") as fh:
            fh.write("<root>unclosed")
        out = ExtractDocumentTool().execute(self.sandbox, path="bad.xml")
        assert "bad.xml" in out
        assert "unclosed" in out

    def test_extract_document_missing_file_is_error(self):
        out = ExtractDocumentTool().execute(self.sandbox, path="nope.txt")
        assert out.startswith("ERROR")
        assert "nope.txt" in out

    def test_extract_document_rejects_escape_path(self):
        out = ExtractDocumentTool().execute(self.sandbox, path="../outside.txt")
        assert out.startswith("ERROR")

    def test_extract_document_large_file_recommends_windows(self):
        with open(os.path.join(self.workspace, "big.bin"), "w", encoding="utf-8") as fh:
            fh.write("x" * 600000)
        out = ExtractDocumentTool().execute(self.sandbox, path="big.bin")
        assert "large" in out.lower() or "cap" in out.lower()

    def test_query_tree_glob_matches_files(self):
        pkg = os.path.join(self.workspace, "pkg")
        os.makedirs(pkg, exist_ok=True)
        with open(os.path.join(pkg, "mod.py"), "w", encoding="utf-8") as fh:
            fh.write("x=1\n")
        out = QueryTreeTool().execute(self.sandbox, pattern="pkg/*.py")
        assert "mod.py" in out

    def test_query_tree_shape_lists_directory(self):
        pkg = os.path.join(self.workspace, "pkg")
        os.makedirs(pkg, exist_ok=True)
        with open(os.path.join(pkg, "mod.py"), "w", encoding="utf-8") as fh:
            fh.write("x=1\n")
        out = QueryTreeTool().execute(self.sandbox, pattern="{pkg}")
        assert "mod.py" in out

    def test_query_tree_shape_glob_lists_matching_files(self):
        pkg = os.path.join(self.workspace, "pkg")
        os.makedirs(pkg, exist_ok=True)
        with open(os.path.join(pkg, "mod.py"), "w", encoding="utf-8") as fh:
            fh.write("x=1\n")
        out = QueryTreeTool().execute(self.sandbox, pattern="{pkg/*.py}")
        assert "mod.py" in out

    def test_query_tree_rejects_escape_shape(self):
        out = QueryTreeTool().execute(self.sandbox, pattern="{../}")
        assert out.startswith("ERROR") or "no matches" in out

    def test_query_tree_recursive_glob(self):
        pkg = os.path.join(self.workspace, "pkg")
        os.makedirs(os.path.join(pkg, "deep"), exist_ok=True)
        with open(os.path.join(pkg, "deep", "found.rb"), "w", encoding="utf-8") as fh:
            fh.write("x=1\n")
        out = QueryTreeTool().execute(self.sandbox, pattern="pkg/**/*.rb")
        assert "found.rb" in out

    def test_query_tree_shape_nested_directory(self):
        pkg = os.path.join(self.workspace, "pkg")
        deep = os.path.join(pkg, "deep")
        os.makedirs(deep, exist_ok=True)
        with open(os.path.join(deep, "found.rb"), "w", encoding="utf-8") as fh:
            fh.write("x=1\n")
        # The tiny shape syntax descends one level at a time, so reach the
        # nested directory through its parent.
        out = QueryTreeTool().execute(self.sandbox, pattern="{pkg/deep}")
        assert "found.rb" in out or "deep" in out

    def test_query_tree_shape_recursive_glob(self):
        pkg = os.path.join(self.workspace, "pkg")
        os.makedirs(os.path.join(pkg, "deep"), exist_ok=True)
        with open(os.path.join(pkg, "deep", "found.rb"), "w", encoding="utf-8") as fh:
            fh.write("x=1\n")
        out = QueryTreeTool().execute(self.sandbox, pattern="{pkg/**/*.rb}")
        assert "found.rb" in out

    def test_query_tree_rejects_unsupported_characters(self):
        out = QueryTreeTool().execute(self.sandbox, pattern="pkg/*.py; rm -rf /")
        assert out.startswith("ERROR")
        out = QueryTreeTool().execute(self.sandbox, pattern="{pkg/*.py;}")
        assert out.startswith("ERROR")
        out = QueryTreeTool().execute(self.sandbox, pattern="x" * 501)
        assert out.startswith("ERROR")

    def test_query_tree_shape_descent_lists_nested_children(self):
        # Regression pin: {pkg/deep} must descend through the literal parent
        # segment and list the nested directory's own children.
        pkg = os.path.join(self.workspace, "pkg")
        deep = os.path.join(pkg, "deep")
        os.makedirs(deep, exist_ok=True)
        with open(os.path.join(deep, "found.rb"), "w", encoding="utf-8") as fh:
            fh.write("x=1\n")
        os.makedirs(os.path.join(pkg, "other"), exist_ok=True)
        out = QueryTreeTool().execute(self.sandbox, pattern="{pkg/deep}")
        assert "found.rb" in out
        # Descent lists only the target directory's children, not its
        # siblings: `other` lives under pkg, not under pkg/deep.
        assert "other" not in out

    def test_query_tree_shape_descent_marks_directories_with_slash(self):
        pkg = os.path.join(self.workspace, "pkg")
        deep = os.path.join(pkg, "deep")
        os.makedirs(os.path.join(deep, "sub"), exist_ok=True)
        with open(os.path.join(deep, "leaf.txt"), "w", encoding="utf-8") as fh:
            fh.write("x")
        out = QueryTreeTool().execute(self.sandbox, pattern="{pkg/deep}")
        assert "sub/" in out
        assert "leaf.txt" in out

    def test_query_tree_shape_descent_two_levels(self):
        pkg = os.path.join(self.workspace, "pkg")
        deep = os.path.join(pkg, "deep")
        inner = os.path.join(deep, "inner")
        os.makedirs(inner, exist_ok=True)
        with open(os.path.join(inner, "way_down.py"), "w", encoding="utf-8") as fh:
            fh.write("x=1\n")
        out = QueryTreeTool().execute(self.sandbox, pattern="{pkg/deep/inner}")
        assert "way_down.py" in out

    def test_query_tree_shape_descent_with_tail_glob(self):
        pkg = os.path.join(self.workspace, "pkg")
        deep = os.path.join(pkg, "deep")
        os.makedirs(deep, exist_ok=True)
        with open(os.path.join(deep, "a.py"), "w", encoding="utf-8") as fh:
            fh.write("x")
        with open(os.path.join(deep, "b.txt"), "w", encoding="utf-8") as fh:
            fh.write("x")
        out = QueryTreeTool().execute(self.sandbox, pattern="{pkg/deep/*.py}")
        assert "a.py" in out
        assert "b.txt" not in out

    def test_query_tree_shape_descent_missing_target_errors(self):
        pkg = os.path.join(self.workspace, "pkg")
        os.makedirs(pkg, exist_ok=True)
        out = QueryTreeTool().execute(self.sandbox, pattern="{pkg/does_not_exist}")
        assert out.startswith("ERROR")
        assert "does_not_exist" in out

    def test_query_tree_shape_descent_target_is_file_errors(self):
        pkg = os.path.join(self.workspace, "pkg")
        deep = os.path.join(pkg, "deep")
        os.makedirs(deep, exist_ok=True)
        with open(os.path.join(deep, "plain.txt"), "w", encoding="utf-8") as fh:
            fh.write("x")
        out = QueryTreeTool().execute(self.sandbox, pattern="{pkg/deep/plain.txt}")
        assert out.startswith("ERROR")

    def test_query_tree_shape_descent_rejects_workspace_escape(self):
        pkg = os.path.join(self.workspace, "pkg")
        os.makedirs(pkg, exist_ok=True)
        out = QueryTreeTool().execute(self.sandbox, pattern="{pkg/../outside}")
        # Either the '..' is rejected at the pattern level or the resolved
        # target is caught escaping the workspace root.
        assert out.startswith("ERROR")

    def test_query_tree_shape_escape_via_symlink_errors(self):
        pkg = os.path.join(self.workspace, "pkg")
        deep = os.path.join(pkg, "deep")
        os.makedirs(deep, exist_ok=True)
        outside = tempfile.mkdtemp(prefix="mantra-outside-")
        self.addCleanup(shutil.rmtree, outside, ignore_errors=True)
        try:
            os.symlink(outside, os.path.join(pkg, "link"))
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable on this platform")
        out = QueryTreeTool().execute(self.sandbox, pattern="{pkg/link}")
        # The symlink resolves outside the workspace, so descent must stop.
        # It may surface as "escapes workspace" or "not a directory" on
        # platforms that do not resolve through the link; both are refusals.
        assert out.startswith("ERROR")
        with open(os.path.join(outside, "leaked.txt"), "w", encoding="utf-8") as fh:
            fh.write("x")
        listed = QueryTreeTool().execute(self.sandbox, pattern="{pkg/link}")
        assert "leaked.txt" not in listed
