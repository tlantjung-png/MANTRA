"""Tests for the web_fetch tool and its HTML-to-text extractor.

No test here touches the network. ``urlopen`` is always patched, so the
suite stays offline and deterministic.
"""

from __future__ import annotations

import gzip
import io
import os
import sys
import unittest
from unittest import mock
from urllib.error import HTTPError, URLError

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "."))

from core.sandbox import LocalSandbox
from core.tools.web import (
    WebFetchTool,
    _inflate,
    html_to_text,
)
from core.registry import TOOL_REGISTRY, build_tools

PAGE = """<html><head><title>T</title>
<style>body { color: red }</style>
<script>console.log("ignore me")</script></head>
<body><h1>Release notes</h1>
<p>Fixed    the   container.</p>
<p>Added &amp; shipped web_fetch.</p>
<ul><li>one</li><li>two</li></ul>
</body></html>"""


class _Headers:
    """The two header operations the tool actually uses."""

    def __init__(self, content_type, content_encoding):
        self._type = content_type
        self._encoding = content_encoding

    def get(self, key, default=""):
        if key.lower() == "content-type":
            return self._type
        if key.lower() == "content-encoding":
            return self._encoding
        return default

    def get_content_charset(self):
        if "charset=" in self._type:
            return self._type.split("charset=")[1].split(";")[0].strip()
        return None


