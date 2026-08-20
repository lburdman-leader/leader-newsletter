"""HTTP transport tests, including the scheme boundary and the offline guard."""

from __future__ import annotations

import gzip
import http.client
import zlib

import pytest

from newsletter.ingestion.http import (
    HttpError,
    HttpResponse,
    UrllibHttpClient,
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
