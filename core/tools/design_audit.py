"""Deterministic design audit for arbitrary web pages.

Combines two evidence sources: the page's raw HTML (structure: linked
stylesheets, inline CSS, image dimensions) and, when a headless browser is
installed, a rendered screenshot analyzed with plain stdlib code (PNG
decoding via zlib, luminance/saturation statistics). No dependency, no
model call, no score. Findings are advisory input for the
``design-audit`` skill, not a gate.
"""

from __future__ import annotations

import os
import shutil
import struct
import subprocess
import tempfile
import zlib
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any, ClassVar
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request

from core.tools import web
from core.types import Sandbox
from core.types import Tool

_BROWSER_CANDIDATES = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
)

_WINDOW = "1024,768"
_VIRTUAL_TIME_BUDGET_MS = 5000
_BROWSER_TIMEOUT = 30

_SCREENSHOT_STRIDE_TARGET = 200_000

_WHITE_LUM = 245.0
_DARK_LUM = 40.0


@dataclass(frozen=True)
class Finding:
    rule: str
    title: str
    hint: str


@dataclass
class StructureData:
    css_links: int = 0
    inline_styles: int = 0
    images_total: int = 0
    images_missing_dims: int = 0


class _StructureExtractor(HTMLParser):
    _SKIP = {"script", "style", "noscript", "template", "svg", "iframe"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.structure = StructureData()
        self._skip_stack: list[str] = []

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        tag = tag.lower()
        if tag in self._SKIP:
            self._skip_stack.append(tag)
            return
        names = {k.lower(): (v or "").strip() for k, v in attrs}
        if tag == "link" and "stylesheet" in (names.get("rel", "")).lower().split():
            if names.get("href"):
                self.structure.css_links += 1
        elif tag == "img":
            self.structure.images_total += 1
            if "width" not in names or "height" not in names:
                self.structure.images_missing_dims += 1

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in self._SKIP:
            if self._skip_stack and self._skip_stack[-1] == tag:
                self._skip_stack.pop()

    def close(self) -> None:
        super().close()
        # Inline <style> blocks are skipped by _SKIP, so _parse_structure
        # counts raw "<style" occurrences as a cheap inline-CSS proxy.


def _parse_structure(html: str) -> StructureData:
    extractor = _StructureExtractor()
    try:
        extractor.feed(html)
        extractor.close()
    except Exception:  # pragma: no cover - malformed markup
        pass
    structure = extractor.structure
    structure.inline_styles = html.lower().count("<style")
    return structure


def _fetch_html(url: str) -> tuple[str | None, str | None, str | None]:
    """Fetch one URL over the validating pipeline; never raises.

    Returns ``(html, final_url, error)``; on success ``error`` is None.
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
    if not web._looks_textual(content_type):
        return None, None, f"audit failed: {web._safe_url(final_url)} is not an HTML page (content-type {content_type})"
    body = web._decode(raw, encoding if web._looks_textual(content_type) else None, [])
    return body, final_url, None


def _find_browser() -> str | None:
    for candidate in _BROWSER_CANDIDATES:
        if os.path.isfile(candidate):
            return candidate
    return None


def _render_screenshot(url: str, out_dir: str) -> str | None:
    """Render the page to PNG with a headless browser; None if unavailable."""
    browser = _find_browser()
    if browser is None:
        return None
    shot = os.path.join(out_dir, "shot.png")
    profile = os.path.join(out_dir, "profile")
    cmd = [
        browser,
        "--headless=new",
        "--disable-gpu",
        "--no-first-run",
        "--no-default-browser-check",
        # Keep results stable across machines: auto dark mode would
        # invert backgrounds and skew luminance metrics.
        "--disable-features=WebContentsForceDark",
        f"--user-data-dir={profile}",
        f"--virtual-time-budget={_VIRTUAL_TIME_BUDGET_MS}",
        f"--window-size={_WINDOW}",
        f"--screenshot={shot}",
        url,
    ]
    kwargs: dict[str, Any] = {"timeout": _BROWSER_TIMEOUT}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
    try:
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kwargs)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if not os.path.isfile(shot) or os.path.getsize(shot) == 0:
        return None
    return shot


def _png_pixels(data: bytes) -> tuple[int, int, Any]:
    """Decode a non-interlaced 8-bit PNG to RGBA pixels.

    Pure stdlib: zlib inflation plus per-scanline unfiltering. Raises
    ValueError for unsupported encodings. Returns ``(width, height,
    pixels)`` where ``pixels`` yields ``(r, g, b, a)`` tuples.
    """
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG file")
    pos = 8
    width = height = None
    bit_depth = color_type = interlace = None
    idat = b""
    while pos < len(data):
        length = struct.unpack(">I", data[pos:pos + 4])[0]
        ctype = data[pos + 4:pos + 8]
        chunk = data[pos + 8:pos + 8 + length]
        if ctype == b"IHDR":
            width, height, bit_depth, color_type, _, _, interlace = struct.unpack(">IIBBBBB", chunk)
        elif ctype == b"IDAT":
            idat += chunk
        pos += 12 + length
    if width is None or height is None or bit_depth != 8 or interlace != 0:
        raise ValueError("unsupported PNG (need 8-bit, non-interlaced)")
    bpp = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(color_type)
    if bpp is None:
        raise ValueError("unsupported PNG color type")
    try:
        raw = zlib.decompress(idat)
    except zlib.error as exc:
        raise ValueError(f"corrupt PNG data: {exc}") from exc

    stride = width * bpp
    lines: list[bytes] = []
    prev = b"\x00" * stride
    offset = 0
    for _ in range(height):
        if offset >= len(raw):
            raise ValueError("truncated PNG scanlines")
        filter_type = raw[offset]
        line = bytearray(raw[offset + 1:offset + 1 + stride])
        if len(line) < stride:
            raise ValueError("truncated PNG scanline")
        offset += 1 + stride
        if filter_type == 1:  # Sub
            for i in range(bpp, stride):
                line[i] = (line[i] + line[i - bpp]) & 0xFF
        elif filter_type == 2:  # Up
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 0xFF
        elif filter_type == 3:  # Average
            for i in range(stride):
                left = line[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + ((left + prev[i]) >> 1)) & 0xFF
        elif filter_type == 4:  # Paeth
            for i in range(stride):
                left = line[i - bpp] if i >= bpp else 0
                up = prev[i]
                up_left = prev[i - bpp] if i >= bpp else 0
                p = left + up - up_left
                pa = abs(p - left)
                pb = abs(p - up)
                pc = abs(p - up_left)
                if pa <= pb and pa <= pc:
                    pred = left
                elif pb <= pc:
                    pred = up
                else:
                    pred = up_left
                line[i] = (line[i] + pred) & 0xFF
        elif filter_type != 0:
            raise ValueError(f"unknown PNG filter {filter_type}")
        lines.append(bytes(line))
        prev = bytes(line)

    def pixels():
        for line in lines:
            for i in range(0, stride, bpp):
                if color_type == 0:
                    yield (line[i], line[i], line[i], 255)
                elif color_type == 2:
                    yield (line[i], line[i + 1], line[i + 2], 255)
                elif color_type == 3:
                    raise ValueError("palette PNG unsupported")
                elif color_type == 4:
                    yield (line[i], line[i], line[i], line[i + 1])
                else:
                    yield (line[i], line[i + 1], line[i + 2], line[i + 3])

    return width, height, pixels()


def _analyze_screenshot(path: str) -> dict[str, float | int]:
    with open(path, "rb") as handle:
        data = handle.read()
    width, height, pixels = _png_pixels(data)
    total = width * height
    stride = max(1, total // _SCREENSHOT_STRIDE_TARGET)
    count = 0
    lum_sum = 0.0
    lum_sq = 0.0
    sat_sum = 0.0
    white = 0
    dark = 0
    for index, (r, g, b, a) in enumerate(pixels):
        if index % stride:
            continue
        lum = 0.2126 * r + 0.7152 * g + 0.0722 * b
        lum_sum += lum
        lum_sq += lum * lum
        mx = max(r, g, b)
        mn = min(r, g, b)
        sat_sum += (mx - mn) / 255.0
        if lum >= _WHITE_LUM:
            white += 1
        if lum <= _DARK_LUM:
            dark += 1
        count += 1
    if count == 0:
        raise ValueError("empty screenshot")
    mean = lum_sum / count
    variance = max(0.0, lum_sq / count - mean * mean)
    return {
        "width": width,
        "height": height,
        "avg luminance": round(mean, 1),
        "luminance spread": round(variance ** 0.5, 1),
        "avg saturation": round(sat_sum / count, 3),
        "blank (white) ratio": round(white / count, 3),
        "dark ratio": round(dark / count, 3),
    }


def _visual_findings(metrics: dict[str, float | int]) -> list[Finding]:
    findings: list[Finding] = []
    spread = float(metrics["luminance spread"])
    blank = float(metrics["blank (white) ratio"])
    dark = float(metrics["dark ratio"])
    # Blank and failed renders are detected by flatness, not by a fixed
    # light baseline: the installed browser may inherit a dark system
    # theme, so a mostly-dark page is not itself a failure.
    if (blank >= 0.85 or dark >= 0.85) and spread < 6:
        findings.append(Finding("D01", "render appears blank", "check that the page paints content after scripts run"))
    if dark >= 0.95 and spread < 8:
        findings.append(Finding("D02", "render appears uniformly dark", "check for a failed render or an all-black screen"))
    if spread < 10 and blank < 0.9:
        findings.append(Finding("D03", "extremely flat contrast", "increase contrast between text and background"))
    return findings


def _structural_findings(structure: StructureData) -> list[Finding]:
    findings: list[Finding] = []
    if structure.css_links == 0 and structure.inline_styles == 0:
        findings.append(Finding("F01", "no CSS found", "link a stylesheet or add inline styles"))
    if structure.images_total >= 2 and structure.images_missing_dims / structure.images_total > 0.5:
        findings.append(
            Finding(
                "F02",
                "images missing dimensions",
                f"add width/height to {structure.images_missing_dims} of {structure.images_total} images to prevent layout shift",
            )
        )
    return findings


def format_report(
    url: str,
    structure: StructureData,
    visual: dict[str, float | int] | None,
    findings: list[Finding],
    browser_note: str,
) -> str:
    lines = [f"design_audit: {url} - {len(findings)} finding(s)"]
    lines.append("[structure]")
    lines.append(f"  stylesheets: {structure.css_links}")
    lines.append(f"  inline <style> blocks: {structure.inline_styles}")
    lines.append(f"  images: {structure.images_total} ({structure.images_missing_dims} missing width/height)")
    if visual is not None:
        lines.append("[visual (headless render)]")
        for key, value in visual.items():
            lines.append(f"  {key}: {value}")
    else:
        lines.append("[visual] " + browser_note)
    if findings:
        lines.append("[findings]")
        for finding in findings:
            lines.append(f"  [{finding.rule} {finding.title}] -> {finding.hint}")
    return "\n".join(lines)


class DesignAuditTool(Tool):
    """Fetch a page, inspect its structure, and render it headless."""

    name = "design_audit"
    description = (
        "Audit a web page's design signals: structure (linked "
        "stylesheets, inline CSS, image dimensions) and, when a headless "
        "browser is installed, a rendered screenshot analyzed for blank "
        "renders, dark pages, and overall contrast. Returns a bounded, "
        "deterministic report with stable rule ids and fix hints. No "
        "score, no rewriting."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "Absolute http:// or https:// URL to audit",
            },
        },
        "required": ["url"],
    }

    timeout = web._TIMEOUT

    def execute(self, sandbox: Sandbox, url: str) -> str:
        html, final_url, error = _fetch_html(url)
        if error:
            return error
        structure = _parse_structure(html or "")

        findings = _structural_findings(structure)

        visual = None
        browser_note = "no headless browser found; structure-only audit"
        tmp = None
        try:
            tmp = tempfile.mkdtemp(prefix="mantra-design-")
            shot = _render_screenshot(final_url, tmp)
            if shot is None:
                browser_note = "headless browser unavailable or render failed; structure-only audit"
            else:
                try:
                    visual = _analyze_screenshot(shot)
                except ValueError as exc:
                    browser_note = f"screenshot could not be decoded: {exc}"
                findings.extend(_visual_findings(visual) if visual is not None else [])
        finally:
            if tmp is not None:
                shutil.rmtree(tmp, ignore_errors=True)

        return format_report(final_url, structure, visual, findings, browser_note)
