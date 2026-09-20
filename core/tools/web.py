"""Web fetch: retrieve URL, extract readable text."""

from __future__ import annotations

import atexit
import concurrent.futures
import http.client
import ipaddress
import re
import socket
import threading
import urllib.parse
import zlib
from typing import Any, ClassVar
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import (
    HTTPHandler,
    HTTPRedirectHandler,
    HTTPSHandler,
    Request,
    build_opener,
    urlopen as stdlib_urlopen,
)

from core.agent.approvals import _redact_sensitive
from core.tools._htmltext import html_to_text
from core.types import Sandbox
from core.types import Tool

# Cap compressed and inflated size to bound memory.
_MAX_BYTES = 2_000_000
_MAX_INFLATED = 4_000_000

_BLOCKED_HOSTS = frozenset(
    {
        "localhost",
        "metadata.google.internal",
        "metadata.google",
        "instance-data",
    }
)

# Timeout balances slow hosts vs hung sockets.
_TIMEOUT = 15

_DEFAULT_MAX_CHARS = 12000

_USER_AGENT = "MANTRA/1.0 (coding harness; +https://github.com/mantra)"

_TEXTUAL = ("text/", "application/json", "application/xml", "application/javascript")

# Cache DNS results to avoid repeated 2s stalls.
_DNS_CACHE: dict[str, tuple[float, bool]] = {}
_DNS_LOCK = threading.Lock()
_DNS_TTL = 300.0


def _decode(raw: bytes, encoding: str | None, note: list[str]) -> str:
    # Charset cascade: declared encoding, then utf-8, then latin-1, which
    # decodes any byte stream and makes the final fallback unreachable.
    for candidate in (encoding, "utf-8", "latin-1"):
        if not candidate:
            continue
        try:
            return raw.decode(candidate)
        except (UnicodeDecodeError, LookupError):
            continue
    note.append("(charset could not be determined; decoded as latin-1)")
    return raw.decode("latin-1", errors="replace")


class _InflatedResult(bytes):
    """Bytes that also unpack as ``(data, truncated)`` for backward compat.

    Tests call ``_inflate`` and expect ``bytes``, while the tool itself
    does ``data, truncated = _inflate(...)``. This type satisfies both.
    """

    def __new__(cls, data: bytes, truncated: bool):
        obj = super().__new__(cls, data)
        obj._truncated = bool(truncated)
        return obj

    def __iter__(self):  # type: ignore[override]
        yield bytes(self)
        yield self._truncated

    def __eq__(self, other: object) -> bool:
        if isinstance(other, (bytes, bytearray)):
            return bytes(self) == bytes(other)
        if isinstance(other, tuple) and len(other) == 2:
            return (bytes(self), self._truncated) == other
        return super().__eq__(other)  # type: ignore[no-any-return]

    def __hash__(self) -> int:
        # Bytes semantics (the truncation flag is a compat side-channel).
        return bytes.__hash__(self)


def _inflate(raw: bytes, content_encoding: str, cap: int = _MAX_INFLATED) -> _InflatedResult:  # type: ignore[return]
    """Undo transport compression, refusing to expand past ``cap``.

    urllib does not do this for you. Decompressing the whole payload
    first and trimming afterwards meant a small, hostile response could
    occupy an arbitrary amount of memory before the cap was ever
    consulted, so the ceiling is applied while the stream is being
    expanded instead.

    Returns ``(data, truncated)`` as an object that is also ``bytes`` for
    backward compatibility with direct ``assertEqual`` checks.
    """
    enc = (content_encoding or "").lower()
    try:
        if "gzip" in enc:
            decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)
        elif "deflate" in enc:
            try:
                decompressor = zlib.decompressobj()
                data = decompressor.decompress(raw, cap + 1)
                if not decompressor.eof and data[:cap + 1] == b"":
                    raise zlib.error("not a zlib stream")
                d, t = _clip(data, cap)
                return _InflatedResult(d, t)
            except zlib.error:
                # Servers that say deflate but send the raw stream.
                decompressor = zlib.decompressobj(-zlib.MAX_WBITS)
        else:
            return _InflatedResult(raw, False)
    except (OSError, zlib.error):
        return _InflatedResult(raw, False)

    try:
        data = decompressor.decompress(raw, cap + 1)
    except zlib.error:
        return _InflatedResult(raw, False)
    d, t = _clip(data, cap)
    return _InflatedResult(d, t)


