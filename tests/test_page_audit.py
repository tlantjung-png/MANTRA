"""On-page audit: deterministic page inspection with bounded output.

No test here touches the network. ``core.tools.web.urlopen`` is always
patched, so the suite stays offline and deterministic.
"""

from __future__ import annotations

import sys
from unittest import mock

sys.path.insert(0, ".")

from core.sandbox import LocalSandbox  # noqa: E402
from core.tools.page_audit import PageAuditTool  # noqa: E402
from core.registry import TOOL_REGISTRY  # noqa: E402

HEALTHY = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<title>Releases</title>
<meta name="description" content="All product releases.">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="canonical" href="https://example.com/releases">
</head><body>
<h1>Releases</h1>
<h2>Version 2</h2>
<p>This is filler content that repeats until the page is long enough to audit. This is filler content that repeats until the page is long enough to audit. This is filler content that repeats until the page is long enough to audit. This is filler content that repeats until the page is long enough to audit. This is filler content that repeats until the page is long enough to audit. This is filler content that repeats until the page is long enough to audit. This is filler content that repeats until the page is long enough to audit. This is filler content that repeats until the page is long enough to audit. This is filler content that repeats until the page is long enough to audit. This is filler content that repeats until the page is long enough to audit. This is filler content that repeats until the page is long enough to audit. This is filler content that repeats until the page is long enough to audit. This is filler content that repeats until the page is long enough to audit. This is filler content that repeats until the page is long enough to audit. This is filler content that repeats until the page is long enough to audit. This is filler content that repeats until the page is long enough to audit.</|inc_tool_call_ml|>
<img src="/logo.png" alt="Logo">
<a href="/docs">Docs</a>
<a href="https://partner.example.com">Partner</a>
</body></html>"""

MISSING = """<html><head><title>An extremely long title that surely exceeds sixty characters here yes it does</title></head><body>
<img src="x.png">
<a href="#">
<h1>One</h1>
<h1>Two</h1>
</body></html>"""

NO_TITLE = """<html><body>
<h2>Section</h2>
<p>Short.</p>
</body></html>"""


class _Headers:
    def get(self, key, default=""):
        if key.lower() == "content-type":
            return "text/html; charset=utf-8"
        if key.lower() == "content-encoding":
            return ""
        return default

    def get_content_charset(self):
        return "utf-8"


class _Response:
    def __init__(self, body, url, status=200):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self._body = body
        self._status = status
        self._url = url
        self.headers = _Headers()

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


def _serve(pages: dict[str, str]) -> mock.patch:
    def fake_open(request, timeout=None):
        url = request.full_url
        for prefix, body in pages.items():
            if url.startswith(prefix):
                return _Response(body, url)
        raise OSError(f"unexpected URL {url}")

    return mock.patch("core.tools.web.urlopen", side_effect=fake_open)


def _audit(url: str, compare_url: str = "") -> str:
    return PageAuditTool().execute(None, url=url, compare_url=compare_url)


# --- healthy page ---------------------------------------------------------


def test_healthy_page_has_no_findings() -> None:
    with _serve({"https://example.com/releases": HEALTHY}):
        out = _audit("https://example.com/releases")
    assert "0 finding(s)" in out


def test_healthy_metrics_are_reported() -> None:
    with _serve({"https://example.com/releases": HEALTHY}):
        out = _audit("https://example.com/releases")
    assert "h1: 1" in out
    assert "h2: 1" in out
    assert "canonical: https://example.com/releases" in out
    assert "viewport: yes" in out
    assert "1 missing alt" in out or "0 missing alt" in out


# --- rule detection -------------------------------------------------------


def test_missing_title() -> None:
    with _serve({"https://example.com/notitle": NO_TITLE}):
        out = _audit("https://example.com/notitle")
    assert "[O01 missing title]" in out
    assert "[O07 missing h1]" in out
    assert "[O11 thin content]" in out


def test_title_too_long() -> None:
    with _serve({"https://example.com/missing": MISSING}):
        out = _audit("https://example.com/missing")
    assert "[O02 title too long]" in out


def test_missing_meta_description() -> None:
    with _serve({"https://example.com/missing": MISSING}):
        out = _audit("https://example.com/missing")
    assert "[O03 missing meta description]" in out


def test_missing_canonical() -> None:
    with _serve({"https://example.com/missing": MISSING}):
        out = _audit("https://example.com/missing")
    assert "[O04 missing canonical link]" in out


def test_missing_viewport() -> None:
    with _serve({"https://example.com/missing": MISSING}):
        out = _audit("https://example.com/missing")
    assert "[O05 missing viewport meta]" in out


def test_multiple_h1() -> None:
    with _serve({"https://example.com/missing": MISSING}):
        out = _audit("https://example.com/missing")
    assert "[O08 multiple h1]" in out


def test_image_without_alt() -> None:
    with _serve({"https://example.com/missing": MISSING}):
        out = _audit("https://example.com/missing")
    assert "[O10 images without alt text]" in out


def test_no_outbound_links() -> None:
    with _serve({"https://example.com/missing": MISSING}):
        out = _audit("https://example.com/missing")
    assert "[O13 no outbound links]" in out


def test_noindex_is_flagged() -> None:
    page = "<html><head><meta name=\"robots\" content=\"noindex\"></head><body><h1>A</h1></body></html>"
    with _serve({"https://example.com/x": page}):
        out = _audit("https://example.com/x")
    assert "[O06 page is noindex]" in out


# --- comparison mode ------------------------------------------------------


def test_comparison_lists_both_metrics() -> None:
    better = HEALTHY
    worse = MISSING
    with _serve(
        {
            "https://example.com/better": better,
            "https://example.com/worse": worse,
        }
    ):
        out = _audit("https://example.com/better", compare_url="https://example.com/worse")
    assert "[comparison]" in out
    assert "h1: 1 | 2" in out


def test_comparison_equal_markers() -> None:
    with _serve(
        {
            "https://example.com/a": HEALTHY,
            "https://example.com/b": HEALTHY,
        }
    ):
        out = _audit("https://example.com/a", compare_url="https://example.com/b")
    assert not any(line.startswith("  !=") for line in out.splitlines())
    assert "= equal, != differs" in out


# --- fetch errors ---------------------------------------------------------


def test_unsupported_scheme() -> None:
    assert _audit("ftp://example.com/x").startswith("audit failed: unsupported scheme")


def test_no_url() -> None:
    assert _audit("").startswith("audit failed: no URL given")


def test_fetch_error_is_reported() -> None:
    with _serve({"https://example.com/ok": HEALTHY}):
        out = _audit("https://example.com/down")
    assert out.startswith("audit failed:")


def test_compare_fetch_error_is_reported() -> None:
    with _serve({"https://example.com/ok": HEALTHY}):
        out = _audit("https://example.com/ok", compare_url="https://example.com/down")
    assert out.startswith("audit failed:")


# --- bounds and determinism ----------------------------------------------


def test_report_is_deterministic() -> None:
    with _serve({"https://example.com/missing": MISSING}):
        first = _audit("https://example.com/missing")
        second = _audit("https://example.com/missing")
    assert first == second


def test_non_string_arguments_do_not_raise() -> None:
    tool = PageAuditTool()
    assert tool.execute(None, url=123).startswith("audit failed:")  # type: ignore[arg-type]


def test_registered_with_expected_schema() -> None:
    assert TOOL_REGISTRY["page_audit"] is PageAuditTool
    schema = PageAuditTool().schema()
    assert schema["function"]["name"] == "page_audit"
    assert "url" in schema["function"]["parameters"]["properties"]
    assert schema["function"]["parameters"]["required"] == ["url"]
