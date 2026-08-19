"""Why ``anthropic-news`` stays disabled, pinned against captured markup.

Anthropic publishes no feed, so the only option is ``scrapling_static``. The link
and title selectors below work fine against the real index. The *date* does not,
and that is disqualifying: the authoritative window filter needs a real
publication date, and neither the index nor the article page states one in any
machine-readable form.

These tests are the evidence for that verdict. They run entirely offline against
``tests/fixtures/sources/anthropic-news/``, which is real markup captured on
2026-08-19. If Anthropic ever ships a feed, a ``<time datetime>`` attribute,
JSON-LD or an ``article:published_time`` meta tag, the date assertions here start
failing -- which is exactly the signal to revisit ``config/sources.yaml``.
"""

from __future__ import annotations

import pytest

from newsletter.ingestion.dates import parse_datetime
from newsletter.ingestion.scrapling import ScraplingAdapter
from newsletter.models import DiscoveredArticle, RawArticle, SourceConfig, TopicCategory
from newsletter.normalization.article import NormalizationError, normalize_article
from tests.conftest import FakeHttpClient, read_fixture

INDEX_URL = "https://www.anthropic.com/news"
WATERMARK_URL = "https://www.anthropic.com/news/claude-text-watermark"

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


def make_source() -> SourceConfig:
    return SourceConfig(
        id="anthropic-news",
        name="Anthropic News",
        entrypoint=INDEX_URL,
        strategy="scrapling_static",
        priority=7,
        category_hint=TopicCategory.AI_MODELS,
        enabled=False,
        selectors=SELECTORS,
    )


@pytest.fixture
def adapter(anthropic_index: str, anthropic_article: str) -> ScraplingAdapter:
    client = FakeHttpClient({INDEX_URL: anthropic_index, WATERMARK_URL: anthropic_article})
    return ScraplingAdapter(make_source(), http=client)


# --------------------------------------------------------------------------- #
# what does work: links and titles
# --------------------------------------------------------------------------- #


def test_link_selector_finds_every_news_item(adapter: ScraplingAdapter, window) -> None:
    """``a[href^="/news/"]`` resolves the index to absolute article URLs."""
    found = adapter.discover(window)

    assert [article.url for article in found] == [
        "https://www.anthropic.com/news/claude-opus-5",
        WATERMARK_URL,
        "https://www.anthropic.com/news/improving-fable-5-s-biology-safeguards",
        "https://www.anthropic.com/news/tino-cuellar",
        "https://www.anthropic.com/news/investigating-incidents-cybersecurity-evals",
    ]
    assert all(isinstance(article, DiscoveredArticle) for article in found)


def test_titles_are_recoverable_from_the_anchor(adapter: ScraplingAdapter, window) -> None:
    """Each anchor carries its headline, alongside the date and subject tag."""
    found = adapter.discover(window)
    titles = [article.title_hint or "" for article in found]

    assert all(titles)
    assert "How Claude" in titles[1] and "text watermark works" in titles[1]


# --------------------------------------------------------------------------- #
# what does not work: the date. This is the blocker.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("rendered", ["Aug 14, 2026", "Jul 24, 2026", "Jul 30, 2026"])
def test_anthropic_date_text_is_not_parseable(rendered: str) -> None:
    """Anthropic renders ``Mon D, YYYY``, which is neither ISO 8601 nor RFC 2822.

    ``parse_datetime`` correctly refuses to guess rather than invent a timestamp.
    """
    assert parse_datetime(rendered) is None


def test_index_yields_no_publication_date(adapter: ScraplingAdapter, window) -> None:
    """The ``<time>`` elements exist but carry no ``datetime`` attribute."""
    found = adapter.discover(window)

    assert found, "discovery must find items, otherwise this proves nothing"
    assert all(article.published_at_hint is None for article in found)


def test_article_page_states_no_date_so_normalization_refuses_it(
    anthropic_article: str, window
) -> None:
    """Stage 3 cannot rescue the date either: the article page does not state one.

    No ``article:published_time``, no ``datePublished`` JSON-LD, no ``<time>``
    element. With no hint from discovery, the article is rejected rather than
    dated by guesswork -- so an enabled ``anthropic-news`` would contribute
    exactly nothing to an edition while still costing a fetch per article.
    """
    raw = RawArticle(
        source_id="anthropic-news",
        url=WATERMARK_URL,
        final_url=WATERMARK_URL,
        raw_content=anthropic_article,
        retrieved_at=window.end,
        content_type="text/html",
    )

    with pytest.raises(NormalizationError, match="no publication date"):
        normalize_article(raw, make_source(), hint=None)
