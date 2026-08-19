"""Scrapling adapter tests. Offline: fixtures are parsed by the real Scrapling engine."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from newsletter.ingestion.base import DiscoveryError, FetchError, UnsupportedStrategyError
from newsletter.ingestion.scrapling import ScraplingAdapter
from newsletter.models import (
    DateWindow,
    DiscoveredArticle,
    FetchStrategy,
    RawArticle,
    SourceConfig,
    TopicCategory,
)
from tests.conftest import FakeHttpClient

INDEX_URL = "https://site.example/research"
VIDEO_MODEL = "https://site.example/research/video-model"
ABSOLUTE_LINK = "https://site.example/research/absolute-link"
UNDATED_POST = "https://site.example/research/undated-post"
LAST_MONTH = "https://site.example/research/last-month"

SELECTORS = {
    "index_item": "article.card",
    "link": "a.card-link",
    "title": "h2.card-title",
    "date": "time.card-date",
    "date_attr": "datetime",
}


def make_source(**overrides: object) -> SourceConfig:
    values: dict[str, object] = {
        "id": "example-site",
        "name": "Example Site",
        "entrypoint": INDEX_URL,
        "strategy": "scrapling_static",
        "priority": 7,
        "category_hint": TopicCategory.AI_VIDEO,
        "selectors": SELECTORS,
    }
    values.update(overrides)
    return SourceConfig(**values)  # type: ignore[arg-type]


@pytest.fixture
def adapter(index_html: str, site_article_html: str) -> ScraplingAdapter:
    client = FakeHttpClient(
        {
            INDEX_URL: index_html,
            VIDEO_MODEL: site_article_html,
            ABSOLUTE_LINK: site_article_html,
            UNDATED_POST: site_article_html,
        }
    )
    return ScraplingAdapter(make_source(), http=client)


# --------------------------------------------------------------------------- #
# discovery
# --------------------------------------------------------------------------- #


def test_discover_extracts_in_window_and_undated_items(adapter: ScraplingAdapter, window) -> None:
    found = adapter.discover(window)

    # Dropped: the July item (out of window), the duplicate teaser, the item with
    # no link at all.
    assert [a.url for a in found] == [VIDEO_MODEL, ABSOLUTE_LINK, UNDATED_POST]
    assert all(isinstance(a, DiscoveredArticle) for a in found)


def test_relative_links_are_resolved_against_the_index_url(
    adapter: ScraplingAdapter, window
) -> None:
    assert adapter.discover(window)[0].url == VIDEO_MODEL


def test_absolute_links_are_preserved(adapter: ScraplingAdapter, window) -> None:
    assert ABSOLUTE_LINK in [a.url for a in adapter.discover(window)]


def test_duplicate_links_are_collapsed(adapter: ScraplingAdapter, window) -> None:
    urls = [a.url for a in adapter.discover(window)]
    assert len(urls) == len(set(urls))


def test_dates_come_from_the_time_attribute(adapter: ScraplingAdapter, window) -> None:
    found = adapter.discover(window)
    assert found[0].published_at_hint == datetime(2026, 8, 16, 8, 0, tzinfo=UTC)
    assert found[1].published_at_hint == datetime(2026, 8, 18, 7, 15, tzinfo=UTC)


def test_item_without_a_date_is_kept_with_no_hint(adapter: ScraplingAdapter, window) -> None:
    undated = next(a for a in adapter.discover(window) if a.url == UNDATED_POST)
    assert undated.published_at_hint is None


def test_out_of_window_item_is_dropped(adapter: ScraplingAdapter, window) -> None:
    assert LAST_MONTH not in [a.url for a in adapter.discover(window)]


def test_titles_come_from_the_title_selector(adapter: ScraplingAdapter, window) -> None:
    assert adapter.discover(window)[0].title_hint == "A new video generation model"


def test_discovery_is_deterministic(adapter: ScraplingAdapter, window) -> None:
    assert [a.url for a in adapter.discover(window)] == [a.url for a in adapter.discover(window)]


def test_max_articles_is_respected(index_html: str, window) -> None:
    client = FakeHttpClient({INDEX_URL: index_html})
    adapter = ScraplingAdapter(make_source(options={"max_articles": 1}), http=client)
    assert len(adapter.discover(window)) == 1


def test_falls_back_to_bare_links_without_an_index_item_selector(index_html: str, window) -> None:
    source = make_source(selectors={"link": "a.card-link"})
    adapter = ScraplingAdapter(source, http=FakeHttpClient({INDEX_URL: index_html}))
    urls = [a.url for a in adapter.discover(window)]
    # No date selector configured, so nothing can be filtered by date here.
    assert VIDEO_MODEL in urls
    assert LAST_MONTH in urls


# --------------------------------------------------------------------------- #
# discovery failures
# --------------------------------------------------------------------------- #


def test_stale_selector_raises_discovery_error(index_html: str, window) -> None:
    """A site redesign must fail loudly, not return an empty edition."""
    source = make_source(selectors={"index_item": "article.does-not-exist", "link": "a"})
    adapter = ScraplingAdapter(source, http=FakeHttpClient({INDEX_URL: index_html}))
    with pytest.raises(DiscoveryError, match="matched nothing"):
        adapter.discover(window)


def test_unreachable_index_raises_discovery_error(window) -> None:
    client = FakeHttpClient({}, failures={INDEX_URL: "HTTP 403 Forbidden"})
    adapter = ScraplingAdapter(make_source(), http=client)
    with pytest.raises(DiscoveryError, match="could not read index page"):
        adapter.discover(window)


# --------------------------------------------------------------------------- #
# fetching
# --------------------------------------------------------------------------- #


def test_fetch_returns_raw_article(adapter: ScraplingAdapter, window) -> None:
    raw = adapter.fetch(adapter.discover(window)[0])

    assert isinstance(raw, RawArticle)
    assert raw.url == VIDEO_MODEL
    assert "A new video generation model" in raw.raw_content
    assert raw.content_type == "text/html"
    assert raw.http_metadata == {"strategy": "scrapling_static"}


def test_fetch_failure_raises_fetch_error(index_html: str, window) -> None:
    client = FakeHttpClient({INDEX_URL: index_html}, failures={VIDEO_MODEL: "HTTP 500"})
    adapter = ScraplingAdapter(make_source(), http=client)
    with pytest.raises(FetchError, match="could not fetch"):
        adapter.fetch(adapter.discover(window=_wide_window())[0])


def _wide_window() -> DateWindow:
    return DateWindow(start=datetime(2026, 7, 1, tzinfo=UTC), end=datetime(2026, 8, 19, tzinfo=UTC))


def test_scrapling_types_never_leak(adapter: ScraplingAdapter, window) -> None:
    """The rest of the pipeline only ever sees plain domain models."""
    candidate = adapter.discover(window)[0]
    raw = adapter.fetch(candidate)
    assert type(candidate).__module__.startswith("newsletter.")
    assert type(raw).__module__.startswith("newsletter.")
    assert isinstance(raw.raw_content, str)


# --------------------------------------------------------------------------- #
# browser strategies: explicit extension points, not silent failures
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "strategy", [FetchStrategy.SCRAPLING_DYNAMIC, FetchStrategy.SCRAPLING_STEALTH]
)
def test_browser_strategies_require_a_loader(strategy: FetchStrategy) -> None:
    with pytest.raises(UnsupportedStrategyError, match="scrapling\\[fetchers\\]"):
        ScraplingAdapter(make_source(strategy=strategy), http=FakeHttpClient({}))


def test_injected_page_loader_satisfies_a_browser_strategy(
    index_html: str, site_article_html: str, window
) -> None:
    """The extension point works: a browser backend plugs in here."""
    pages = {INDEX_URL: index_html, VIDEO_MODEL: site_article_html}
    loaded: list[str] = []

    def loader(url: str) -> tuple[str, str]:
        loaded.append(url)
        return pages[url], url

    adapter = ScraplingAdapter(
        make_source(strategy=FetchStrategy.SCRAPLING_DYNAMIC),
        page_loader=loader,
    )
    found = adapter.discover(window)
    raw = adapter.fetch(found[0])

    assert [a.url for a in found] == [VIDEO_MODEL, ABSOLUTE_LINK, UNDATED_POST]
    assert raw.http_metadata == {"strategy": "scrapling_dynamic"}
    assert loaded == [INDEX_URL, VIDEO_MODEL]