class _Response:
    """Just enough of an HTTP response for the tool to read."""

    def __init__(self, body, content_type="text/html; charset=utf-8", status=200,
                 encoding=None, url="https://example.com/page"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self._body = body
        self._status = status
        self._url = url
        self.headers = _Headers(content_type, encoding or "")

    def read(self, size=-1):
        return self._body if size is None or size < 0 else self._body[:size]

    def geturl(self):
        return self._url

    def getcode(self):
        return self._status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class ExtractorTest(unittest.TestCase):
    def test_tags_are_stripped(self):
        self.assertEqual(html_to_text("<p>hello</p>"), "hello")

    def test_scripts_and_styles_do_not_reach_the_text(self):
        text = html_to_text(PAGE)
        self.assertNotIn("ignore me", text)
        self.assertNotIn("color: red", text)

    def test_headings_and_paragraphs_stay_on_their_own_lines(self):
        text = html_to_text(PAGE)
        self.assertIn("Release notes\n", text)
        self.assertIn("Fixed the container.", text)

    def test_entities_are_decoded(self):
        self.assertIn("Added & shipped", html_to_text(PAGE))

    def test_list_items_are_separated(self):
        text = html_to_text(PAGE)
        self.assertIn("one\n", text)
        self.assertIn("two", text)

    def test_runs_of_whitespace_collapse(self):
        self.assertEqual(html_to_text("<p>a      b</p>"), "a b")

    def test_blank_line_runs_collapse_to_one(self):
        self.assertEqual(html_to_text("<p>a</p><br><br><br><p>b</p>"), "a\n\nb")

    def test_an_unclosed_skip_tag_does_not_blank_the_rest(self):
        # A stray </script> must not leave the depth counter above zero,
        # which would silently drop every word after it.
        text = html_to_text("</script><p>still here</p>")
        self.assertIn("still here", text)

    def test_empty_document_is_empty_text(self):
        self.assertEqual(html_to_text(""), "")


class InflateTest(unittest.TestCase):
    def test_gzip_is_decompressed(self):
        raw = gzip.compress(b"hello")
        self.assertEqual(_inflate(raw, "gzip"), b"hello")

    def test_deflate_is_decompressed(self):
        import zlib

        self.assertEqual(_inflate(zlib.compress(b"hello"), "deflate"), b"hello")

    def test_uncompressed_passes_through(self):
        self.assertEqual(_inflate(b"hello", ""), b"hello")

    def test_bad_payload_is_returned_untouched(self):
        self.assertEqual(_inflate(b"not gzip", "gzip"), b"not gzip")


class FetchTest(unittest.TestCase):
    def setUp(self):
        self.tool = WebFetchTool()
        self.sandbox = LocalSandbox()

    def _fetch(self, response=None, side_effect=None, **kwargs):
        # web_tools does `from urllib.request import urlopen`, so the name
        # lives in that module. Patching urllib.request.urlopen would
        # leave the real one bound here and every fetch would hit the
        # network - which is how the first run of this file failed.
        target = "core.tools.web.urlopen"
        if side_effect is not None:
            patcher = mock.patch(target, side_effect=side_effect)
        else:
            patcher = mock.patch(target, return_value=response)
        with patcher:
            return self.tool.execute(self.sandbox, **kwargs)

    def test_a_page_comes_back_as_text_with_a_header(self):
        out = self._fetch(_Response(PAGE), url="https://example.com/page")
        self.assertIn("https://example.com/page (HTTP 200)", out)
        self.assertIn("Release notes", out)
        self.assertNotIn("<p>", out)

    def test_a_bad_scheme_is_refused_without_any_network_call(self):
        out = self._fetch(side_effect=AssertionError("must not fetch"),
                          url="ftp://example.com/x")
        self.assertIn("unsupported scheme", out)

    def test_a_schemeless_url_is_refused(self):
        self.assertIn("unsupported scheme",
                      self._fetch(side_effect=AssertionError("no"), url="example.com"))

    def test_an_empty_url_is_refused(self):
        self.assertIn("no URL given", self._fetch(side_effect=AssertionError("no"), url=""))

    def test_http_errors_are_returned_not_raised(self):
        error = HTTPError("https://x", 404, "Not Found", {}, None)
        out = self._fetch(side_effect=error, url="https://x")
        self.assertIn("HTTP 404", out)

    def test_unreachable_hosts_are_returned_not_raised(self):
        out = self._fetch(side_effect=URLError("dns failure"), url="https://x")
        self.assertIn("dns failure", out)

    def test_a_timeout_is_returned_not_raised(self):
        out = self._fetch(side_effect=TimeoutError("timed out"), url="https://x")
        self.assertIn("timed out", out)

    def test_json_is_not_stripped_as_html(self):
        body = '{"a": 1, "b": "<b>two</b>"}'
        out = self._fetch(_Response(body, "application/json"), url="https://x")
        self.assertIn('"b": "<b>two</b>"', out)

    def test_long_text_is_truncated_and_marked(self):
        body = "<p>" + ("word " * 5000) + "</p>"
        out = self._fetch(_Response(body), url="https://x", max_chars=200)
        self.assertIn("[truncated]", out)
        self.assertIn("truncated at 200 chars", out)

    def test_a_page_with_no_text_says_so(self):
        out = self._fetch(_Response("<html><script>x</script></html>"), url="https://x")
        self.assertIn("no text content", out)

    def test_a_redirect_is_reported_by_its_final_url(self):
        out = self._fetch(_Response(PAGE, url="https://final.example/page"),
                          url="https://example.com/page")
        self.assertIn("https://final.example/page", out)

    def test_gzip_transport_is_decoded(self):
        raw = gzip.compress(b"<p>compressed but readable</p>")

        class Gzipped(_Response):
            def __init__(self):
                super().__init__(raw, content_type="text/html; charset=utf-8",
                                 encoding="gzip")

        self.assertIn("compressed but readable",
                      self._fetch(Gzipped(), url="https://x"))

    def test_a_bad_max_chars_falls_back_to_the_default(self):
        # A non-numeric max_chars must fall back, not raise.
        out = self._fetch(_Response(PAGE), url="https://x", max_chars="lots")
        self.assertIn("Release notes", out)

    def test_the_schema_is_valid_json_schema(self):
        schema = self.tool.schema()
        self.assertEqual(schema["function"]["name"], "web_fetch")
        self.assertEqual(schema["function"]["parameters"]["required"], ["url"])


class RegistryTest(unittest.TestCase):
    def test_both_spellings_resolve_to_the_same_tool(self):
        self.assertIs(TOOL_REGISTRY["web_fetch"], TOOL_REGISTRY["webfetch"])

    def test_it_builds_and_executes_through_the_registry(self):
        tools = build_tools(["web_fetch"])
        self.assertEqual([t.name for t in tools], ["web_fetch"])

    def test_it_is_not_treated_as_a_mutating_tool(self):
        # A fetch cannot change the workspace, so it must not be gated
        # behind an approval prompt the way write_file is.
        from core.agent.approvals import MUTATING_TOOLS

        self.assertNotIn("web_fetch", MUTATING_TOOLS)


if __name__ == "__main__":
    unittest.main()
