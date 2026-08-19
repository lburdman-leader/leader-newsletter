"""RSS adapter tests. Entirely offline, driven by tests/fixtures/sources/example-feed."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from newsletter.ingestion.base import DiscoveryError, FetchError
from newsletter.ingestion.rss import RssAdapter
from newsletter.models import DateWindow, DiscoveredArticle, RawArticle, SourceConfig, TopicCategory
from tests.conftest import FakeHttpClient

FEED_URL = "https://feed.example/rss.xml"

MODEL_RELEASE = "https://feed.example/news/model-release"
OLD_ANNOUNCEMENT = "https://feed.example/news/old-announcement"
UNDATED_NOTE = "https://feed.example/news/undated-note"
MONETIZATION = "https://feed.example/news/monetization-update"


def make_source(**overrides: object) -> SourceConfig:
    values: dict[str, object] = {
        "id": "example-feed",
        "name": "Example Feed",
        "entrypoint": FEED_URL,
        "strategy": "rss",
        "priority": 8,
        "category_hint": TopicCategory.AI_MODELS,
    }
    values.update(overrides)
    return SourceConfig(**values)  # type: ignore[arg-type]


@pytest.fixture
def adapter(feed_xml: str, feed_article_html: str) -> RssAdapter:
    client = FakeHttpClient(
        {
            FEED_URL: feed_xml,
            MODEL_RELEASE: feed_article_html,
            UNDATED_NOTE: feed_article_html,
            MONETIZATION: feed_article_html,
        }
    )
    return RssAdapter(make_source(), http=client)


# --------------------------------------------------------------------------- #
# discovery
# --------------------------------------------------------------------------- #


def test_discover_returns_in_window_and_undated_entries(adapter: RssAdapter, window) -> None:
    found = adapter.discover(window)

    # In feed order: dated-in-window, undated (kept), dated-in-window.
    # Dropped: the August 1 entry (out of window) and the relative link (unusable).
    assert [a.url for a in found] == [MODEL_RELEASE, UNDATED_NOTE, MONETIZATION]
    assert all(isinstance(a, DiscoveredArticle) for a in found)
    assert all(a.source_id == "example-feed" for a in found)


def test_discover_parses_dates_as_aware_utc(adapter: RssAdapter, window) -> None:
    found = adapter.discover(window)
    assert found[0].published_at_hint == datetime(2026, 8, 17, 10, 0, tzinfo=UTC)
    assert found[2].published_at_hint == datetime(2026, 8, 18, 6, 30, tzinfo=UTC)


def test_discover_never_invents_a_missing_date(adapter: RssAdapter, window) -> None:
    """An undated entry is kept for Stage 3 to resolve, not given a fake date."""
    undated = next(a for a in adapter.discover(window) if a.url == UNDATED_NOTE)
    assert undated.published_at_hint is None


def test_discover_drops_entries_outside_the_window(adapter: RssAdapter, window) -> None:
    assert OLD_ANNOUNCEMENT not in [a.url for a in adapter.discover(window)]


def test_discover_skips_entries_with_unusable_links(adapter: RssAdapter, window) -> None:
    """A relative link cannot be published or fetched, so it is skipped, not guessed."""
    assert all(a.url.startswith("https://") for a in adapter.discover(window))
    assert len(adapter.discover(window)) == 3


def test_discover_extracts_titles(adapter: RssAdapter, window) -> None:
    titles = [a.title_hint for a in adapter.discover(window)]
    assert titles[0] == "Example Labs releases a new reasoning model"
    assert titles[2] == "Monetization policy update"


def test_discover_is_deterministic(adapter: RssAdapter, window) -> None:
    assert [a.url for a in adapter.discover(window)] == [a.url for a in adapter.discover(window)]


def test_discover_respects_max_articles(feed_xml: str, window) -> None:
    client = FakeHttpClient({FEED_URL: feed_xml})
    adapter = RssAdapter(make_source(options={"max_articles": 2}), http=client)
    assert len(adapter.discover(window)) == 2


def test_wider_window_includes_the_older_entry(adapter: RssAdapter) -> None:
    wide = DateWindow(start=datetime(2026, 7, 1, tzinfo=UTC), end=datetime(2026, 8, 19, tzinfo=UTC))
    assert OLD_ANNOUNCEMENT in [a.url for a in adapter.discover(wide)]


# --------------------------------------------------------------------------- #
# discovery failures
# --------------------------------------------------------------------------- #


def test_unreachable_feed_raises_discovery_error(window) -> None:
    client = FakeHttpClient({}, failures={FEED_URL: "connection refused"})
    adapter = RssAdapter(make_source(), http=client)
    with pytest.raises(DiscoveryError, match="could not read feed"):
        adapter.discover(window)


def test_empty_feed_raises_discovery_error(window) -> None:
    empty = '<?xml version="1.0"?><rss version="2.0"><channel><title>x</title></channel></rss>'
    adapter = RssAdapter(make_source(), http=FakeHttpClient({FEED_URL: empty}))
    with pytest.raises(DiscoveryError, match="no entries"):
        adapter.discover(window)


def test_garbage_response_raises_discovery_error(window) -> None:
    adapter = RssAdapter(make_source(), http=FakeHttpClient({FEED_URL: "not a feed at all"}))
    with pytest.raises(DiscoveryError):
        adapter.discover(window)


def test_malformed_but_parseable_feed_still_yields_entries(window) -> None:
    """Real feeds are often slightly invalid; usable content is still used."""
    sloppy = """<?xml version="1.0"?>
    <rss version="2.0"><channel><title>Sloppy</title>
      <item>
        <title>Still readable</title>
        <link>https://feed.example/news/sloppy</link>
        <pubDate>Mon, 17 Aug 2026 10:00:00 GMT</pubDate>
      </item>
    </channel>
    """  # deliberately unclosed <rss>
    adapter = RssAdapter(make_source(), http=FakeHttpClient({FEED_URL: sloppy}))
    found = adapter.discover(window)
    assert [a.url for a in found] == ["https://feed.example/news/sloppy"]


# --------------------------------------------------------------------------- #
# fetching
# --------------------------------------------------------------------------- #


def test_fetch_returns_raw_article(adapter: RssAdapter, window, feed_article_html: str) -> None:
    candidate = adapter.discover(window)[0]
    raw = adapter.fetch(candidate)

    assert isinstance(raw, RawArticle)
    assert raw.source_id == "example-feed"
    assert raw.url == MODEL_RELEASE
    assert raw.final_url == MODEL_RELEASE
    assert raw.raw_content == feed_article_html
    assert raw.retrieved_at.tzinfo is not None
    assert raw.http_metadata["status"] == 200


def test_fetch_records_the_final_url_after_a_redirect(
    feed_xml: str, feed_article_html: str, window
) -> None:
    canonical = "https://feed.example/news/model-release?utm_source=rss"
    client = FakeHttpClient(
        {FEED_URL: feed_xml, MODEL_RELEASE: feed_article_html},
        redirects={MODEL_RELEASE: canonical},
    )
    adapter = RssAdapter(make_source(), http=client)
    raw = adapter.fetch(adapter.discover(window)[0])
    assert raw.url == MODEL_RELEASE
    assert raw.final_url == canonical


def test_fetch_failure_raises_fetch_error(feed_xml: str, window) -> None:
    client = FakeHttpClient({FEED_URL: feed_xml}, failures={MODEL_RELEASE: "HTTP 503"})
    adapter = RssAdapter(make_source(), http=client)
    candidate = adapter.discover(window)[0]
    with pytest.raises(FetchError, match="could not fetch"):
        adapter.fetch(candidate)


def test_use_feed_content_avoids_a_second_request(feed_xml: str, window) -> None:
    client = FakeHttpClient({FEED_URL: feed_xml})
    adapter = RssAdapter(make_source(options={"use_feed_content": True}), http=client)
    candidate = adapter.discover(window)[0]

    raw = adapter.fetch(candidate)

    assert "Full article body carried by the feed." in raw.raw_content
    assert raw.http_metadata == {"origin": "feed"}
    assert client.calls == [FEED_URL]  # no article request at all


def test_use_feed_content_falls_back_to_http_when_the_feed_has_none(
    feed_xml: str, feed_article_html: str, window
) -> None:
    client = FakeHttpClient({FEED_URL: feed_xml, UNDATED_NOTE: feed_article_html})
    adapter = RssAdapter(make_source(options={"use_feed_content": True}), http=client)
    candidate = next(a for a in adapter.discover(window) if a.url == UNDATED_NOTE)

    raw = adapter.fetch(candidate)

    assert raw.raw_content == feed_article_html
    assert UNDATED_NOTE in client.calls
