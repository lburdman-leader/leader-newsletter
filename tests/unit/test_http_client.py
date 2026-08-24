"""HTTP transport tests, including the scheme boundary and the offline guard."""

from __future__ import annotations

import gzip
import http.client
import ssl
import urllib.request
import zlib
from pathlib import Path

import pytest

from newsletter.ingestion.http import (
    HttpError,
    HttpResponse,
    UrllibHttpClient,
    build_ssl_context,
    decode_payload,
)


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://example.com/x",
        "javascript:alert(1)",
        "/relative/path",
        "",
    ],
)
def test_unsupported_schemes_are_refused_before_any_request(url: str) -> None:
    """A feed can contain any string; the transport is the last line of defence."""
    with pytest.raises(HttpError, match="unsupported URL"):
        UrllibHttpClient().get(url)


def test_the_offline_guard_actually_blocks_real_connections() -> None:
    """Proves the autouse `no_network` fixture is not a no-op."""
    with pytest.raises((RuntimeError, HttpError)):
        UrllibHttpClient(timeout=0.1).get("https://example.invalid/definitely-not-fetched")


@pytest.mark.parametrize(
    "failure",
    [
        http.client.IncompleteRead(b"half a body"),
        http.client.RemoteDisconnected("closed without response"),
        http.client.BadStatusLine("garbage"),
    ],
    ids=["truncated body", "closed early", "bad status line"],
)
def test_a_malformed_response_is_an_http_error_and_not_an_escape(
    monkeypatch: pytest.MonkeyPatch, failure: Exception
) -> None:
    """The malformed-response family must not escape the transport boundary.

    ``http.client.HTTPException`` is not an ``OSError``, so before this it slipped
    past every clause in ``get`` and past the per-article ``except (AdapterError,
    HttpError)`` in ingestion. One truncated body then aborted an entire live run,
    which is precisely what the failure-isolation rule forbids. A TLS-inspecting
    proxy produces these routinely.
    """

    class _Failing:
        def read(self, _amt: int) -> bytes:
            raise failure

        def geturl(self) -> str:
            return "https://x.example/a"

        def __enter__(self) -> _Failing:
            return self

        def __exit__(self, *_: object) -> None:
            return None

    monkeypatch.setattr(
        "newsletter.ingestion.http.urllib.request.urlopen",
        lambda *a, **k: _Failing(),
    )

    with pytest.raises(HttpError, match="malformed response"):
        UrllibHttpClient(block_private_hosts=False).get("https://x.example/a")


def test_content_type_ignores_charset_and_case() -> None:
    response = HttpResponse(
        url="https://x.example/a",
        final_url="https://x.example/a",
        status=200,
        text="<html></html>",
        headers={"Content-Type": "Application/RSS+XML; charset=UTF-8"},
    )
    assert response.content_type == "application/rss+xml"


def test_content_type_defaults_when_the_header_is_absent() -> None:
    response = HttpResponse(
        url="https://x.example/a", final_url="https://x.example/a", status=200, text=""
    )
    assert response.content_type == "text/html"


def test_metadata_is_small_and_serializable() -> None:
    response = HttpResponse(
        url="https://x.example/a",
        final_url="https://x.example/a",
        status=201,
        text="",
        headers={"Content-Type": "text/html"},
    )
    assert response.metadata == {"status": 201, "content_type": "text/html"}


# --------------------------------------------------------------------------- #
# content encoding — a live feed taught us this one
# --------------------------------------------------------------------------- #


def test_gzip_is_decompressed_even_though_identity_was_requested() -> None:
    """A real feed returns gzip regardless of Accept-Encoding.

    Without this, the body decodes to replacement characters and the failure
    surfaces as a bogus 'malformed feed' error several layers away.
    """
    body = "<rss><channel><title>Feed</title></channel></rss>"
    assert decode_payload(gzip.compress(body.encode()), content_encoding="gzip") == body


def test_deflate_is_decompressed_in_both_framings() -> None:
    body = "<rss/>"
    assert decode_payload(zlib.compress(body.encode()), content_encoding="deflate") == body

    compressor = zlib.compressobj(wbits=-zlib.MAX_WBITS)
    raw = compressor.compress(body.encode()) + compressor.flush()
    assert decode_payload(raw, content_encoding="deflate") == body


@pytest.mark.parametrize("encoding", ["", "identity", "IDENTITY"])
def test_uncompressed_bodies_pass_straight_through(encoding: str) -> None:
    assert decode_payload(b"<rss/>", content_encoding=encoding) == "<rss/>"


def test_an_unsupported_encoding_fails_loudly() -> None:
    with pytest.raises(HttpError, match="unsupported content encoding"):
        decode_payload(b"\x00\x01", content_encoding="br")


def test_corrupt_compression_is_reported_as_a_transport_error() -> None:
    with pytest.raises(HttpError, match="could not decompress"):
        decode_payload(b"not actually gzip", content_encoding="gzip")


def test_a_decompression_bomb_is_refused() -> None:
    """The size cap applies after decompression, not just to the wire bytes."""
    payload = gzip.compress(b"x" * 10_000)
    with pytest.raises(HttpError, match="decompressed response exceeds"):
        decode_payload(payload, content_encoding="gzip", max_bytes=1_000)


def test_an_unknown_charset_falls_back_to_utf8() -> None:
    assert decode_payload("café".encode(), charset="not-a-charset") == "café"


# --------------------------------------------------------------------------- #
# TLS trust
# --------------------------------------------------------------------------- #


def test_nothing_configured_means_the_standard_context() -> None:
    """None, not a context we assembled: the default install stays the default."""
    assert build_ssl_context() is None
    assert UrllibHttpClient().ssl_context is None


def test_relaxing_strict_x509_keeps_verification_and_hostname_checking_on() -> None:
    """The one thing that may be relaxed, and everything that may not.

    The middlebox CA on a TLS-inspecting corporate network is trusted but omits
    an Authority Key Identifier, which ``VERIFY_X509_STRICT`` rejects. Waiving
    that is the whole fix; waiving verification would be a different program.
    """
    context = build_ssl_context(relax_x509_strict=True)

    assert context is not None
    assert not context.verify_flags & ssl.VERIFY_X509_STRICT
    assert context.verify_mode is ssl.CERT_REQUIRED
    assert context.check_hostname is True


def test_a_context_that_would_not_verify_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """The invariant is asserted in code, not merely absent from the config schema."""
    unsafe = ssl.create_default_context()
    unsafe.check_hostname = False
    unsafe.verify_mode = ssl.CERT_NONE
    monkeypatch.setattr(ssl, "create_default_context", lambda *a, **k: unsafe)

    with pytest.raises(ValueError, match="does not verify"):
        build_ssl_context(relax_x509_strict=True)


def test_a_ca_bundle_that_is_not_there_fails_loudly(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="CA bundle does not exist"):
        build_ssl_context(ca_bundle=tmp_path / "corporate-ca.pem")


def test_the_configured_context_reaches_the_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    """Injected exactly like ``resolver``, and actually handed to ``urlopen``."""
    context = build_ssl_context(relax_x509_strict=True)
    seen: dict[str, object] = {}

    def fake_urlopen(request: object, **kwargs: object) -> object:
        seen.update(kwargs)
        raise http.client.RemoteDisconnected("far enough")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    client = UrllibHttpClient(ssl_context=context, resolver=lambda host: ["93.184.216.34"])

    with pytest.raises(HttpError):
        client.get("https://example.com/feed")

    assert seen["context"] is context
