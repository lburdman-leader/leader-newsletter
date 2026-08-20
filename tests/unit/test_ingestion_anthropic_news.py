"""Why ``anthropic-news`` is enabled, and the tripwire that says when to stop.

Anthropic publishes no feed, so the only option is ``scrapling_static``. Nothing
on the page states a date in any standard form: no ``article:published_time``,
no JSON-LD, no ``<time datetime>``, and the rendered text is ``Aug 14, 2026``,
which is neither ISO 8601 nor RFC 2822. The single machine-readable timestamp is
a ``publishedOn`` field inside the escaped Next.js RSC payload, which the
source's ``embedded_date_key`` reads during normalization.

That route works, and these tests prove it against real markup captured on
2026-08-19 in ``tests/fixtures/sources/anthropic-news/``. It is also the most
fragile thing in the source list: it depends on the shape of Anthropic's own
rendering internals rather than on a published contract. **This file is the
tripwire.** When the payload stops looking like the capture, these tests fail --
and that failure, not a silently empty section in the edition, is how the break
gets noticed.

Everything here runs offline; ``tests/conftest.py`` enforces that.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from scrapling import Selector

from newsletter.ingestion.dates import parse_datetime
from newsletter.ingestion.scrapling import ScraplingAdapter
from newsletter.models import DiscoveredArticle, RawArticle, SourceConfig, TopicCategory
from newsletter.normalization.article import (
    NormalizationError,
    extract_embedded_date,
    normalize_article,
)
from tests.conftest import FakeHttpClient, read_fixture

INDEX_URL = "https://www.anthropic.com/news"
WATERMARK_URL = "https://www.anthropic.com/news/claude-text-watermark"

#: The article's own date, verbatim from the captured payload. Distinct from the
#: ``_createdAt`` (2026-08-13) sitting beside it and from the related post's
#: ``publishedOn`` (2026-08-07) further down, so a wrong read cannot coincide
#: with the right one.
WATERMARK_PUBLISHED_AT = datetime(2026, 8, 14, 19, 16, tzinfo=UTC)

#: Derived from the captured DOM. Anthropic's class names are build-hashed
#: (``PublicationList-module-scss-module__KxYrHG__listItem``), so they are
#: unusable as selectors; the stable handle is the href prefix.
SELECTORS = {
    "link": 'a[href^="/news/"]',
    "date": "time",
}


@pytest.fixture
def anthropic_index() -> str:
    return read_fixture("anthropic-news", "index.html")


@pytest.fixture
def anthropic_article() -> str:
    return read_fixture("anthropic-news", "article.html")


def make_source(*, embedded_date_key: str | None = "publishedOn") -> SourceConfig:
    """The live ``anthropic-news`` config, with the date key overridable."""
    return SourceConfig(
        id="anthropic-news",
        name="Anthropic News",
        entrypoint=INDEX_URL,
        strategy="scrapling_static",
        priority=7,
        category_hint=TopicCategory.AI_MODELS,
        enabled=True,
        selectors=SELECTORS,
        embedded_date_key=embedded_date_key,
    )


def make_raw(html: str, retrieved_at: datetime) -> RawArticle:
    return RawArticle(
        source_id="anthropic-news",
        url=WATERMARK_URL,
        final_url=WATERMARK_URL,
        raw_content=html,
        retrieved_at=retrieved_at,
        content_type="text/html",
    )


@pytest.fixture
def adapter(anthropic_index: str, anthropic_article: str) -> ScraplingAdapter:
    client = FakeHttpClient({INDEX_URL: anthropic_index, WATERMARK_URL: anthropic_article})
    return ScraplingAdapter(make_source(), http=client)


# --------------------------------------------------------------------------- #
# discovery: links and titles, but deliberately no dates
# --------------------------------------------------------------------------- #


def test_discovery_yields_every_news_item_titled_and_undated(
    adapter: ScraplingAdapter, window
) -> None:
    """``a[href^="/news/"]`` resolves the index to absolute, titled candidates.

    They arrive undated on purpose. The index payload holds one ``publishedOn``
    per listed item with no privileged first position, so a first-match read
    there would stamp every item with the first item's date. Discovery passes
    candidates through undated and lets normalization date them one by one.
    """
    found = adapter.discover(window)

    assert [article.url for article in found] == [
        "https://www.anthropic.com/news/claude-opus-5",
        WATERMARK_URL,
        "https://www.anthropic.com/news/improving-fable-5-s-biology-safeguards",
        "https://www.anthropic.com/news/tino-cuellar",
        "https://www.anthropic.com/news/investigating-incidents-cybersecurity-evals",
    ]
    assert all(isinstance(article, DiscoveredArticle) for article in found)

    titles = [article.title_hint or "" for article in found]
    assert all(titles)
    assert "How Claude" in titles[1] and "text watermark works" in titles[1]

    assert all(article.published_at_hint is None for article in found)


def test_rendered_date_text_is_still_not_parseable() -> None:
    """The visible ``Mon D, YYYY`` text remains unusable, which is why the payload matters."""
    assert parse_datetime("Aug 14, 2026") is None


# --------------------------------------------------------------------------- #
# the date: extracted from the embedded payload
# --------------------------------------------------------------------------- #


def test_embedded_payload_yields_the_articles_own_date(anthropic_article: str) -> None:
    """The first ``publishedOn`` is the article's own, and it means publication.

    Two neighbouring dates in the same payload are traps a sloppy read falls
    into: a ``relatedPosts`` teaser further down, and an ``_createdAt`` sitting
    beside the right key on a different day. Both are named explicitly so a
    failure says which one was picked up.

    Agreeing with the date printed beside the headline is what shows the key
    means "published" rather than merely being internally consistent. Verified
    by hand on 7 live articles from 2026-06-30 to 2026-08-14, pinned here on the
    captured one.
    """
    found = extract_embedded_date(Selector(anthropic_article), "publishedOn")

    assert found != datetime(2026, 8, 7, 1, 0, tzinfo=UTC), "picked up a relatedPosts date"
    assert found != datetime(2026, 8, 13, 23, 49, 28, tzinfo=UTC), "picked up _createdAt"
    assert found == WATERMARK_PUBLISHED_AT

    rendered = datetime.strptime("Aug 14, 2026", "%b %d, %Y").replace(tzinfo=UTC).date()
    assert ">Aug 14, 2026<" in anthropic_article, "the visible date moved; recapture the fixture"
    assert found.date() == rendered


def test_normalization_dates_the_article_from_the_payload(anthropic_article: str, window) -> None:
    """End to end: an Anthropic article now normalizes, dated and inside the window."""
    article = normalize_article(make_raw(anthropic_article, window.end), make_source(), hint=None)

    assert article.published_at == WATERMARK_PUBLISHED_AT
    assert window.contains(article.published_at)
    assert article.canonical_url == WATERMARK_URL


# --------------------------------------------------------------------------- #
# failing safe: a missing or unusable key must never produce a guess
# --------------------------------------------------------------------------- #


def test_missing_key_yields_none_rather_than_a_guess(anthropic_article: str) -> None:
    """A key that is not in the payload returns None. Nothing is inferred."""
    assert extract_embedded_date(Selector(anthropic_article), "firstPublishedAt") is None


def test_payload_without_the_key_fails_the_article_explicitly(
    anthropic_article: str, window
) -> None:
    """Rule 7: the article is refused, and the reason names the key an operator must fix."""
    broken = anthropic_article.replace("publishedOn", "renamedByAFrameworkUpgrade")

    with pytest.raises(NormalizationError) as raised:
        normalize_article(make_raw(broken, window.end), make_source(), hint=None)

    assert "publishedOn" in raised.value.reason
    assert "refusing to invent one" in raised.value.reason


def test_a_non_string_value_is_refused_instead_of_read_past(window) -> None:
    """A key that stops holding a string must not fall through to the next record.

    This is the dangerous failure: sliding past a ``null`` would silently return
    a *related post's* date, which is a plausible-looking wrong answer rather
    than an obvious break.
    """
    payload = (
        '<script>self.__next_f.push([1,"{\\"publishedOn\\":null,'
        '\\"relatedPosts\\":[{\\"publishedOn\\":\\"2026-08-07T01:00:00.000Z\\"}]}"])</script>'
    )

    assert extract_embedded_date(Selector(payload), "publishedOn") is None


def test_source_without_the_key_configured_does_not_scan(anthropic_article: str, window) -> None:
    """The mechanism is opt-in: an unconfigured source behaves exactly as before."""
    with pytest.raises(NormalizationError, match="no publication date"):
        normalize_article(
            make_raw(anthropic_article, window.end),
            make_source(embedded_date_key=None),
            hint=None,
        )
