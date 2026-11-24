"""Deterministic on-page audit for arbitrary web pages.

Fetches a page through the same validating fetch pipeline as ``web_fetch``
and inspects its raw HTML: title, meta description, canonical link,
viewport, robots meta, heading structure, images, links, and content size.
Plain stdlib HTML parsing, no model call, no dependency, no score. The
comparison mode audits a second URL and prints the two metrics side by
side. Findings are advisory input for the ``on-page-audit`` skill, not a
gate.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any, ClassVar
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import Request

from core.tools import web
from core.tools._htmltext import html_to_text
from core.types import Sandbox
from core.types import Tool

_TITLE_LONG = 60
_MIN_WORDS = 200
_MAX_FINDINGS = 30
_SNIPPET_CHARS = 80

_EMPTY_RE = re.compile(r"^\s*$")

_META_ROBOTS = {"robots", "googlebot", "bingbot"}


@dataclass(frozen=True)
class Finding:
    rule: str
    title: str
    hint: str


@dataclass
class PageData:
    url: str = ""
    status: int | None = None
    title: str = ""
    meta_description: str = ""
    canonical: str = ""
    viewport: bool = False
    robots_noindex: bool = False
    robots_nofollow: bool = False
    lang: str = ""
    has_og_title: bool = False
    has_og_description: bool = False
    headings: list[str] = field(default_factory=list)
    images: list[str] = field(default_factory=list)  # alt texts
    links: list[tuple[str, str]] = field(default_factory=list)  # (href, text)
    word_count: int = 0


class _OnPageExtractor(HTMLParser):
    """Collect the on-page elements the audit inspects."""

    _SKIP = {"script", "style", "noscript", "template", "svg", "iframe"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.page = PageData()
        self._skip_stack: list[str] = []
        self._in_title = 0
        self._in_a = 0
        self._link_href = ""
        self._link_parts: list[str] = []

    # -- context helpers --------------------------------------------------

    def _meta(self, attrs: list[tuple[str, str | None]]) -> tuple[str, str]:
        name = ""
        content = ""
        for key, value in attrs:
            k = (key or "").lower()
            v = (value or "").strip()
            if k == "name":
                name = v.lower()
            elif k == "property":
                name = v.lower()
            elif k == "content":
                content = v
        return name, content

    def _rel(self, attrs: list[tuple[str, str | None]]) -> set[str]:
        for key, value in attrs:
            if (key or "").lower() == "rel":
                return set((value or "").lower().split())
        return set()

    # -- parser callbacks -------------------------------------------------

    def _flush_link(self) -> None:
        """Record a pending anchor, even if its close tag never came."""
        if self._link_href:
            self.page.links.append((self._link_href, "".join(self._link_parts).strip()))
        self._link_href = ""
        self._link_parts = []

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        tag = tag.lower()
        if tag in self._SKIP:
            self._skip_stack.append(tag)
            return
        names = {k.lower(): (v or "").strip() for k, v in attrs}
        if tag == "html":
            self.page.lang = names.get("lang", "")
        elif tag == "title":
            self._in_title += 1
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self.page.headings.append(tag)
        elif tag == "img":
            alt = names.get("alt", "")
            self.page.images.append(alt)
        elif tag == "a":
            if self._in_a > 0:
                self._flush_link()
            self._in_a += 1
            self._link_href = names.get("href", "")
            self._link_parts = []
        elif tag == "meta":
            name, content = self._meta(list(attrs))
            if name == "description":
                self.page.meta_description = content
            elif name == "viewport" and content:
                self.page.viewport = True
            elif name in _META_ROBOTS:
                if "noindex" in content.lower():
                    self.page.robots_noindex = True
                if "nofollow" in content.lower():
                    self.page.robots_nofollow = True
            elif name == "og:title" and content:
                self.page.has_og_title = True
            elif name == "og:description" and content:
                self.page.has_og_description = True
        elif tag == "link" and "canonical" in self._rel(list(attrs)):
            if names.get("href"):
                self.page.canonical = names["href"]

    def handle_startendtag(self, tag: str, attrs: Any) -> None:
        tag = tag.lower()
        if tag == "img":
            names = {k.lower(): (v or "").strip() for k, v in attrs}
            self.page.images.append(names.get("alt", ""))
        elif tag == "link" and "canonical" in self._rel(list(attrs)):
            names = {k.lower(): (v or "").strip() for k, v in attrs}
            if names.get("href"):
                self.page.canonical = names["href"]

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in self._SKIP:
            if self._skip_stack and self._skip_stack[-1] == tag:
                self._skip_stack.pop()
            return
        if tag == "title":
            if self._in_title > 0:
                self._in_title -= 1
        elif tag == "a":
            if self._in_a > 0:
                self._in_a -= 1
                self._flush_link()

    def handle_data(self, data: str) -> None:
        if self._skip_stack:
            return
        if self._in_title > 0:
            self.page.title += data
        if self._in_a > 0:
            self._link_parts.append(data)

    def close(self) -> None:
        super().close()
        if self._in_a > 0:
            self._flush_link()


def _parse(html: str) -> PageData:
    extractor = _OnPageExtractor()
    try:
        extractor.feed(html)
        extractor.close()
    except Exception:  # pragma: no cover - malformed markup
        pass
    page = extractor.page
    page.title = re.sub(r"\s+", " ", page.title).strip()
    page.meta_description = re.sub(r"\s+", " ", page.meta_description).strip()
    page.word_count = len(html_to_text(html).split())
    return page


def _fetch(url: str) -> tuple[str | None, PageData | None, str | None]:
    """Fetch one URL over the validating pipeline; never raises.

    Returns ``(html, page, error)``; on success ``error`` is None and
    ``page`` carries the fetched URL and status. Any failure returns an
    error message and ``None`` for both payloads.
    """
    url = str(url or "").strip()
    if not url:
        return None, None, "audit failed: no URL given"
    scheme = urlparse(url).scheme.lower()
    if scheme not in ("http", "https"):
        return (
            None,
            None,
            f"audit failed: unsupported scheme '{scheme or 'none'}' - "
            "only http and https can be audited",
        )
    blocked = web._check_url_allowed(url)
    if blocked:
        return None, None, blocked.replace("fetch failed", "audit failed")

    try:
        request = Request(url, headers={"User-Agent": web._USER_AGENT})  # noqa: S310 - validating opener, scheme checked above
        with web.urlopen(request, timeout=web._TIMEOUT) as response:
            raw = response.read(web._MAX_BYTES + 1)
            final_url = response.geturl()
            content_type = response.headers.get("Content-Type", "")
            encoding = response.headers.get_content_charset()
            compressed = response.headers.get("Content-Encoding", "")
            status = getattr(response, "status", None) or getattr(response, "getcode", lambda: None)()
    except HTTPError as exc:
        detail = str(exc.reason) if hasattr(exc, "reason") else str(exc)
        if "blocked" in detail.lower():
            return None, None, detail.replace("fetch failed", "audit failed")
        return None, None, f"audit failed: HTTP {exc.code} {exc.reason} for {web._safe_url(url)}"
    except URLError as exc:
        reason = str(exc.reason) if hasattr(exc, "reason") else str(exc)
        if "blocked" in reason.lower():
            return None, None, reason.replace("fetch failed", "audit failed")
        return None, None, f"audit failed: {exc.reason} for {web._safe_url(url)}"
    except (TimeoutError, OSError) as exc:
        return None, None, f"audit failed: {exc} for {web._safe_url(url)}"

    blocked_final = web._check_url_allowed(final_url)
    if blocked_final:
        return None, None, blocked_final.replace("fetch failed", "audit failed")
    final_scheme = urlparse(final_url).scheme.lower()
    if final_scheme not in ("http", "https"):
        return None, None, f"audit failed: blocked redirect to unsupported scheme {final_scheme!r}"

    if len(raw) > web._MAX_BYTES:
        raw = raw[: web._MAX_BYTES]
    raw, _ = web._inflate(raw, compressed)
    if "html" not in (content_type or "").lower() and not web._looks_textual(content_type):
        return None, None, f"audit failed: {web._safe_url(final_url)} is not an HTML page (content-type {content_type})"
    body = web._decode(raw, encoding if web._looks_textual(content_type) else None, [])
    page = _parse(body)
    page.url = final_url
    page.status = status
    return body, page, None


def _absolute(base: str, href: str) -> str:
    try:
        return urljoin(base, href)
    except ValueError:
        return href


def _audit(page: PageData, base: str) -> list[Finding]:
    findings: list[Finding] = []
    base_host = urlparse(base).hostname or ""

    if not page.title:
        findings.append(Finding("O01", "missing title", "add a descriptive title tag"))
    elif len(page.title) > _TITLE_LONG:
        findings.append(Finding("O02", "title too long", f"shorten the title to at most {_TITLE_LONG} characters"))

    if not page.meta_description:
        findings.append(Finding("O03", "missing meta description", "add a meta description summary"))

    if not page.canonical:
        findings.append(Finding("O04", "missing canonical link", "point a canonical link at the page's own URL"))

    if not page.viewport:
        findings.append(Finding("O05", "missing viewport meta", "add a viewport meta tag for mobile rendering"))

    if page.robots_noindex:
        findings.append(Finding("O06", "page is noindex", "remove noindex if the page should be found"))

    h1 = [h for h in page.headings if h == "h1"]
    if not h1:
        findings.append(Finding("O07", "missing h1", "add exactly one h1 that names the page"))
    elif len(h1) > 1:
        findings.append(Finding("O08", "multiple h1", "keep a single h1 per page"))

    if h1 and "h2" not in page.headings:
        findings.append(Finding("O09", "no h2 under h1", "add h2 sections to structure the page"))

    empty_alt = [alt for alt in page.images if _EMPTY_RE.match(alt)]
    if empty_alt:
        findings.append(Finding("O10", "images without alt text", f"add alt text to {len(empty_alt)} image(s)"))

    if page.word_count < _MIN_WORDS:
        findings.append(Finding("O11", "thin content", f"add detail: only {page.word_count} words"))

    if urlparse(base).scheme.lower() == "http":
        findings.append(Finding("O12", "page served over http", "serve the page over https"))

    external = 0
    empty_text = 0
    for href, text in page.links:
        absolute = _absolute(base, href)
        parsed = urlparse(absolute)
        if parsed.scheme in ("http", "https") and parsed.hostname != base_host:
            external += 1
        if not text:
            empty_text += 1
    if external == 0 and page.links:
        findings.append(Finding("O13", "no outbound links", "link to a trusted external source"))
    if empty_text:
        findings.append(Finding("O14", "links without anchor text", f"give text to {empty_text} link(s)"))

    return findings


def _metrics(page: PageData, base: str) -> dict[str, str]:
    h1 = sum(1 for h in page.headings if h == "h1")
    h2 = sum(1 for h in page.headings if h == "h2")
    internal = sum(1 for href, _ in page.links if urlparse(_absolute(base, href)).scheme not in ("http", "https") or urlparse(_absolute(base, href)).hostname == urlparse(base).hostname)
    external = len(page.links) - internal
    return {
        "status": str(page.status or "?"),
        "title": f"{page.title[:_SNIPPET_CHARS] or '(none)'} ({len(page.title)} chars)",
        "meta description": f"{page.meta_description[:_SNIPPET_CHARS] or '(none)'} ({len(page.meta_description)} chars)",
        "canonical": page.canonical or "(none)",
        "viewport": "yes" if page.viewport else "no",
        "noindex": "yes" if page.robots_noindex else "no",
        "h1": str(h1),
        "h2": str(h2),
        "headings": " ".join(page.headings) or "(none)",
        "images": f"{len(page.images)} ({len([a for a in page.images if _EMPTY_RE.match(a)])} missing alt)",
        "links": f"{len(page.links)} ({internal} internal, {external} external)",
        "words": str(page.word_count),
    }


def _render_metrics(label: str, metrics: dict[str, str]) -> list[str]:
    lines = [f"[{label}]"]
    for key, value in metrics.items():
        lines.append(f"  {key}: {value}")
    return lines


def format_report(
    page: PageData,
    findings: list[Finding],
    compare: PageData | None = None,
) -> str:
    base = page.url
    metrics = _metrics(page, base)
    lines: list[str] = [
        f"page_audit: {page.url} (HTTP {page.status}) - {len(findings)} finding(s)"
    ]
    lines.extend(_render_metrics("metrics", metrics))
    if findings:
        lines.append("[findings]")
        for finding in findings[: _MAX_FINDINGS]:
            lines.append(f"  [{finding.rule} {finding.title}] -> {finding.hint}")
    if compare is not None:
        lines.append("[comparison]")
        compare_metrics = _metrics(compare, compare.url)
        keys = list(metrics.keys())
        for key in keys:
            left = metrics.get(key, "")
            right = compare_metrics.get(key, "")
            mark = "  " if left == right else "!="
            lines.append(f"  {mark} {key}: {left} | {right}")
        lines.append(f"  (= equal, != differs)")
    return "\n".join(lines)


class PageAuditTool(Tool):
    """Fetch a page and audit its on-page SEO signals; advisory only."""

    name = "page_audit"
    description = (
        "Fetch a web page and audit its on-page signals for search "
        "visibility: title, meta description, canonical link, viewport, "
        "robots meta, heading structure, image alt text, links, and "
        "content length. With compare_url, audits a second page and shows "
        "the metrics side by side. Returns a bounded, deterministic "
        "report with stable rule ids and fix hints. No score, no "
        "rewriting."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "Absolute http:// or https:// URL to audit",
            },
            "compare_url": {
                "type": "string",
                "description": "Optional absolute URL to audit and compare against",
            },
        },
        "required": ["url"],
    }

    timeout = web._TIMEOUT

    def execute(
        self,
        sandbox: Sandbox,
        url: str,
        compare_url: str = "",
    ) -> str:
        html, page, error = _fetch(url)
        if error:
            return error

        compare = None
        if compare_url:
            _, compare, compare_error = _fetch(compare_url)
            if compare_error:
                return compare_error

        findings = _audit(page, page.url)
        return format_report(page, findings, compare)
