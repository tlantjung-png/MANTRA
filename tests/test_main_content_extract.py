"""Optional main-content-only extraction in the shared markup reader.

Covers the reader itself, the document tool, and the two unchanged callers
that must keep their original default behavior. No test here uses the
network: the fetch entry points are patched.
"""

from __future__ import annotations

import os
from unittest import mock

from core.sandbox import LocalSandbox
from core.tools._htmltext import html_to_text
from core.tools.extract import ExtractDocumentTool
from core.tools.page_audit import PageAuditTool
from core.tools.web import WebFetchTool

PAGE = """<html><head><title>Test</title>
<style>.x { color: red }</style>
<script>secret-script-text</script></head>
<body>
<header>SITE_HEADER_TEXT</header>
<nav>SITE_NAV_TEXT<a href="/x">SITE_NAV_LINK_TEXT</a></nav>
<aside>SITE_ASIDE_TEXT</aside>
<form><p>SITE_FORM_TEXT</p></form>
<noscript>SITE_NOSCRIPT_TEXT</noscript>
<template>SITE_TEMPLATE_TEXT</template>
<svg><text>SITE_SVG_TEXT</text></svg>
<main>
<header>ARTICLE_HEADER_TEXT</header>
<article>
<h1>HEADING_TEXT</h1>
<h2>SUBHEADING_TEXT</h2>
<p>PARAGRAPH_TEXT</p>
<ul><li>LIST_ITEM_TEXT</li></ul>
<table><tr><td>TABLE_TEXT</td></tr></table>
<pre><code>CODE_TEXT</code></pre>
<nav>INLINE_NAV_TEXT</nav>
</article>
</main>
<footer>SITE_FOOTER_TEXT</footer>
</body></html>"""

DEFAULT_SHELL = (
    "SITE_HEADER_TEXT",
    "SITE_NAV_TEXT",
    "SITE_NAV_LINK_TEXT",
    "SITE_ASIDE_TEXT",
    "SITE_FORM_TEXT",
    "SITE_FOOTER_TEXT",
    "INLINE_NAV_TEXT",
)
# These are dropped by the existing skip rules even in the default mode.
SHELL = DEFAULT_SHELL + (
    "SITE_NOSCRIPT_TEXT",
    "SITE_TEMPLATE_TEXT",
    "SITE_SVG_TEXT",
    "secret-script-text",
)
ARTICLE = (
    "ARTICLE_HEADER_TEXT",
    "HEADING_TEXT",
    "SUBHEADING_TEXT",
    "PARAGRAPH_TEXT",
    "LIST_ITEM_TEXT",
    "TABLE_TEXT",
    "CODE_TEXT",
)


# --- shared reader --------------------------------------------------------


def test_default_extraction_still_keeps_navigation_text() -> None:
    text = html_to_text(PAGE)
    for token in DEFAULT_SHELL:
        assert token in text


def test_main_only_removes_shell_but_keeps_paragraphs_and_headings() -> None:
    text = html_to_text(PAGE, main_only=True)
    for token in SHELL:
        assert token not in text
    for token in ARTICLE:
        assert token in text


def test_page_header_is_dropped_but_article_header_is_kept() -> None:
    page = (
        "<header>PAGE_HEADER</header>"
        "<article><header>ARTICLE_HEADER</header><p>BODY</p></article>"
    )
    text = html_to_text(page, main_only=True)
    assert "PAGE_HEADER" not in text
    assert "ARTICLE_HEADER" in text
    assert "BODY" in text


def test_nested_excluded_regions_do_not_hide_following_text() -> None:
    text = html_to_text(
        "<nav>outer<aside>inner</aside>still-nav</nav><p>after</p>",
        main_only=True,
    )
    assert "outer" not in text
    assert "inner" not in text
    assert "still-nav" not in text
    assert "after" in text


def test_never_rendered_tags_stay_skipped_in_both_modes() -> None:
    page = (
        "<p>keep</p><script>drop-script</script><style>drop-style</style>"
        "<noscript>drop-noscript</noscript><template>drop-template</template>"
        "<svg>drop-svg</svg><iframe>drop-iframe</iframe>"
    )
    for text in (html_to_text(page), html_to_text(page, main_only=True)):
        assert text == "keep"


