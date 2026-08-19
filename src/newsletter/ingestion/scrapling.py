"""Scrapling-based source adapter for sources without a usable feed.

Scrapling is the parsing engine: ``Selector`` gives robust CSS/XPath selection
over untrusted markup, and its adaptive-selector features are available as a
recovery path when a site changes. Transport is pluggable
(:class:`~newsletter.ingestion.http.HttpClient`), which keeps every extraction
test offline and keeps the default install free of browser binaries.

Strategy support:

===================  ====================================================
``scrapling_static``  supported now (HTTP + Selector)
``scrapling_dynamic`` extension point: needs a browser loader
``scrapling_stealth`` extension point: needs a browser loader
===================  ====================================================

Dynamic and stealth raise :class:`UnsupportedStrategyError` with actionable
instructions rather than pretending to work. A browser is justified by observed
source behaviour, never by convenience (PRD section 15).

Scrapling objects never leave this module.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from scrapling import Selector

from newsletter.ingestion.base import (
    DiscoveryError,
    FetchError,
    UnsupportedStrategyError,
    max_articles_for,
)
from newsletter.ingestion.dates import parse_datetime
from newsletter.ingestion.http import HttpClient, HttpError, UrllibHttpClient
from newsletter.logging_setup import get_logger
from newsletter.models import (
    DateWindow,
    DiscoveredArticle,
    FetchStrategy,
    RawArticle,
    SourceConfig,
)

logger = get_logger("ingestion.scrapling")

#: Selector keys read from ``SourceConfig.selectors``.
DEFAULT_LINK_SELECTOR = "a"
DEFAULT_DATE_ATTRIBUTE = "datetime"

BROWSER_STRATEGIES = frozenset({FetchStrategy.SCRAPLING_DYNAMIC, FetchStrategy.SCRAPLING_STEALTH})

#: A loader turns a URL into (html, final_url). Injected for browsers and tests.
PageLoader = Callable[[str], tuple[str, str]]


class ScraplingAdapter:
    """Discover articles from an index page and fetch article pages."""

    def __init__(
        self,
        source: SourceConfig,
        *,
        http: HttpClient | None = None,
        page_loader: PageLoader | None = None,
    ) -> None:
        self.source = source
        self.http = http or UrllibHttpClient()
        self._page_loader = page_loader

        if source.strategy in BROWSER_STRATEGIES and page_loader is None:
            raise UnsupportedStrategyError(
                source.id,
                f"strategy {source.strategy.value!r} needs a browser backend. "
                "Install it with: pip install 'scrapling[fetchers]' && scrapling install, "
                "then inject a page_loader. Prefer rss or scrapling_static unless the "
                "source genuinely requires a browser.",
            )

    # -- discovery ---------------------------------------------------------- #

    def discover(self, window: DateWindow) -> list[DiscoveredArticle]:
        try:
            page = self._load(self.source.entrypoint)
        except (HttpError, FetchError) as exc:
            raise DiscoveryError(self.source.id, f"could not read index page: {exc}") from exc

        selectors = self.source.selectors
        item_selector = selectors.get("index_item")
        link_selector = selectors.get("link", DEFAULT_LINK_SELECTOR)

        containers = page.css(item_selector) if item_selector else page.css(link_selector)
        if not containers:
            raise DiscoveryError(
                self.source.id,
                f"index selector {item_selector or link_selector!r} matched nothing; "
                "the page structure probably changed",
            )

        limit = max_articles_for(self.source)
        discovered: list[DiscoveredArticle] = []
        seen: set[str] = set()
        skipped_out_of_window = 0
        skipped_unusable = 0

        for container in containers:
            if len(discovered) >= limit:
                break

            anchor = container if not item_selector else _first(container.css(link_selector))
            if anchor is None:
                skipped_unusable += 1
                continue

            href = anchor.attrib.get("href")
            if not href:
                skipped_unusable += 1
                continue

            url = page.urljoin(str(href).strip())
            if url in seen:
                continue

            published_at = self._container_datetime(container, selectors)
            if published_at is not None and not window.contains(published_at):
                skipped_out_of_window += 1
                continue

            try:
                candidate = DiscoveredArticle(
                    source_id=self.source.id,
                    url=url,
                    title_hint=self._container_title(container, anchor, selectors),
                    published_at_hint=published_at,
                )
            except ValueError as exc:
                logger.warning("%s: unusable link %r (%s)", self.source.id, url, exc)
                skipped_unusable += 1
                continue

            seen.add(url)
            discovered.append(candidate)

        logger.info(
            "%s: %d index items -> %d candidates (%d out of window, %d unusable)",
            self.source.id,
            len(containers),
            len(discovered),
            skipped_out_of_window,
            skipped_unusable,
        )
        return discovered

    # -- fetching ----------------------------------------------------------- #

    def fetch(self, article: DiscoveredArticle) -> RawArticle:
        page = self._load(article.url)
        return RawArticle(
            source_id=self.source.id,
            url=article.url,
            final_url=page.url or article.url,
            raw_content=page.html_content,
            retrieved_at=datetime.now(UTC),
            content_type="text/html",
            http_metadata={"strategy": self.source.strategy.value},
        )

    # -- helpers ------------------------------------------------------------ #

    def _load(self, url: str) -> Selector:
        """Retrieve a page and hand back a parsed selector tree."""
        if self._page_loader is not None:
            html, final_url = self._page_loader(url)
            return Selector(html, url=final_url or url)

        try:
            response = self.http.get(url)
        except HttpError as exc:
            raise FetchError(self.source.id, f"could not fetch {url}: {exc}") from exc
        return Selector(response.text, url=response.final_url)

    def _container_datetime(self, container: Any, selectors: dict[str, str]) -> datetime | None:
        """Publication date from the index item, or None. Never invented."""
        date_selector = selectors.get("date")
        if not date_selector:
            return None
        element = _first(container.css(date_selector))
        if element is None:
            return None

        attribute = selectors.get("date_attr", DEFAULT_DATE_ATTRIBUTE)
        raw = element.attrib.get(attribute)
        if raw:
            parsed = parse_datetime(str(raw))
            if parsed is not None:
                return parsed
        return parse_datetime(element.get_all_text(strip=True))

    @staticmethod
    def _container_title(container: Any, anchor: Any, selectors: dict[str, str]) -> str | None:
        title_selector = selectors.get("title")
        if title_selector:
            element = _first(container.css(title_selector))
            if element is not None:
                text = element.get_all_text(strip=True)
                if text:
                    return " ".join(text.split())
        text = anchor.get_all_text(strip=True)
        return " ".join(text.split()) if text else None


def _first(matches: Any) -> Any | None:
    """Scrapling returns a list-like; there is no ``css_first`` in 0.4."""
    return matches[0] if len(matches) else None
