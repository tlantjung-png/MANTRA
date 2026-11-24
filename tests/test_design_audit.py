"""Design audit: structural page inspection plus screenshot pixel analysis.

No test here touches the network or launches a browser. ``web.urlopen``
and ``_render_screenshot`` are patched, and PNG fixtures are generated in
memory with plain stdlib code, so the suite stays offline and
deterministic.
"""

from __future__ import annotations

import struct
import sys
import zlib
from unittest import mock

sys.path.insert(0, ".")

from core.tools.design_audit import (  # noqa: E402
    DesignAuditTool,
    _analyze_screenshot,
    _png_pixels,
)
from core.registry import TOOL_REGISTRY  # noqa: E402

STRUCTURED = """<html><head>
<link rel="stylesheet" href="/main.css">
</head><body>
<img src="a.png" width="100" height="50">
<img src="b.png">
<img src="c.png">
</body></html>"""

UNSTYLED = """<html><head></head><body><h1>Hi</h1></body></html>"""


def _make_png(width: int, height: int, pixel_fn, color_type: int = 2, filter_type: int = 0) -> bytes:
    bpp = {0: 1, 2: 3, 4: 2, 6: 4}[color_type]
    raw = b""
    for y in range(height):
        scanline = bytearray()
        for x in range(width):
            scanline += bytes(pixel_fn(x, y))
        if filter_type == 1:
            out = bytearray()
            prev = bytearray(bpp)
            for i in range(0, len(scanline), bpp):
                px = bytes((scanline[i + j] - prev[j]) & 0xFF for j in range(bpp))
                out += px
                prev = scanline[i:i + bpp]
            scanline = out
        raw += bytes([filter_type]) + bytes(scanline)
    ihdr = struct.pack(">IIBBBBB", width, height, 8, color_type, 0, 0, 0)
    idat = zlib.compress(raw)
    def chunk(ctype: bytes, payload: bytes) -> bytes:
        return struct.pack(">I", len(payload)) + ctype + payload + struct.pack(">I", zlib.crc32(ctype + payload) & 0xFFFFFFFF)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


def _white_png(width: int = 8, height: int = 8) -> bytes:
    return _make_png(width, height, lambda x, y: (255, 255, 255))


def _black_png(width: int = 8, height: int = 8) -> bytes:
    return _make_png(width, height, lambda x, y: (0, 0, 0))


def _textured_png(width: int = 8, height: int = 8) -> bytes:
    return _make_png(width, height, lambda x, y: (min(x * 32, 255), min(y * 32, 255), 128))


class _Headers:
    def get(self, key, default=""):
        if key.lower() == "content-type":
            return "text/html; charset=utf-8"
        return default

    def get_content_charset(self):
        return "utf-8"


class _Response:
    def __init__(self, body, url):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self._body = body
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


def _serve(pages: dict[str, str]) -> mock.patch:
    def fake_open(request, timeout=None):
        url = request.full_url
        for prefix, body in pages.items():
            if url.startswith(prefix):
                return _Response(body, url)
        raise OSError(f"unexpected URL {url}")

    return mock.patch("core.tools.web.urlopen", side_effect=fake_open)


def _audit(url: str, screenshot: bytes | None = None) -> str:
    def fake_screenshot(_url, out_dir):
        if screenshot is None:
            return None
        import os
        path = os.path.join(out_dir, "shot.png")
        with open(path, "wb") as handle:
            handle.write(screenshot)
        return path

    with mock.patch("core.tools.design_audit._render_screenshot", side_effect=fake_screenshot):
        return DesignAuditTool().execute(None, url=url)


# --- PNG decoder ----------------------------------------------------------


def test_png_decoder_reads_rgb() -> None:
    png = _make_png(2, 1, lambda x, y: (10, 20, 30))
    width, height, pixels = _png_pixels(png)
    assert (width, height) == (2, 1)
    assert list(pixels) == [(10, 20, 30, 255), (10, 20, 30, 255)]


def test_png_decoder_unfilters_sub() -> None:
    png = _make_png(3, 1, lambda x, y: (x * 40, 100, 200), filter_type=1)
    width, height, pixels = _png_pixels(png)
    assert list(pixels) == [(0, 100, 200, 255), (40, 100, 200, 255), (80, 100, 200, 255)]


