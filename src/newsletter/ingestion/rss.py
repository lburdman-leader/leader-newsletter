"""RSS/Atom source adapter.

The preferred ingestion path: a feed already gives structured titles, links and
publication dates, so it beats scraping on both reliability and cost.

Two behaviours are deliberate:

* an entry whose date cannot be parsed is **kept**, not dropped and not given an
  invented date -- Stage 3 resolves the date from the article itself;
* an entry whose date is known and outside the window is dropped here, which
  avoids fetching articles the pipeline would discard anyway.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import feedparser

from newsletter.ingestion.base import DiscoveryError, FetchError, max_articles_for
from newsletter.ingestion.dates import from_struct_time, parse_datetime
from newsletter.ingestion.http import HttpClient, HttpError, UrllibHttpClient
from newsletter.logging_setup import get_logger
from newsletter.models import (
    DateWindow,
    DiscoveredArticle,
    RawArticle,
    SourceConfig,
)

logger = get_logger("ingestion.rss")


class RssAdapter:
    """Discover articles from an RSS/Atom feed and fetch their pages."""

    def __init__(self, source: SourceConfig, *, http: HttpClient | None = None) -> None:
        self.source = source
        self.http = http or UrllibHttpClient()
        #: Full content carried by the feed itself, keyed by article URL.
        self._feed_content: dict[str, str] = {}

    # -- discovery ---------------------------------------------------------- #

    def discover(self, window: DateWindow) -> list[DiscoveredArticle]:
        try:
            response = self.http.get(self.source.entrypoint)
        except HttpError as exc:
            raise DiscoveryError(self.source.id, f"could not read feed: {exc}") from exc

        parsed = feedparser.parse(response.text)
        entries = list(getattr(parsed, "entries", []))

        if not entries:
            bozo = getattr(parsed, "bozo_exception", None)
            detail = f": {bozo}" if bozo else ""
            raise DiscoveryError(self.source.id, f"feed contained no entries{detail}")

        if getattr(parsed, "bozo", 0):
            # Malformed but parseable: usable, and worth surfacing.
            logger.warning(
                "%s: feed is malformed but yielded %d entries (%s)",
                self.source.id,
                len(entries),
                getattr(parsed, "bozo_exception", "unknown issue"),
            )

        limit = max_articles_for(self.source)
        discovered: list[DiscoveredArticle] = []
        skipped_out_of_window = 0
        skipped_unusable = 0

        for entry in entries:
            if len(discovered) >= limit:
                break

            url = str(entry.get("link") or "").strip()
            if not url:
                skipped_unusable += 1
                continue

            published_at = self._entry_datetime(entry)
            if published_at is not None and not window.contains(published_at):
                skipped_out_of_window += 1
                continue

            title = str(entry.get("title") or "").strip() or None

            try:
                candidate = DiscoveredArticle(
                    source_id=self.source.id,
                    url=url,
                    title_hint=title,
                    published_at_hint=published_at,
                )
            except ValueError as exc:
                # A feed can contain a relative or non-http link; skip it loudly.
                logger.warning("%s: unusable entry link %r (%s)", self.source.id, url, exc)
                skipped_unusable += 1
                continue

            content = self._entry_content(entry)
            if content:
                self._feed_content[candidate.url] = content
            discovered.append(candidate)

        logger.info(
            "%s: %d entries -> %d candidates (%d out of window, %d unusable)",
            self.source.id,
            len(entries),
            len(discovered),
            skipped_out_of_window,
            skipped_unusable,
        )
        return discovered

    # -- fetching ----------------------------------------------------------- #

    def fetch(self, article: DiscoveredArticle) -> RawArticle:
        """Fetch the article page, or reuse full content already in the feed."""
        if self.source.options.get("use_feed_content"):
            content = self._feed_content.get(article.url)
            if content:
                return RawArticle(
                    source_id=self.source.id,
                    url=article.url,
                    final_url=article.url,
                    raw_content=content,
                    retrieved_at=datetime.now(UTC),
                    content_type="text/html",
                    http_metadata={"origin": "feed"},
                )

        try:
            response = self.http.get(article.url)
        except HttpError as exc:
            raise FetchError(self.source.id, f"could not fetch {article.url}: {exc}") from exc

        return RawArticle(
            source_id=self.source.id,
            url=article.url,
            final_url=response.final_url,
            raw_content=response.text,
            retrieved_at=datetime.now(UTC),
            content_type=response.content_type,
            http_metadata=response.metadata,
        )

    # -- helpers ------------------------------------------------------------ #

    @staticmethod
    def _entry_datetime(entry: Any) -> datetime | None:
        """Publication date of a feed entry, or None. Never invented."""
        for key in ("published_parsed", "updated_parsed", "created_parsed"):
            moment = from_struct_time(entry.get(key))
            if moment is not None:
                return moment
        for key in ("published", "updated", "created", "date"):
            moment = parse_datetime(entry.get(key))
            if moment is not None:
                return moment
        return None

    @staticmethod
    def _entry_content(entry: Any) -> str | None:
        """Full article body when the feed carries one."""
        content = entry.get("content")
        if isinstance(content, list) and content:
            value = content[0].get("value") if isinstance(content[0], dict) else None
            if value:
                return str(value)
        summary = entry.get("summary")
        return str(summary) if summary else None