def test_malformed_markup_returns_safely_in_both_modes() -> None:
    fragments = (
        "<article><p>unclosed paragraph",
        "</nav><p>stray close</p>",
        "<main><header>head<p>body</main><footer>tail</footer>",
        "<nav><aside>nested<script>oops</nav></aside>after",
        "<p>text</p><div><p>more",
        "",
    )
    for fragment in fragments:
        for main_only in (False, True):
            result = html_to_text(fragment, main_only=main_only)
            assert isinstance(result, str)


# --- document tool --------------------------------------------------------


def test_extract_document_honors_and_exposes_main_only(sandbox) -> None:
    with open(os.path.join(sandbox.root, "doc.html"), "w", encoding="utf-8") as fh:
        fh.write(PAGE)
    tool = ExtractDocumentTool()
    default = tool.execute(sandbox, path="doc.html")
    reduced = tool.execute(sandbox, path="doc.html", main_only=True)
    assert "SITE_NAV_TEXT" in default
    assert "SITE_FOOTER_TEXT" in default
    assert "SITE_NAV_TEXT" not in reduced
    assert "SITE_FOOTER_TEXT" not in reduced
    assert "PARAGRAPH_TEXT" in reduced
    assert "HEADING_TEXT" in reduced

    params = tool.schema()["function"]["parameters"]
    assert params["required"] == ["path"]
    assert params["properties"]["main_only"]["type"] == "boolean"


def test_extract_document_error_contract_is_unchanged(sandbox) -> None:
    tool = ExtractDocumentTool()
    assert tool.execute(sandbox, path="missing.html", main_only=True).startswith(
        "ERROR: file not found"
    )
    assert (
        tool.execute(sandbox, path="bad\npath.html", main_only=True)
        == "ERROR: path contains invalid characters"
    )


def test_shell_fallback_forwards_main_only() -> None:
    fake = mock.Mock()
    fake.root = None
    tool = ExtractDocumentTool()

    def _run(main_only=False):
        fake.exec.side_effect = [
            mock.Mock(exit_code=0, stdout="100\n"),
            mock.Mock(exit_code=0, stdout=PAGE),
        ]
        return tool.execute(fake, path="doc.html", main_only=main_only)

    default = _run()
    reduced = _run(main_only=True)
    assert "SITE_NAV_TEXT" in default
    assert "SITE_NAV_TEXT" not in reduced
    assert "PARAGRAPH_TEXT" in reduced


# --- unchanged callers ----------------------------------------------------

CALLER_PAGE = (
    "<html><head><title>T</title></head><body>"
    "<nav>NAV_ONLY_WORD</nav><p>BODY</p></body></html>"
)


class _Headers:
    def get(self, key, default=""):
        key = key.lower()
        if key == "content-type":
            return "text/html; charset=utf-8"
        if key == "content-encoding":
            return ""
        return default

    def get_content_charset(self):
        return "utf-8"


class _Response:
    def __init__(self, body, url="https://example.com/page"):
        self._body = body.encode("utf-8")
        self._url = url
        self.headers = _Headers()

    def read(self, size=-1):
        return self._body if size is None or size < 0 else self._body[:size]

    def geturl(self):
        return self._url

    def getcode(self):
        return 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_web_fetch_caller_keeps_its_original_reader_call() -> None:
    with mock.patch("core.tools.web.urlopen", return_value=_Response(CALLER_PAGE)), \
         mock.patch("core.tools.web.html_to_text", wraps=html_to_text) as reader:
        out = WebFetchTool().execute(LocalSandbox(), url="https://example.com/page")
    reader.assert_called_once()
    args, kwargs = reader.call_args
    assert len(args) == 1 and not kwargs
    assert "NAV_ONLY_WORD" in out


def test_page_audit_caller_keeps_its_original_reader_call() -> None:
    expected_words = len(html_to_text(CALLER_PAGE).split())
    with mock.patch("core.tools.web.urlopen", return_value=_Response(CALLER_PAGE)), \
         mock.patch("core.tools.page_audit.html_to_text", wraps=html_to_text) as reader:
        out = PageAuditTool().execute(None, url="https://example.com/page")
    reader.assert_called_once()
    args, kwargs = reader.call_args
    assert len(args) == 1 and not kwargs
    assert f"words: {expected_words}" in out
