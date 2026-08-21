"""Adaptive assessment: how much of the week a run actually pays to read.

The pool is bounded, so these tests pin the two things the bound must never do --
crowd out a reader's reserved link, or stop while a coverage floor is still unmet.
The second is the specific bug the two features exist together to avoid: an
edition of ten stories from the wrong beat is not a finished edition.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from newsletter.config import CoverageFloor, NewsletterSettings
from newsletter.models import (
    ArticleAssessment,
    AssessmentRecord,
    NormalizedArticle,
    RunManifest,
    SourceConfig,
    TopicCategory,
)
from newsletter.pipeline import AnalysisBudget, analyze_pool

NOW = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)

SUBMISSIONS = "reader-submissions"
OWN_BEAT = [
    TopicCategory.YOUTUBE_PLATFORM,
    TopicCategory.YOUTUBE_MONETIZATION,
    TopicCategory.KIDS_CONTENT,
]

#: Ten stories, a floor of four, batches of five out of at most twenty.
SETTINGS = NewsletterSettings(
    max_items=10,
    min_score=70,
    analysis_pool_min=5,
    analysis_pool_max=20,
    coverage_floors={"own_beat": CoverageFloor(categories=OWN_BEAT, minimum=4)},
)

SOURCES: dict[str, SourceConfig] = {
    source_id: SourceConfig(
        id=source_id,
        name=source_id.title(),
        entrypoint=f"https://{source_id}.example/feed",
        strategy="rss",
        priority=priority,
        category_hint=TopicCategory.AI_MODELS,
    )
    for source_id, priority in (("wire", 5), (SUBMISSIONS, 7))
}

PRIORITIES = {source_id: source.priority for source_id, source in SOURCES.items()}


def make_article(article_id: str, *, source_id: str = "wire", age: int = 0) -> NormalizedArticle:
    return NormalizedArticle(
        article_id=article_id,
        source_id=source_id,
        canonical_url=f"https://{source_id}.example/{article_id}",
        title=f"Story {article_id}",
        published_at=NOW - timedelta(hours=age),
        clean_text="A long enough article body for the analysis-pool tests.",
        content_hash=f"contenthash-{article_id}",
        retrieved_at=NOW,
    )


class RecordingAnalyzer:
    """Assesses whatever it is handed, and remembers what that was.

    ``scores`` maps article id -> the four ratings; anything unnamed is assessed
    as a strong AI story, which is the pool shape the floor has to survive.
    """

    model = "fake-model"
    prompt_version = "v2"
    schema_version = "2"

    def __init__(self, categories: Mapping[str, TopicCategory] | None = None) -> None:
        self.categories = dict(categories or {})
        self.seen: list[str] = []

    def analyze_all(
        self,
        articles: Iterable[NormalizedArticle],
        sources_by_id: Mapping[str, SourceConfig],
        *,
        manifest: RunManifest,
        now: datetime | None = None,
    ) -> list[tuple[NormalizedArticle, AssessmentRecord]]:
        assessed = []
        for article in articles:
            self.seen.append(article.article_id)
            manifest.llm_calls += 1
            assessed.append((article, self._record(article)))
        return assessed

    def _record(self, article: NormalizedArticle) -> AssessmentRecord:
        return AssessmentRecord(
            assessment=ArticleAssessment(
                category=self.categories.get(article.article_id, TopicCategory.AI_MODELS),
                topic_relevance=5,
                business_impact=5,
                novelty=5,
                actionability=5,
                confidence=0.9,
                summary=f"Summary for {article.article_id}.",
                why_it_matters="It matters for enterprise readers.",
            ),
            content_hash=article.content_hash,
            model=self.model,
            prompt_version=self.prompt_version,
            schema_version=self.schema_version,
            created_at=NOW,
        )


def run(
    analyzer: RecordingAnalyzer,
    candidates: Sequence[NormalizedArticle],
    settings: NewsletterSettings = SETTINGS,
    **budget_overrides: Any,
) -> RunManifest:
    manifest = RunManifest(run_id="r1", started_at=NOW)
    budget = AnalysisBudget(
        settings=settings,
        sources=SOURCES,
        published=None,
        reserved_source_id=budget_overrides.pop("reserved_source_id", None),
        reserved_slots=budget_overrides.pop("reserved_slots", None),
        priorities=PRIORITIES,
    )
    analyze_pool(analyzer, list(candidates), budget=budget, manifest=manifest, now=NOW)
    return manifest


# --------------------------------------------------------------------------- #
# submissions are outside the budget
# --------------------------------------------------------------------------- #


def test_submissions_are_assessed_first_and_the_cap_applies_only_to_the_rest() -> None:
    """A link a reader was promised a slot for is never crowded out by a budget."""
    readers = [make_article(f"reader{index}", source_id=SUBMISSIONS) for index in range(3)]
    pool = [make_article(f"wire{index:03d}", age=index) for index in range(200)]
    analyzer = RecordingAnalyzer()

    manifest = run(analyzer, [*pool, *readers], reserved_source_id=SUBMISSIONS)

    assert analyzer.seen[:3] == ["reader0", "reader1", "reader2"]
    # Twenty from the capped pool, and the three submissions on top of them.
    assert len(analyzer.seen) == 23
    assert manifest.articles_analyzed == 23
    assert manifest.articles_available == 203


# --------------------------------------------------------------------------- #
# the stopping rule — the coupling between the two features
# --------------------------------------------------------------------------- #


def test_batching_stops_as_soon_as_the_edition_could_be_published() -> None:
    """Ten stories and the floor met after two batches; the rest is never read."""
    beat = {f"wire{index:03d}": TopicCategory.KIDS_CONTENT for index in range(4)}
    pool = [make_article(f"wire{index:03d}", age=index) for index in range(200)]

    analyzer = RecordingAnalyzer(beat)
    manifest = run(analyzer, pool)

    assert len(analyzer.seen) == 10
    assert manifest.articles_analyzed == 10


def test_batching_does_not_stop_early_while_a_coverage_floor_is_unmet() -> None:
    """The bug the coupling exists to prevent.

    Ten publishable stories arrive in the first batches, so a loop that stopped
    on "the edition is full" would stop at ten -- and the four beat stories that
    make the edition worth publishing sit at positions 15 to 18.
    """
    beat = {f"wire{index:03d}": TopicCategory.KIDS_CONTENT for index in range(15, 19)}
    pool = [make_article(f"wire{index:03d}", age=index) for index in range(200)]

    analyzer = RecordingAnalyzer(beat)
    manifest = run(analyzer, pool)

    assert len(analyzer.seen) == 20
    assert manifest.articles_analyzed == 20
    assert "wire018" in analyzer.seen


def test_an_unsatisfiable_floor_stops_at_the_cap_rather_than_reading_everything() -> None:
    """No qualifying story exists, so the loop exhausts its budget and no more."""
    analyzer = RecordingAnalyzer()

    manifest = run(analyzer, [make_article(f"wire{index:03d}", age=index) for index in range(200)])

    assert manifest.articles_analyzed == 20


# --------------------------------------------------------------------------- #
# the escape hatch, and determinism
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("uncapped", [0, None])
def test_removing_the_cap_restores_exhaustive_assessment_exactly(uncapped: int | None) -> None:
    """Down to the order: one pass over the pool as deduplication left it."""
    pool = [make_article(f"wire{index:03d}", age=index) for index in range(37)]
    analyzer = RecordingAnalyzer()

    manifest = run(analyzer, pool, SETTINGS.model_copy(update={"analysis_pool_max": uncapped}))

    assert analyzer.seen == [article.article_id for article in pool]
    assert manifest.articles_analyzed == 37


def test_the_same_pool_is_read_in_the_same_order_however_it_arrives() -> None:
    """AC9 reaches the batching: same pool, same batches, same stopping point."""
    pool = [make_article(f"wire{index:03d}", age=index) for index in range(60)]
    pool += [make_article(f"other{index:03d}", source_id=SUBMISSIONS) for index in range(2)]

    forward = RecordingAnalyzer()
    backward = RecordingAnalyzer()
    run(forward, pool, reserved_source_id=SUBMISSIONS)
    run(backward, list(reversed(pool)), reserved_source_id=SUBMISSIONS)

    assert forward.seen == backward.seen
