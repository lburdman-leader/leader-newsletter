"""Adapter factory, date helpers, and the AC10 guarantee: one broken source
never stops the others."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from newsletter.ingestion.base import (
    DiscoveryError,
    FetchError,
    SourceAdapter,
    UnsupportedStrategyError,
    build_adapter,
    ingest_all,
    ingest_source,
    max_articles_for,
)
from newsletter.ingestion.dates import ensure_aware, from_struct_time, parse_datetime
from newsletter.ingestion.rss import RssAdapter
from newsletter.ingestion.scrapling import ScraplingAdapter
from newsletter.models import (
    DateWindow,
    DiscoveredArticle,
    FetchStrategy,
    PipelineStage,
    RawArticle,
    RunManifest,
    SourceConfig,
)

NOW = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)


def make_source(source_id: str, strategy: str = "rss", **overrides: object) -> SourceConfig:
    values: dict[str, object] = {
        "id": source_id,
        "name": source_id.title(),
        "entrypoint": f"https://{source_id}.example/feed",
        "strategy": strategy,
        "priority": 5,
    }
    values.update(overrides)
    return SourceConfig(**values)  # type: ignore[arg-type]


def manifest() -> RunManifest:
    return RunManifest(run_id="test-run", started_at=NOW)


# --------------------------------------------------------------------------- #
# factory
# --------------------------------------------------------------------------- #


def test_factory_builds_the_adapter_named_by_configuration() -> None:
    assert isinstance(build_adapter(make_source("a", "rss")), RssAdapter)
    assert isinstance(build_adapter(make_source("b", "scrapling_static")), ScraplingAdapter)


def test_adapters_satisfy_the_protocol() -> None:
    assert isinstance(build_adapter(make_source("a", "rss")), SourceAdapter)


def test_browser_strategy_without_a_loader_is_rejected_by_the_factory() -> None:
    with pytest.raises(UnsupportedStrategyError):
        build_adapter(make_source("c", "scrapling_dynamic"))


@pytest.mark.parametrize("bad", ["many", -1, 0])
def test_invalid_max_articles_is_rejected(bad: object) -> None:
    with pytest.raises(UnsupportedStrategyError):
        max_articles_for(make_source("a", options={"max_articles": bad}))


def test_max_articles_defaults_to_the_safety_valve() -> None:
    assert max_articles_for(make_source("a")) == 50


# --------------------------------------------------------------------------- #
# date helpers — never invent a date
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2026-08-17T10:00:00Z", datetime(2026, 8, 17, 10, 0, tzinfo=UTC)),
        ("2026-08-17T10:00:00+00:00", datetime(2026, 8, 17, 10, 0, tzinfo=UTC)),
        ("2026-08-17 10:00:00", datetime(2026, 8, 17, 10, 0, tzinfo=UTC)),
        ("2026-08-17", datetime(2026, 8, 17, 0, 0, tzinfo=UTC)),
        ("Mon, 17 Aug 2026 10:00:00 GMT", datetime(2026, 8, 17, 10, 0, tzinfo=UTC)),
        ("2026-08-17T10:00:00.123456789Z", datetime(2026, 8, 17, 10, 0, 0, 123456, tzinfo=UTC)),
    ],
)
def test_parse_datetime_handles_real_world_shapes(raw: str, expected: datetime) -> None:
    assert parse_datetime(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "   ", "yesterday", "not a date", "2026-13-45"])
def test_parse_datetime_returns_none_rather_than_guessing(raw: str | None) -> None:
    assert parse_datetime(raw) is None


def test_parsed_dates_are_always_timezone_aware() -> None:
    parsed = parse_datetime("2026-08-17 10:00:00")
    assert parsed is not None and parsed.tzinfo is not None


def test_ensure_aware_passes_through_existing_offsets() -> None:
    aware = datetime(2026, 8, 17, 10, 0, tzinfo=UTC)
    assert ensure_aware(aware) is aware
    assert ensure_aware(None) is None


def test_from_struct_time_handles_missing_values() -> None:
    assert from_struct_time(None) is None


# --------------------------------------------------------------------------- #
# stub adapters for orchestration tests
# --------------------------------------------------------------------------- #


class StubAdapter:
    """Adapter whose behaviour is scripted by the test."""

    def __init__(
        self,
        source: SourceConfig,
        *,
        urls: list[str] | None = None,
        discover_error: Exception | None = None,
        failing_urls: set[str] | None = None,
    ) -> None:
        self.source = source
        self.urls = urls or []
        self.discover_error = discover_error
        self.failing_urls = failing_urls or set()

    def discover(self, window: DateWindow) -> list[DiscoveredArticle]:
        if self.discover_error is not None:
            raise self.discover_error
        return [
            DiscoveredArticle(source_id=self.source.id, url=url, published_at_hint=NOW)
            for url in self.urls
        ]

    def fetch(self, article: DiscoveredArticle) -> RawArticle:
        if article.url in self.failing_urls:
            raise FetchError(self.source.id, f"boom on {article.url}")
        return RawArticle(
            source_id=self.source.id,
            url=article.url,
            final_url=article.url,
            raw_content=f"<html>{article.url}</html>",
            retrieved_at=NOW,
        )


@pytest.fixture
def window() -> DateWindow:
    return DateWindow(
        start=datetime(2026, 8, 11, tzinfo=UTC), end=datetime(2026, 8, 19, tzinfo=UTC)
    )


# --------------------------------------------------------------------------- #
# AC10 — partial failures
# --------------------------------------------------------------------------- #


def test_one_broken_source_does_not_stop_the_others(window: DateWindow) -> None:
    healthy_a = make_source("alpha")
    broken = make_source("bravo")
    healthy_b = make_source("charlie")

    adapters = {
        "alpha": StubAdapter(healthy_a, urls=["https://alpha.example/1"]),
        "bravo": StubAdapter(broken, discover_error=DiscoveryError("bravo", "feed is down")),
        "charlie": StubAdapter(
            healthy_b, urls=["https://charlie.example/1", "https://charlie.example/2"]
        ),
    }
    run = manifest()

    result = ingest_all(
        [healthy_a, broken, healthy_b],
        window,
        manifest=run,
        adapter_factory=lambda s: adapters[s.id],
    )

    assert [a.url for a in result.raw_articles] == [
        "https://alpha.example/1",
        "https://charlie.example/1",
        "https://charlie.example/2",
    ]
    assert run.sources_attempted == 3
    assert run.sources_succeeded == 2
    assert run.sources_failed == 1
    assert run.articles_discovered == 3


def test_the_failure_is_recorded_not_swallowed(window: DateWindow) -> None:
    broken = make_source("bravo")
    run = manifest()

    ingest_all(
        [broken],
        window,
        manifest=run,
        adapter_factory=lambda s: StubAdapter(s, discover_error=DiscoveryError(s.id, "feed down")),
    )

    assert len(run.errors) == 1
    error = run.errors[0]
    assert error.stage is PipelineStage.DISCOVER
    assert error.source_id == "bravo"
    assert error.exception_class == "DiscoveryError"
    assert "feed down" in error.message
    assert run.failed is True


def test_a_single_bad_article_does_not_fail_its_source(window: DateWindow) -> None:
    source = make_source("alpha")
    run = manifest()
    adapter = StubAdapter(
        source,
        urls=["https://alpha.example/1", "https://alpha.example/2", "https://alpha.example/3"],
        failing_urls={"https://alpha.example/2"},
    )

    outcome = ingest_source(adapter, window, manifest=run)

    assert [a.url for a in outcome.fetched] == [
        "https://alpha.example/1",
        "https://alpha.example/3",
    ]
    assert outcome.discovered == 3
    assert outcome.failed is False
    assert len(run.errors) == 1
    assert run.errors[0].stage is PipelineStage.FETCH


def test_an_unbuildable_adapter_is_recorded_and_skipped(window: DateWindow) -> None:
    """A source needing an uninstalled backend must not abort the run."""
    browser_source = make_source("needs-browser", "scrapling_dynamic")
    healthy = make_source("alpha")
    run = manifest()

    def factory(source: SourceConfig) -> SourceAdapter:
        if source.strategy is FetchStrategy.SCRAPLING_DYNAMIC:
            return build_adapter(source)  # raises UnsupportedStrategyError
        return StubAdapter(source, urls=["https://alpha.example/1"])

    result = ingest_all([browser_source, healthy], window, manifest=run, adapter_factory=factory)

    assert len(result.raw_articles) == 1
    assert run.sources_failed == 1
    assert run.sources_succeeded == 1
    assert run.errors[0].stage is PipelineStage.LOAD_CONFIG


def test_result_summaries_reflect_outcomes(window: DateWindow) -> None:
    good = make_source("alpha")
    bad = make_source("bravo")
    adapters = {
        "alpha": StubAdapter(good, urls=["https://alpha.example/1"]),
        "bravo": StubAdapter(bad, discover_error=DiscoveryError("bravo", "down")),
    }
    result = ingest_all(
        [good, bad], window, manifest=manifest(), adapter_factory=lambda s: adapters[s.id]
    )

    assert [o.source_id for o in result.succeeded] == ["alpha"]
    assert [o.source_id for o in result.failed] == ["bravo"]
    assert result.failed[0].reason is not None


def test_source_order_is_preserved(window: DateWindow) -> None:
    sources = [make_source(name) for name in ("charlie", "alpha", "bravo")]
    result = ingest_all(
        sources,
        window,
        manifest=manifest(),
        adapter_factory=lambda s: StubAdapter(s, urls=[f"https://{s.id}.example/1"]),
    )
    assert [a.source_id for a in result.raw_articles] == ["charlie", "alpha", "bravo"]
