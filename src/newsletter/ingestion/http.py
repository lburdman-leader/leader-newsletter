"""HTTP transport for ingestion.

A deliberately small protocol so that every adapter can be tested offline against
fixtures: tests inject a fake client, production injects :class:`UrllibHttpClient`.

The transport is also a security boundary. Feeds and index pages are untrusted,
and they contain URLs we will follow, so the scheme is validated on every request
and on every redirect target -- ``file:``, ``ftp:`` and friends never reach the
network layer.
"""

from __future__ import annotations

import gzip
import http.client
import ipaddress
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
import zlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from newsletter.models import validate_public_url

DEFAULT_USER_AGENT = (
    "WeeklyIntelligenceNewsletter/0.1 (+https://example.invalid/newsletter; contact: ops)"
)
DEFAULT_TIMEOUT_SECONDS = 30.0
#: Refuse to buffer an unbounded response into memory.
DEFAULT_MAX_BYTES = 5_000_000

#: Resolves a hostname to a list of IP strings. Injectable so tests never do DNS.
Resolver = Callable[[str], list[str]]


class HttpError(Exception):
    """Any transport-level failure, including non-2xx responses."""

    def __init__(self, url: str, message: str, *, status: int | None = None) -> None:
        super().__init__(f"{message} ({url})")
        self.url = url
        self.status = status


@dataclass(frozen=True)
class HttpResponse:
    """A fetched document, decoded to text."""

    url: str
    final_url: str
    status: int
    text: str
    headers: Mapping[str, str] = field(default_factory=dict)

    @property
    def content_type(self) -> str:
        raw = self.headers.get("Content-Type") or self.headers.get("content-type") or ""
        return raw.split(";")[0].strip().lower() or "text/html"

    @property
    def metadata(self) -> dict[str, object]:
        """Small, JSON-serializable provenance for the run manifest."""
        return {"status": self.status, "content_type": self.content_type}


class HttpClient(Protocol):
    """Minimal GET client."""

    def get(self, url: str) -> HttpResponse: ...


# --------------------------------------------------------------------------- #
# TLS
# --------------------------------------------------------------------------- #


def build_ssl_context(
    *,
    ca_bundle: str | Path | None = None,
    relax_x509_strict: bool = False,
) -> ssl.SSLContext | None:
    """The TLS context the transport should use, or None for the standard one.

    Exists for one situation: a corporate network whose middlebox terminates and
    re-signs TLS. Its CA is installed in the machine trust store, so the chain is
    genuinely trusted, but the certificate predates RFC 5280's Authority Key
    Identifier requirement -- and Python 3.13 turned :data:`ssl.VERIFY_X509_STRICT`
    on by default, so every fetch fails with ``unable to get local issuer
    certificate`` on a network where a browser is perfectly happy.

    **This is not a way to skip verification.** Certificate verification and
    hostname checking stay on in every configuration this function can produce;
    there is no argument that turns either off, and the result is asserted before
    it is returned. The only thing ``relax_x509_strict`` relaxes is the strict
    *formatting* rules RFC 5280 puts on a certificate -- the chain must still
    build to a trusted root and still match the host.

    Returns None when nothing is configured, so the default install keeps using
    the context ``urlopen`` builds for itself rather than one we assembled.
    """
    if not ca_bundle and not relax_x509_strict:
        return None

    context = ssl.create_default_context()

    if ca_bundle:
        path = Path(ca_bundle)
        if not path.is_file():
            raise ValueError(f"CA bundle does not exist: {path}")
        try:
            # Additive: `create_default_context` has already loaded the system
            # store, so an extra bundle widens trust rather than replacing it.
            context.load_verify_locations(cafile=str(path))
        except (OSError, ssl.SSLError) as exc:
            raise ValueError(f"CA bundle is not a usable PEM file: {path}: {exc}") from exc

    if relax_x509_strict:
        context.verify_flags &= ~ssl.VERIFY_X509_STRICT

    # The invariant, asserted rather than assumed: whatever the flags above did,
    # a chain is still verified and a hostname is still matched.
    if context.verify_mode is not ssl.CERT_REQUIRED or not context.check_hostname:
        raise ValueError(
            "refusing to build a TLS context that does not verify certificates and hostnames"
        )
    return context


