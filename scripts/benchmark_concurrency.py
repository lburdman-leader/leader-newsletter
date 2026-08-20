#!/usr/bin/env python3
"""Measure what bounded concurrency buys the two network-bound stages.

Neither stage can be timed against the real thing here — that would mean an
OpenAI key and 145 live HTTP fetches — so both are driven by a fake that *sleeps*
for the latency measured on a real run:

* analysis: 6.0s per model call (the observed median of 62 calls, mean 6.1s);
* fetching: 1.0s per article (2:24 of ingestion over ~145 fetches).

Everything else is the production code path: the real analyzer, the real
`ingest_source`, the real ordering and manifest handling.

    python scripts/benchmark_concurrency.py                    # both stages
    python scripts/benchmark_concurrency.py --stage analysis --concurrency 1 8

This is a measurement tool, not part of the test suite: at the defaults it sleeps
for about a quarter of an hour, which is the point.
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from newsletter.ingestion.base import ingest_source
from newsletter.intelligence.analyzer import ArticleAnalyzer
from newsletter.intelligence.schemas import AssessmentPayload
from newsletter.models import (
    DateWindow,
    DiscoveredArticle,
    NormalizedArticle,
    RawArticle,
    RunManifest,
    SourceConfig,
    TopicCategory,
)

NOW = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)
WINDOW = DateWindow(start=datetime(2026, 8, 11, tzinfo=UTC), end=datetime(2026, 8, 19, tzinfo=UTC))

SOURCE = SourceConfig(
    id="bench",
    name="Benchmark Wire",
    entrypoint="https://bench.example/feed",
    strategy="rss",
    priority=5,
    category_hint=TopicCategory.AI_MODELS,
)


def payload(title: str) -> AssessmentPayload:
    return AssessmentPayload(
        category=TopicCategory.AI_MODELS,
        topic_relevance=4,
        business_impact=4,
        novelty=4,
        actionability=3,
        confidence=0.9,
        summary=f"Summary of {title}",
        why_it_matters="It changes what enterprise teams pay and plan for.",
        key_facts=["Available now"],
        event_subject="Example Labs",
        event_action="released",
        event_object="a model",
        event_date="2026-08-17",
    )


class SleepyModelClient:
    """A StructuredClient stand-in that waits exactly as long as the real one did."""

    def __init__(self, seconds: float) -> None:
        self.model = "gpt-4.1-mini"
        self.seconds = seconds

    def parse(self, *, instructions: str, content: str, schema: Any) -> tuple[Any, int]:
        time.sleep(self.seconds)
        return payload("benchmark article"), 1


class SleepyAdapter:
    """A source adapter whose fetch is one second of waiting, like a real page."""

    def __init__(self, urls: list[str], seconds: float) -> None:
        self.source = SOURCE
        self.urls = urls
        self.seconds = seconds

    def discover(self, window: DateWindow) -> list[DiscoveredArticle]:
        return [
            DiscoveredArticle(source_id=SOURCE.id, url=url, published_at_hint=NOW)
            for url in self.urls
        ]

    def fetch(self, article: DiscoveredArticle) -> RawArticle:
        time.sleep(self.seconds)
        return RawArticle(
            source_id=SOURCE.id,
            url=article.url,
            final_url=article.url,
            raw_content=f"<html>{article.url}</html>",
            retrieved_at=NOW,
        )


def articles(count: int) -> list[NormalizedArticle]:
    return [
        NormalizedArticle(
            article_id=f"a{index:04d}",
            source_id=SOURCE.id,
            canonical_url=f"https://bench.example/story-{index}",
            title=f"story {index}",
            published_at=NOW,
            clean_text="Body text long enough to look like an article.",
            content_hash=f"contenthash-{index}",
            retrieved_at=NOW,
        )
        for index in range(count)
    ]


def time_analysis(count: int, seconds: float, concurrency: int) -> tuple[float, int]:
    analyzer = ArticleAnalyzer(SleepyModelClient(seconds), concurrency=concurrency)
    manifest = RunManifest(run_id="bench", started_at=NOW)
    started = time.perf_counter()
    results = analyzer.analyze_all(articles(count), {SOURCE.id: SOURCE}, manifest=manifest, now=NOW)
    return time.perf_counter() - started, len(results)


def time_fetch(
    sources: int, per_source: int, seconds: float, concurrency: int
) -> tuple[float, int]:
    manifest = RunManifest(run_id="bench", started_at=NOW)
    fetched = 0
    started = time.perf_counter()
    for source_index in range(sources):
        adapter = SleepyAdapter(
            [f"https://bench.example/{source_index}/{i}" for i in range(per_source)], seconds
        )
        outcome = ingest_source(adapter, WINDOW, manifest=manifest, concurrency=concurrency)
        fetched += len(outcome.fetched)
    return time.perf_counter() - started, fetched


def report(label: str, rows: list[tuple[int, float, int]]) -> None:
    print(f"\n{label}")
    baseline = rows[0][1]
    for concurrency, elapsed, produced in rows:
        speedup = baseline / elapsed if elapsed else float("inf")
        minutes, secs = divmod(elapsed, 60)
        print(
            f"  concurrency {concurrency:>2}: {elapsed:7.1f}s "
            f"({int(minutes)}m{secs:04.1f}s)  x{speedup:.2f}  items={produced}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", choices=("analysis", "fetch", "both"), default="both")
    parser.add_argument("--concurrency", type=int, nargs="+", default=[1, 8])
    parser.add_argument("--articles", type=int, default=120)
    parser.add_argument("--call-seconds", type=float, default=6.0)
    parser.add_argument("--sources", type=int, default=8)
    parser.add_argument("--per-source", type=int, default=18)
    parser.add_argument("--fetch-seconds", type=float, default=1.0)
    parser.add_argument("--fetch-concurrency", type=int, nargs="+", default=[1, 6])
    args = parser.parse_args()

    if args.stage in ("analysis", "both"):
        rows = [
            (limit, *time_analysis(args.articles, args.call_seconds, limit))
            for limit in args.concurrency
        ]
        report(f"analysis: {args.articles} articles x {args.call_seconds}s per model call", rows)

    if args.stage in ("fetch", "both"):
        rows = [
            (limit, *time_fetch(args.sources, args.per_source, args.fetch_seconds, limit))
            for limit in args.fetch_concurrency
        ]
        report(
            f"fetch: {args.sources} sources x {args.per_source} articles "
            f"x {args.fetch_seconds}s per fetch",
            rows,
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
