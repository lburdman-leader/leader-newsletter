"""Shared test support.

Ingestion tests must never touch the network, so every adapter takes an injected
HTTP client and the tests supply :class:`FakeHttpClient`, driven by the fixtures
in ``tests/fixtures/sources/``.
"""

from __future__ import annotations

import socket
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from newsletter.ingestion.http import HttpError, HttpResponse
from newsletter.intelligence.client import StructuredClient
from newsletter.models import DateWindow

FIXTURES = Path(__file__).parent / "fixtures"
SOURCE_FIXTURES = FIXTURES / "sources"

#: The window every ingestion fixture is written against (ISO week 2026-W34).
WINDOW_START = datetime(2026, 8, 11, tzinfo=UTC)
WINDOW_END = datetime(2026, 8, 19, tzinfo=UTC)


class FakeHttpClient:
    """Deterministic offline HTTP client.

    ``pages`` maps URL -> body. ``failures`` maps URL -> error message and takes
    precedence, so a test can make exactly one URL fail. Every request is recorded
    in ``calls``, which lets a test assert that no network access happened at all.
    """

    def __init__(
        self,
        pages: dict[str, str] | None = None,
        *,
        failures: dict[str, str] | None = None,
        content_type: str = "text/html",
        redirects: dict[str, str] | None = None,
    ) -> None:
        self.pages = pages or {}
        self.failures = failures or {}
        self.redirects = redirects or {}
        self.content_type = content_type
        self.calls: list[str] = []

    def get(self, url: str) -> HttpResponse:
        self.calls.append(url)
        if url in self.failures:
            raise HttpError(url, self.failures[url], status=500)
        if url not in self.pages:
            raise HttpError(url, "HTTP 404 Not Found", status=404)
        return HttpResponse(
            url=url,
            final_url=self.redirects.get(url, url),
            status=200,
            text=self.pages[url],
            headers={"Content-Type": self.content_type},
        )


def read_fixture(*parts: str) -> str:
    """Read a source fixture, e.g. ``read_fixture("example-feed", "feed.xml")``."""
    return (SOURCE_FIXTURES.joinpath(*parts)).read_text(encoding="utf-8")


@pytest.fixture
def window() -> DateWindow:
    """The window the ingestion fixtures are written against."""
    return DateWindow(start=WINDOW_START, end=WINDOW_END)


@pytest.fixture
def feed_xml() -> str:
    return read_fixture("example-feed", "feed.xml")


@pytest.fixture
def feed_article_html() -> str:
    return read_fixture("example-feed", "article.html")


@pytest.fixture
def index_html() -> str:
    return read_fixture("example-site", "index.html")


@pytest.fixture
def site_article_html() -> str:
    return read_fixture("example-site", "article.html")


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hard guarantee behind AC2 and AC14: the suite never touches the network.

    Any adapter that forgets to use its injected HTTP client fails here loudly
    instead of quietly depending on the Internet in CI.
    """

    def blocked(*args: object, **kwargs: object) -> None:
        raise RuntimeError("network access is not allowed in tests; inject a fake client")

    monkeypatch.setattr(socket, "socket", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)


# --------------------------------------------------------------------------- #
# OpenAI SDK fakes — shared by the analyzer, editor and pipeline tests
# --------------------------------------------------------------------------- #


@dataclass
class FakeContentPart:
    refusal: str | None = None
    text: str = ""


@dataclass
class FakeOutputItem:
    content: list[FakeContentPart] = field(default_factory=list)


@dataclass
class FakeResponse:
    """Shaped like ``openai.types.responses.ParsedResponse``, minus the network."""

    output_parsed: Any = None
    output: list[FakeOutputItem] = field(default_factory=list)
    output_text: str = "free-form text that must never be parsed"


class FakeResponsesAPI:
    """Replays scripted results; the last one repeats if attempts continue."""

    def __init__(self, results: list[Any]) -> None:
        self.results = results
        self.calls: list[dict[str, Any]] = []

    def parse(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        result = self.results[min(len(self.calls), len(self.results)) - 1]
        if isinstance(result, Exception):
            raise result
        return result


class FakeOpenAI:
    def __init__(self, *results: Any) -> None:
        self.responses = FakeResponsesAPI(list(results))


def refusal_response(text: str = "I cannot help with that.") -> FakeResponse:
    return FakeResponse(output=[FakeOutputItem(content=[FakeContentPart(refusal=text)])])


def make_client(*results: Any, **kwargs: Any) -> tuple[StructuredClient, FakeOpenAI, list[float]]:
    """A StructuredClient wired to scripted results, with backoff sleeps captured."""
    slept: list[float] = []
    fake = FakeOpenAI(*results)
    client = StructuredClient(
        fake,
        model=kwargs.pop("model", "gpt-4.1-mini"),
        sleeper=slept.append,
        backoff_seconds=kwargs.pop("backoff_seconds", 0.01),
        **kwargs,
    )
    return client, fake, slept
