"""Ingestion contracts and failure-isolating orchestration.

Every source is reached through one interface:

    discover(window) -> list[DiscoveredArticle]
    fetch(article)   -> RawArticle

Adapter-specific machinery (feedparser structures, Scrapling selectors, HTTP
plumbing) stops at this boundary; the rest of the pipeline only ever sees the
models in ``newsletter.models``.

The second responsibility of this module is failure isolation. A broken source
must not stop the edition (AC10), so :func:`ingest_all` catches per-source and
per-article failures, records each one in the run manifest, and continues. It
never swallows an error silently.

Fetching is the third: an article is a single HTTP round trip that spends its
time waiting, so :func:`ingest_source` runs a bounded number of them at once. The
concurrency stops there — sources are still ingested one at a time, in order, so
the manifest's per-source accounting stays a plain sequence of attempts, and a
source is never asked for more connections than a browser would open to it.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from newsletter.concurrency import DEFAULT_FETCH_CONCURRENCY, map_ordered
from newsletter.ingestion.http import HttpClient, HttpError
from newsletter.logging_setup import get_logger
from newsletter.models import (
    DateWindow,
    DiscoveredArticle,
    FetchStrategy,
    PipelineStage,
    RawArticle,
    RunManifest,
    SourceConfig,
)

logger = get_logger("ingestion")

#: Safety valve so one enormous feed cannot dominate a run.
DEFAULT_MAX_ARTICLES = 50


# --------------------------------------------------------------------------- #
# errors
# --------------------------------------------------------------------------- #


class AdapterError(Exception):
    """Base class for ingestion failures, always attributed to a source."""

    stage: PipelineStage = PipelineStage.DISCOVER

    def __init__(self, source_id: str, message: str) -> None:
        super().__init__(f"[{source_id}] {message}")
        self.source_id = source_id
        self.reason = message


class DiscoveryError(AdapterError):
    """The source index or feed could not be read."""

    stage = PipelineStage.DISCOVER


class FetchError(AdapterError):
    """A single article could not be retrieved."""

    stage = PipelineStage.FETCH


class UnsupportedStrategyError(AdapterError):
    """The configured strategy needs a capability that is not installed."""

    stage = PipelineStage.LOAD_CONFIG


# --------------------------------------------------------------------------- #
# adapter contract
# --------------------------------------------------------------------------- #


@runtime_checkable
class SourceAdapter(Protocol):
    """What every source implementation must provide."""

    source: SourceConfig

    def discover(self, window: DateWindow) -> list[DiscoveredArticle]:
        """Candidate articles from the source index or feed.

        Implementations drop candidates whose *known* publication date falls
        outside ``window``, but must keep candidates with an unknown date: the
        authoritative filter runs in Stage 3 on normalized articles.
        """
        ...

    def fetch(self, article: DiscoveredArticle) -> RawArticle:
        """Retrieve one candidate. Raises :class:`FetchError` on failure."""
        ...


AdapterFactory = Callable[[SourceConfig], SourceAdapter]


def max_articles_for(source: SourceConfig) -> int:
    """Per-source cap, from ``options.max_articles``."""
    raw = source.options.get("max_articles", DEFAULT_MAX_ARTICLES)
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise UnsupportedStrategyError(
            source.id, f"options.max_articles is not an integer: {raw!r}"
        ) from exc
    if value <= 0:
        raise UnsupportedStrategyError(source.id, "options.max_articles must be positive")
    return value


def build_adapter(source: SourceConfig, *, http: HttpClient | None = None) -> SourceAdapter:
    """Create the adapter for a source. The strategy comes from configuration only."""
    # Imported lazily so that `newsletter validate` does not pay for feedparser
    # or Scrapling, and so a missing optional dependency fails per-source.
    if source.strategy is FetchStrategy.RSS:
        from newsletter.ingestion.rss import RssAdapter

        return RssAdapter(source, http=http)

    if source.strategy in {
        FetchStrategy.SCRAPLING_STATIC,
        FetchStrategy.SCRAPLING_DYNAMIC,
        FetchStrategy.SCRAPLING_STEALTH,
    }:
        from newsletter.ingestion.scrapling import ScraplingAdapter

        return ScraplingAdapter(source, http=http)

    raise UnsupportedStrategyError(  # pragma: no cover - the enum is closed
        source.id, f"no adapter for strategy {source.strategy.value!r}"
    )


# --------------------------------------------------------------------------- #
# orchestration with failure isolation
# --------------------------------------------------------------------------- #


@dataclass
class SourceOutcome:
    """What one source contributed, and how it failed if it did."""

    source_id: str
    #: Candidates returned by discovery, kept so later stages can reuse the
    #: publication-date hints without re-running discovery.
    candidates: list[DiscoveredArticle] = field(default_factory=list)
    fetched: list[RawArticle] = field(default_factory=list)
    failed: bool = False
    reason: str | None = None

    @property
    def discovered(self) -> int:
        return len(self.candidates)


@dataclass
class IngestionResult:
    """Aggregate ingestion outcome. Counts feed straight into the manifest."""

    raw_articles: list[RawArticle] = field(default_factory=list)
    outcomes: list[SourceOutcome] = field(default_factory=list)

    @property
    def discovered(self) -> int:
        return sum(o.discovered for o in self.outcomes)

    @property
    def succeeded(self) -> list[SourceOutcome]:
        return [o for o in self.outcomes if not o.failed]

    @property
    def failed(self) -> list[SourceOutcome]:
        return [o for o in self.outcomes if o.failed]


def ingest_source(
    adapter: SourceAdapter,
    window: DateWindow,
    *,
    manifest: RunManifest,
    concurrency: int = DEFAULT_FETCH_CONCURRENCY,
) -> SourceOutcome:
    """Discover and fetch one source, isolating per-article failures.

    A discovery failure fails the source. An individual article failure is
    recorded and skipped -- the remaining articles of that source still count.

    Up to ``concurrency`` articles are fetched at once, but the results are read
    back in discovery order and the failures are recorded from this thread, so
    both the fetched sequence and the manifest are the same as they would be one
    request at a time. ``concurrency=1`` is exactly that.
    """
    source_id = adapter.source.id
    outcome = SourceOutcome(source_id=source_id)

    try:
        candidates = adapter.discover(window)
    except (AdapterError, HttpError) as exc:
        manifest.record_error(PipelineStage.DISCOVER, exc, source_id=source_id)
        logger.warning("discovery failed for %s: %s", source_id, exc)
        outcome.failed = True
        outcome.reason = str(exc)
        return outcome

    outcome.candidates = candidates
    logger.info("%s: discovered %d candidates", source_id, len(candidates))

    fetches = map_ordered(
        adapter.fetch,
        candidates,
        concurrency=concurrency,
        capture=(AdapterError, HttpError),
        thread_name_prefix=f"fetch-{source_id}",
    )
    for candidate, fetched in zip(candidates, fetches, strict=True):
        if fetched.error is not None:
            manifest.record_error(PipelineStage.FETCH, fetched.error, source_id=source_id)
            logger.warning("fetch failed for %s (%s): %s", source_id, candidate.url, fetched.error)
            continue
        assert fetched.value is not None
        outcome.fetched.append(fetched.value)

    return outcome


def ingest_all(
    sources: Sequence[SourceConfig] | Iterable[SourceConfig],
    window: DateWindow,
    *,
    manifest: RunManifest,
    adapter_factory: AdapterFactory | None = None,
    concurrency: int = DEFAULT_FETCH_CONCURRENCY,
) -> IngestionResult:
    """Ingest every source in order, isolating failures, updating the manifest.

    Source order is the caller's (``AppConfig.enabled_sources`` is already
    deterministic), and article order within a source is preserved, so a repeated
    run over identical inputs produces an identical article sequence.

    ``concurrency`` applies *within* a source. Sources themselves stay sequential:
    overlapping them would buy little — discovery is one request each — while
    making ``sources_attempted`` / ``sources_succeeded`` / ``sources_failed`` the
    product of a race rather than of a loop.
    """
    factory: AdapterFactory = adapter_factory or build_adapter
    result = IngestionResult()

    for source in sources:
        manifest.sources_attempted += 1
        try:
            adapter = factory(source)
        except (AdapterError, ImportError) as exc:
            manifest.record_error(PipelineStage.LOAD_CONFIG, exc, source_id=source.id)
            logger.warning("adapter unavailable for %s: %s", source.id, exc)
            manifest.sources_failed += 1
            result.outcomes.append(SourceOutcome(source_id=source.id, failed=True, reason=str(exc)))
            continue

        outcome = ingest_source(adapter, window, manifest=manifest, concurrency=concurrency)
        result.outcomes.append(outcome)
        result.raw_articles.extend(outcome.fetched)

        if outcome.failed:
            manifest.sources_failed += 1
        else:
            manifest.sources_succeeded += 1

    manifest.articles_discovered = result.discovered
    return result