class UrllibHttpClient:
    """Standard-library HTTP client.

    Scrapling is the parsing engine (see ``ingestion/scrapling.py``); its browser
    fetchers live behind the ``scrapling[fetchers]`` extra and are only needed for
    the dynamic and stealth strategies. For feeds and static pages the standard
    library is sufficient, so the default install stays light (ADR-0012).
    """

    def __init__(
        self,
        *,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        user_agent: str = DEFAULT_USER_AGENT,
        max_bytes: int = DEFAULT_MAX_BYTES,
        block_private_hosts: bool = True,
        resolver: Resolver | None = None,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        self.timeout = timeout
        self.user_agent = user_agent
        self.max_bytes = max_bytes
        self.block_private_hosts = block_private_hosts
        self.resolver = resolver or resolve_host
        #: Injected exactly like ``resolver``: absent means the standard secure
        #: context ``urlopen`` builds for itself, so the default install verifies
        #: certificates against the system trust store and nothing else.
        self.ssl_context = ssl_context

    def get(self, url: str) -> HttpResponse:
        try:
            safe_url = validate_public_url(url)
        except ValueError as exc:
            raise HttpError(url, f"refusing to fetch unsupported URL: {exc}") from exc

        if self.block_private_hosts:
            reason = private_host_reason(safe_url, resolver=self.resolver)
            if reason:
                raise HttpError(safe_url, reason)

        request = urllib.request.Request(
            safe_url,
            headers={
                "User-Agent": self.user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml,"
                "application/rss+xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en",
                # We do not want compression; some servers send it regardless,
                # which is why decode_payload still handles it.
                "Accept-Encoding": "identity",
            },
        )

        try:
            with urllib.request.urlopen(
                request, timeout=self.timeout, context=self.ssl_context
            ) as response:
                final_url = response.geturl()
                try:
                    validate_public_url(final_url)
                except ValueError as exc:
                    raise HttpError(safe_url, f"redirected to unsupported URL: {exc}") from exc

                payload = response.read(self.max_bytes + 1)
                if len(payload) > self.max_bytes:
                    raise HttpError(safe_url, f"response exceeds {self.max_bytes} bytes")

                headers = dict(response.headers.items())
                charset = response.headers.get_content_charset() or "utf-8"
                status = getattr(response, "status", 200) or 200
        except urllib.error.HTTPError as exc:
            raise HttpError(safe_url, f"HTTP {exc.code} {exc.reason}", status=exc.code) from exc
        except urllib.error.URLError as exc:
            raise HttpError(safe_url, f"connection failed: {exc.reason}") from exc
        except TimeoutError as exc:
            raise HttpError(safe_url, f"timed out after {self.timeout}s") from exc
        except http.client.HTTPException as exc:
            # A truncated chunked body, a bad status line, an over-long header:
            # the malformed-response family. HTTPException is not an OSError, so
            # without this it escapes the transport boundary entirely and one bad
            # article aborts the whole run instead of being recorded and skipped.
            # TLS-inspecting middleboxes produce these routinely.
            raise HttpError(safe_url, f"malformed response: {type(exc).__name__}: {exc}") from exc
        except OSError as exc:
            raise HttpError(safe_url, f"transport error: {exc}") from exc

        text = decode_payload(
            payload,
            content_encoding=headers.get("Content-Encoding", ""),
            charset=charset,
            max_bytes=self.max_bytes,
            url=safe_url,
        )

        return HttpResponse(
            url=safe_url,
            final_url=final_url,
            status=status,
            text=text,
            headers=headers,
        )


def decode_payload(
    payload: bytes,
    *,
    content_encoding: str = "",
    charset: str = "utf-8",
    max_bytes: int = DEFAULT_MAX_BYTES,
    url: str = "",
) -> str:
    """Decompress if needed, then decode to text.

    Found the hard way against a live feed: a server may return
    ``Content-Encoding: gzip`` even when the request asked for ``identity``.
    Without this, the body decodes into replacement characters and the feed
    parser reports a malformed document — a confusing symptom for a transport
    problem.
    """
    encoding = content_encoding.strip().lower()

    if encoding == "gzip":
        try:
            payload = gzip.decompress(payload)
        except (OSError, EOFError, zlib.error) as exc:
            raise HttpError(url, f"could not decompress a gzip response: {exc}") from exc
    elif encoding == "deflate":
        try:
            payload = zlib.decompress(payload)
        except zlib.error:
            try:  # raw deflate, without the zlib header
                payload = zlib.decompress(payload, -zlib.MAX_WBITS)
            except zlib.error as exc:
                raise HttpError(url, f"could not decompress a deflate response: {exc}") from exc
    elif encoding and encoding not in {"identity", ""}:
        raise HttpError(url, f"unsupported content encoding {encoding!r}")

    if len(payload) > max_bytes:
        raise HttpError(url, f"decompressed response exceeds {max_bytes} bytes")

    try:
        return payload.decode(charset, errors="replace")
    except LookupError:
        return payload.decode("utf-8", errors="replace")


# --------------------------------------------------------------------------- #
# private-network guard
# --------------------------------------------------------------------------- #


def resolve_host(host: str) -> list[str]:
    """Resolve a hostname to its IP addresses. Returns [] when it cannot."""
    try:
        infos = socket.getaddrinfo(host, None)
    except (socket.gaierror, UnicodeError, OSError):
        return []
    return [info[4][0] for info in infos]


def _is_public_address(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def private_host_reason(url: str, *, resolver: Resolver = resolve_host) -> str | None:
    """Return why ``url`` points somewhere it must not, or None when it is safe.

    Any URL the system fetches may have come from a stranger -- a feed entry, a
    scraped link, or a reader submission. Without this, such a URL could reach a
    loopback service, a private network address or a cloud metadata endpoint, and
    the fetched body would then be summarised into a newsletter. Refusing to
    resolve into private space is the only cheap defence.
    """
    host = urllib.parse.urlsplit(url).hostname
    if not host:
        return "URL has no host"

    try:  # a literal IP needs no resolution
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return (
            None if _is_public_address(host) else f"refusing to fetch a non-public address: {host}"
        )

    addresses = resolver(host)
    if not addresses:
        return f"host does not resolve: {host}"
    if not all(_is_public_address(address) for address in addresses):
        return f"host resolves to a non-public address: {host}"
    return None