def _clip(data: bytes, cap: int) -> tuple[bytes, bool]:
    if len(data) > cap:
        return data[:cap], True
    return data, False


def _decode_ip_part(part: str) -> int | None:
    """Decode a single IPv4 octet that may be decimal, hex or octal."""
    part = part.strip()
    if not part:
        return None
    try:
        if part.lower().startswith("0x"):
            return int(part, 16)
        if len(part) > 1 and part[0] == "0" and part.isdigit() and all(c in "01234567" for c in part):
            # Octal encoding like 0177
            return int(part, 8)
        if part.isdigit():
            return int(part, 10)
    except ValueError:
        return None
    return None


def _parse_alternative_ip(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """Try to interpret host as an IP using alternative numeric encodings."""
    host = host.strip()
    if not host:
        return None
    # Single integer form like 2130706433 or 0x7f000001
    if "." not in host and ":" not in host:
        try:
            # Handle hex single integer
            if host.lower().startswith("0x"):
                val = int(host, 16)
            elif host.isdigit():
                val = int(host, 10)
            else:
                return None
            if 0 <= val <= 0xFFFFFFFF:
                # Convert to dotted form
                return ipaddress.IPv4Address(val)
        except ValueError:
            pass
        return None
    # Dotted form with 4 parts, each may be encoded
    if "." in host and ":" not in host:
        parts = host.split(".")
        if len(parts) == 4:
            decoded = []
            for p in parts:
                v = _decode_ip_part(p)
                if v is None or not 0 <= v <= 255:
                    return None
                decoded.append(str(v))
            try:
                return ipaddress.IPv4Address(".".join(decoded))
            except ValueError:
                return None
        # Handle 2 or 3 part forms like 127.1 or 10.1
        if 1 < len(parts) < 4:
            # Last part may represent multiple octets
            decoded_first = []
            for p in parts[:-1]:
                v = _decode_ip_part(p)
                if v is None or not 0 <= v <= 255:
                    return None
                decoded_first.append(v)
            last = _decode_ip_part(parts[-1])
            if last is None:
                return None
            # Expand last part into remaining octets
            remaining = 4 - len(decoded_first)
            vals = []
            for i in range(remaining - 1, -1, -1):
                vals.append((last >> (i * 8)) & 0xFF)
            full = decoded_first + vals
            try:
                return ipaddress.IPv4Address(".".join(str(x) for x in full))
            except ValueError:
                return None
    return None


# Shared, bounded DNS resolver pool: stalled lookups occupy a worker
# instead of abandoning a daemon thread per fetch.
# Non-daemon workers are intentional: daemon threads would be killed
# mid-lookup at interpreter exit. Use atexit to join idle workers cleanly.
_RESOLVER_POOL = concurrent.futures.ThreadPoolExecutor(
    max_workers=4, thread_name_prefix="mantra-dns"
)
atexit.register(_RESOLVER_POOL.shutdown, wait=False, cancel_futures=True)


def _resolve_limited(host: str, timeout: float) -> list[tuple] | None:
    """Resolve ``host`` with a hard wall-clock bound.

    The lookup runs on a shared, bounded thread pool, so a stalled
    resolver costs exactly the timeout instead of the operating system's
    full retry window — without leaking a daemon thread per fetch.
    Returns the address list, or None on timeout or failure.
    """
    future = _RESOLVER_POOL.submit(
        socket.getaddrinfo, host, None,
        family=socket.AF_UNSPEC, type=socket.SOCK_STREAM,
    )
    try:
        results = future.result(timeout=timeout)
    except (OSError, UnicodeError, ValueError, TimeoutError):
        # resolver failure treated as "no addresses"; caller re-checks
        return None
    return results or None


def _is_private_hostname(hostname: str | None, resolve: bool = True) -> bool:
    if not hostname:
        return False
    host = hostname.lower().strip().rstrip(".")
    if host in _BLOCKED_HOSTS:
        return True
    if host == "0.0.0.0" or host == "::1":
        return True
    # Try alternative encodings before standard literal check
    alt_ip = _parse_alternative_ip(host)
    if alt_ip is not None:
        return alt_ip.is_private or alt_ip.is_loopback or alt_ip.is_link_local or alt_ip.is_reserved or alt_ip.is_multicast
    # literal IP checks without DNS
    try:
        ip = ipaddress.ip_address(host)
        return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast
    except ValueError:
        pass
    # metadata service IP as literal
    if host in ("169.254.169.254", "169.254.169.253", "fd00::", "fe80::"):
        return True
    # private range hostnames that are IP-like
    if host.startswith("10.") or host.startswith("192.168."):
        return True
    if host.startswith("172."):
        try:
            second = int(host.split(".")[1])
            if 16 <= second <= 31:
                return True
        except (IndexError, ValueError):
            pass
    if not resolve:
        # Literal and encoded forms only. The fetch path passes resolve=False
        # so name resolution has exactly one authority: the pinned connection.
        return False
    # DNS check with cache to avoid repeated stalls.
    try:
        if re.match(r"^[a-z0-9.-]+$", host):
            import time as _time
            now = _time.monotonic()
            # The cache is consulted from resolver threads and the fetch
            # thread alike, so every touch is guarded.
            with _DNS_LOCK:
                cached = _DNS_CACHE.get(host)
                if cached and now - cached[0] < _DNS_TTL:
                    return cached[1]
            result = False
            infos = _resolve_limited(host, 2.0)
            if infos is None:
                # A stalled resolution is not a rebinding primitive:
                # rebinding needs a *successful* public answer here and
                # a private one at connect time. Blocking every slow or
                # unresolvable host would deny legitimate fetches, so
                # the fetch proceeds and fails naturally on a bad name;
                # the per-hop redirect checks and the final-URL re-check
                # below remain the gate for actual private targets.
                return False
            for _family, _, _, _, sockaddr in infos:
                addr = sockaddr[0]
                try:
                    ip = ipaddress.ip_address(addr.split("%")[0])
                    if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
                        result = True
                        break
                except ValueError:
                    continue
            # Bound the cache: an attacker supplying many unique
            # hostnames must not grow it without limit.
            with _DNS_LOCK:
                if len(_DNS_CACHE) >= 1024:
                    for stale_host in list(_DNS_CACHE.keys())[:256]:
                        _DNS_CACHE.pop(stale_host, None)
                _DNS_CACHE[host] = (now, result)
            if result:
                return True
    except (OSError, UnicodeError, ValueError):
        pass  # inconclusive resolution proceeds; the pinned connect below is the gate
    return False


_CRED_QUERY_PARAM = re.compile(
    r"(?i)\b(signature|sig|credential|accesskeyid|access[_-]?key|token|api[_-]?key|apikey|secret|passw(or)?d|authorization)\b"
)


def _redact_query(url: str) -> str:
    """Redact credential values in a URL query.

    Redaction is applied per query parameter so no value pattern can
    consume across '&' separators (the approvals redactor's value regex
    would otherwise eat every following parameter).
    """
    try:
        parsed = urllib.parse.urlsplit(url)
        if not parsed.query:
            return url
    except ValueError:
        return url
    kept: list[str] = []
    changed = False
    for pair in parsed.query.split("&"):
        if not pair:
            continue
        name = pair.split("=", 1)[0]
        if _CRED_QUERY_PARAM.search(name):
            redacted = f"{name}=[REDACTED]"
        else:
            redacted = _redact_sensitive(pair)
        kept.append(redacted)
        if redacted != pair:
            changed = True
    if not changed:
        return url
    return parsed._replace(query="&".join(kept)).geturl()


def _safe_url(url: str) -> str:
    """URL safe to embed in the model transcript.

    Userinfo is stripped from the netloc (a URL may legitimately carry
    credentials for the fetch itself, but they must never be echoed) and
    credential-named query parameters have their values redacted. Signed
    redirect targets are covered by the query redaction.
    """
    try:
        parsed = urllib.parse.urlsplit(url)
        if parsed.username is not None or parsed.password is not None:
            host = parsed.hostname or ""
            if ":" in host and not host.startswith("["):
                host = f"[{host}]"
            netloc = host
            if parsed.port is not None:
                netloc = f"{netloc}:{parsed.port}"
            url = parsed._replace(netloc=netloc).geturl()
    except ValueError:
        pass
    return _redact_query(url)


def _check_url_allowed(url: str) -> str | None:
    try:
        parsed = urlparse(url)
    except ValueError:
        return f"fetch failed: malformed URL {_safe_url(url)!r}"
    scheme = (parsed.scheme or "").lower()
    if scheme not in ("http", "https"):
        return f"fetch failed: unsupported scheme '{parsed.scheme or 'none'}' - only http and https can be fetched"
    # Fully decode host (handles double encoding).
    def _fully_decode(s: str) -> str:
        prev = s
        for _ in range(5):
            cur = urllib.parse.unquote(prev)
            if cur == prev:
                break
            prev = cur
        return prev
    hostname = parsed.hostname
    if hostname:
        try:
            hostname = _fully_decode(hostname)
        except (UnicodeError, ValueError):
            pass  # undecodable host: the raw form is still checked below
    if _is_private_hostname(hostname, resolve=False):
        return f"fetch failed: blocked private or internal host {parsed.hostname!r}"
    if not hostname and parsed.netloc:
        try:
            raw = parsed.netloc.split("@")[-1].split(":")[0]
            netloc = _fully_decode(raw)
            if _is_private_hostname(netloc, resolve=False):
                return f"fetch failed: blocked private or internal host {netloc!r}"
        except (UnicodeError, ValueError):
            pass  # same: raw netloc form was already checked
    return None


# Test hook: the test suite replaces this name to simulate HTTP
# responses without touching the network. It is bound to the validating
# opener below, once the handler it depends on has been defined.
urlopen = stdlib_urlopen


def _resolve_and_pin(hostname: str) -> str | None:
    """Resolve ``hostname`` once and return an address safe to dial.

    The single resolution is both the check and the connection target:
    validating one answer and then dialing a second, independent
    resolution is the classic DNS-rebinding window, so the address that
    was validated is the address the transport must use.

    Returns the pinned address, or None when the host resolves to
    anything private (or does not resolve) — the caller must block.
    """
    host = (hostname or "").strip().strip("[]").rstrip(".")
    if not host:
        return None
    infos = _resolve_limited(host, 5.0)
    if not infos:
        return None
    pinned: str | None = None
    for _family, _, _, _, sockaddr in infos:
        addr = sockaddr[0].split("%")[0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            return None
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            return None
        if pinned is None:
            pinned = addr
    return pinned


class _PinnedHTTPConnection(http.client.HTTPConnection):
    """HTTP connection that dials a pre-validated address.

    The Host header, TLS SNI and certificate verification all continue to
    use the original hostname; only the socket's destination is pinned.
    """

    def __init__(self, *args: Any, pinned_ip: str | None = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._pinned_ip = pinned_ip

    def connect(self) -> None:  # type: ignore[override]
        if self._pinned_ip:
            self.sock = socket.create_connection((self._pinned_ip, self.port), self.timeout)
        else:
            super().connect()


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS variant: dial the pinned address, keep SNI + cert checks on the hostname."""

    def __init__(self, *args: Any, pinned_ip: str | None = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._pinned_ip = pinned_ip

    def connect(self) -> None:  # type: ignore[override]
        if not self._pinned_ip:
            super().connect()
            return
        sock = socket.create_connection((self._pinned_ip, self.port), self.timeout)
        # SNI and certificate verification stay bound to the hostname the
        # user asked for, not the pinned address.
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


class _PinningHandler(HTTPRedirectHandler, HTTPHandler, HTTPSHandler):
    """Open requests against an address validated in the same breath.

    Subclassing the default HTTP/HTTPS/redirect handlers makes the opener
    builder skip all three stdlib defaults, so every connection goes
    through the pinned path. Resolves the request host once, refuses any
    private answer, and pins the connection to the validated address —
    closing the check-then-connect rebinding gap that two independent
    resolutions would leave open. Redirects are re-validated by the
    parent class and re-pinned here because each hop becomes a new
    request.
    """

    _BLOCKED_MSG = "blocked: host {host!r} did not resolve to a public address"

    def _pinned_for(self, req: Any) -> str | None:
        hostname = urllib.parse.urlsplit(req.full_url).hostname or ""
        return _resolve_and_pin(hostname)

    def http_open(self, req: Any) -> Any:
        pinned = self._pinned_for(req)
        if pinned is None:
            return self._blocked(req)
        return self.do_open(_partial(_PinnedHTTPConnection, pinned_ip=pinned), req)

    def https_open(self, req: Any) -> Any:
        pinned = self._pinned_for(req)
        if pinned is None:
            return self._blocked(req)
        return self.do_open(_partial(_PinnedHTTPSConnection, pinned_ip=pinned), req)

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        """Reject redirect targets whose scheme is not http/https.

        The stdlib redirect handler follows any Location scheme, which
        would let a public server bounce the fetch to file://, ftp:// or
        data:// with no private-host checks on that hop.
        """
        scheme = urllib.parse.urlsplit(newurl).scheme.lower()
        if scheme not in ("http", "https"):
            raise HTTPError(
                newurl, code,
                f"blocked: redirect to unsupported scheme {scheme!r}",
                headers, fp,
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)

    def _blocked(self, req: Any) -> HTTPError:
        host = urllib.parse.urlsplit(req.full_url).hostname
        return HTTPError(req.full_url, 403, self._BLOCKED_MSG.format(host=host), None, None)


def _partial(cls: Any, **kwargs: Any) -> Any:
    """Bind keyword constructor args, tolerating do_open's call shape.

    ``do_open`` invokes ``cls(host, timeout=req.timeout, **extra)``, which
    is exactly a partial application of ``pinned_ip``.
    """
    import functools

    return functools.partial(cls, **kwargs)


def _make_opener() -> object:
    """Create an opener with safe redirects and pinned DNS.

    Separated for testability: tests can mock this function to return
    a custom opener that simulates responses without network access.
    """
    return build_opener(_PinningHandler())


try:
    # Routed through the validating opener so each hop is checked, not
    # just the URL the chain happens to end on. Tests rebind this name.
    urlopen = _make_opener().open
except Exception:  # pragma: no cover - fail closed, never fetch unvalidated
    # Fail closed: retain redirect and DNS pinning guarantees rather than
    # fetching through an unvalidated opener.
    def urlopen(request, timeout=None):  # type: ignore[no-redef]
        raise URLError("fetch unavailable: secure opener failed to initialise")


class WebFetchTool(Tool):
    """Fetch a URL and return its readable text.

    Returns an error *string* rather than raising, because a failure here
    is information the agent can act on - follow a different link, fix
    the URL, tell the operator the host is down. Raising would end the
    turn with a traceback instead.
    """

    name = "web_fetch"
    description = (
        "Fetch a web page over HTTP(S) and return its readable text. "
        "HTML tags, scripts and styles are stripped. Use it to read "
        "documentation, release notes, issue threads and API references. "
        "Table and page layout is not preserved."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "Absolute http:// or https:// URL to fetch",
            },
            "max_chars": {
                "type": "integer",
                "description": (
                    "Truncate the extracted text after this many characters "
                    f"(default {_DEFAULT_MAX_CHARS})"
                ),
            },
        },
        "required": ["url"],
    }

    timeout = _TIMEOUT

    def execute(
        self,
        sandbox: Sandbox,
        url: str,
        max_chars: int = _DEFAULT_MAX_CHARS,
    ) -> str:
        note: list[str] = []
        url = (url or "").strip()
        if not url:
            return "fetch failed: no URL given"

        scheme = urlparse(url).scheme.lower()
        if scheme not in ("http", "https"):
            return (
                f"fetch failed: unsupported scheme '{scheme or 'none'}' - "
                "only http and https can be fetched"
            )
        blocked = _check_url_allowed(url)
        if blocked:
            return blocked

        try:
            budget = max(0, min(int(max_chars), 200_000))
        except (TypeError, ValueError):
            budget = _DEFAULT_MAX_CHARS
        if budget == 0:
            budget = _DEFAULT_MAX_CHARS

        try:
            request = Request(url, headers={"User-Agent": _USER_AGENT})  # noqa: S310 - validating opener, scheme checked above
            # Through the validating opener, so a redirect to an internal
            # host is refused at that hop instead of being followed and
            # only noticed once the final URL is inspected.
            # Validating opener: scheme, private hosts and redirects are
            # checked per hop; final URL re-checked below.
            with urlopen(request, timeout=self.timeout) as response:
                raw = response.read(_MAX_BYTES + 1)
                final_url = response.geturl()
                content_type = response.headers.get("Content-Type", "")
                encoding = response.headers.get_content_charset()
                compressed = response.headers.get("Content-Encoding", "")
                status = getattr(response, "status", None) or getattr(response, "getcode", lambda: None)()
        except HTTPError as exc:
            # The body of an error response often says which header was
            # wrong, so surface the status and let the agent carry on.
            # Also covers blocked redirects raised by the safe handler.
            detail = str(exc.reason) if hasattr(exc, "reason") else str(exc)
            if "blocked" in detail.lower():
                return detail
            return f"fetch failed: HTTP {exc.code} {exc.reason} for {_safe_url(url)}"
        except URLError as exc:
            reason = str(exc.reason) if hasattr(exc, "reason") else str(exc)
            if "blocked" in reason.lower():
                return reason
            return f"fetch failed: {exc.reason} for {_safe_url(url)}"
        except (TimeoutError, OSError) as exc:
            return f"fetch failed: {exc} for {_safe_url(url)}"

        # The safe redirect handler already blocked private hosts on each hop,
        # but re-check final URL as defence in depth (e.g. for non redirect case).
        blocked_final = _check_url_allowed(final_url)
        if blocked_final:
            return blocked_final
        # Ensure final scheme is still http/https (handler blocks, but check again)
        final_scheme = urlparse(final_url).scheme.lower()
        if final_scheme not in ("http", "https"):
            return f"fetch failed: blocked redirect to unsupported scheme {final_scheme!r}"

        if len(raw) > _MAX_BYTES:
            raw = raw[:_MAX_BYTES]
            note.append(f"(response truncated at {_MAX_BYTES} bytes)")

        raw, inflated_truncated = _inflate(raw, compressed)
        if inflated_truncated:
            note.append(f"(decompressed response truncated at {_MAX_INFLATED} bytes)")
        charset_note: list[str] = []
        body = _decode(raw, encoding if _looks_textual(content_type) else None, charset_note)
        note.extend(charset_note)

        if _looks_textual(content_type) and "html" in content_type.lower():
            body = html_to_text(body)

        body = body.strip()
        if not body:
            return f"fetched {_safe_url(final_url)} (HTTP {status}) but it had no text content"

        if len(body) > budget:
            body = body[:budget].rstrip() + "\n... [truncated]"
            note.append(f"(text truncated at {budget} chars)")

        header = f"{_safe_url(final_url)} (HTTP {status})"
        if note:
            header += " " + " ".join(note)
        return f"{header}\n\n{body}"


def _looks_textual(content_type: str) -> bool:
    """False for types that are certainly not prose (images, archives)."""
    head = (content_type or "").split(";")[0].strip().lower()
    if not head:
        # No declared type: assume text rather than refusing to read it.
        return True
    return head.startswith(_TEXTUAL) or head.endswith("+xml") or head.endswith("+json")