def test_png_decoder_rejects_bad_signature() -> None:
    try:
        _png_pixels(b"not a png at all")
    except ValueError:
        return
    raise AssertionError("expected ValueError")


def test_png_decoder_rejects_interlaced() -> None:
    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 1)
    def chunk(ctype, payload):
        return struct.pack(">I", len(payload)) + ctype + payload + struct.pack(">I", zlib.crc32(ctype + payload) & 0xFFFFFFFF)
    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(b"\x00\x01\x02\x03")) + chunk(b"IEND", b"")
    try:
        _png_pixels(png)
    except ValueError:
        return
    raise AssertionError("expected ValueError")


# --- screenshot analysis --------------------------------------------------


def _write_png(tmp_path, data: bytes) -> str:
    path = tmp_path / "shot.png"
    path.write_bytes(data)
    return str(path)


def test_white_screenshot_is_blank(tmp_path) -> None:
    metrics = _analyze_screenshot(_write_png(tmp_path, _white_png()))
    assert metrics["blank (white) ratio"] == 1.0


def test_black_screenshot_is_dark(tmp_path) -> None:
    metrics = _analyze_screenshot(_write_png(tmp_path, _black_png()))
    assert metrics["dark ratio"] == 1.0


def test_textured_screenshot_has_spread(tmp_path) -> None:
    metrics = _analyze_screenshot(_write_png(tmp_path, _textured_png()))
    assert metrics["luminance spread"] > 20
    assert metrics["blank (white) ratio"] < 0.9


# --- tool behavior --------------------------------------------------------


def test_unstyled_page_flags_no_css() -> None:
    with _serve({"https://example.com/plain": UNSTYLED}):
        with mock.patch("core.tools.design_audit._render_screenshot", return_value=None):
            out = DesignAuditTool().execute(None, url="https://example.com/plain")
    assert "[F01 no CSS found]" in out
    assert "browser unavailable" in out or "no headless browser" in out


def test_images_missing_dimensions_flagged() -> None:
    with _serve({"https://example.com/structured": STRUCTURED}):
        with mock.patch("core.tools.design_audit._render_screenshot", return_value=None):
            out = DesignAuditTool().execute(None, url="https://example.com/structured")
    assert "[F02 images missing dimensions]" in out


def test_blank_render_is_flagged() -> None:
    with _serve({"https://example.com/blank": STRUCTURED}):
        out = _audit("https://example.com/blank", screenshot=_white_png(64, 64))
    assert "[D01 render appears blank]" in out


def test_dark_render_is_flagged() -> None:
    with _serve({"https://example.com/dark": STRUCTURED}):
        out = _audit("https://example.com/dark", screenshot=_black_png(64, 64))
    assert "[D02 render appears uniformly dark]" in out


def test_healthy_visual_has_no_d_findings() -> None:
    with _serve({"https://example.com/ok": STRUCTURED}):
        out = _audit("https://example.com/ok", screenshot=_textured_png(64, 64))
    assert "[D01" not in out
    assert "[D02" not in out
    assert "[D03" not in out


def test_dark_theme_with_text_is_not_a_failed_render() -> None:
    """Mostly-dark pages with visible variation are a theme, not a blank."""

    def dark_textured(x, y):
        return (10 + (x * 3) % 40, 10 + (y * 3) % 40, 10)

    png = _make_png(64, 64, dark_textured)
    with _serve({"https://example.com/darktheme": STRUCTURED}):
        out = _audit("https://example.com/darktheme", screenshot=png)
    assert "[D01" not in out
    assert "[D02" not in out


def test_report_is_deterministic() -> None:
    with _serve({"https://example.com/ok": STRUCTURED}):
        first = _audit("https://example.com/ok", screenshot=_textured_png(32, 32))
        second = _audit("https://example.com/ok", screenshot=_textured_png(32, 32))
    assert first == second


def test_unsupported_scheme() -> None:
    assert DesignAuditTool().execute(None, url="ftp://example.com").startswith("audit failed: unsupported scheme")


def test_registered_with_expected_schema() -> None:
    assert TOOL_REGISTRY["design_audit"] is DesignAuditTool
    schema = DesignAuditTool().schema()
    assert schema["function"]["name"] == "design_audit"
    assert "url" in schema["function"]["parameters"]["properties"]
    assert schema["function"]["parameters"]["required"] == ["url"]
