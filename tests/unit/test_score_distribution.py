"""The score-distribution harness.

Two things have to be true of a calibration harness, and nothing else is worth a
test. It must report the distribution that is actually there, and it must agree
with production about what a score is -- a harness with its own arithmetic would
recalibrate the threshold against a formula the edition does not use.
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType

import pytest

from newsletter.config import AppConfig, CoverageFloor, NewsletterSettings, RuntimeSettings
from newsletter.models import (
    ArticleAssessment,
    AssessmentRecord,
    NormalizedArticle,
    SourceConfig,
    TopicCategory,
)
from newsletter.persistence.sqlite import Database
from newsletter.ranking.scoring import compute_score

REPO_ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)


def load_harness() -> ModuleType:
    """Import ``scripts/score_distribution.py``, which is not an installed module."""
    path = REPO_ROOT / "scripts" / "score_distribution.py"
    spec = importlib.util.spec_from_file_location("score_distribution", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


harness = load_harness()


# --------------------------------------------------------------------------- #
# a small corpus whose scores are known by hand
# --------------------------------------------------------------------------- #

SOURCES = {
    "beat": SourceConfig(
        id="beat",
        name="Beat Wire",
        entrypoint="https://beat.example/feed",
        strategy="rss",
        priority=7,
        category_hint=TopicCategory.YOUTUBE_PLATFORM,
    ),
    "trade": SourceConfig(
        id="trade",
        name="Trade Press",
        entrypoint="https://trade.example/feed",
        strategy="rss",
        priority=4,
        category_hint=TopicCategory.AI_BUSINESS,
    ),
}

#: ``(source, category, ratings, prompt_version)``. Scores are stated in the test
#: that reads them, so a change to the weights fails here loudly.
CORPUS = [
    ("beat", TopicCategory.YOUTUBE_PLATFORM, (5, 5, 5, 5), "v2"),  # 30+25+20+15+7 = 97
    ("beat", TopicCategory.KIDS_CONTENT, (4, 4, 3, 3), "v2"),  # 24+20+12+9+7  = 72
    ("beat", TopicCategory.YOUTUBE_MONETIZATION, (3, 3, 3, 2), "v2"),  # 18+15+12+6+7  = 58
    ("trade", TopicCategory.AI_BUSINESS, (3, 2, 2, 2), "v2"),  # 18+10+8+6+4   = 46
    ("trade", TopicCategory.AI_MODELS, (1, 1, 1, 1), "v2"),  # 6+5+4+3+4     = 22
    ("trade", TopicCategory.AI_BUSINESS, (5, 5, 5, 5), "v3"),  # 30+25+20+15+4 = 94, other rubric
]
V2_SCORES = [97, 72, 58, 46, 22]


def make_article(index: int, source_id: str) -> NormalizedArticle:
    return NormalizedArticle(
        article_id=f"article-{index:02d}",
        source_id=source_id,
        canonical_url=f"https://{source_id}.example/story-{index}",
        title=f"Story {index}",
        published_at=NOW,
        clean_text="Body text long enough to be a story.",
        content_hash=f"contenthash-{index:02d}",
        retrieved_at=NOW,
    )


def make_record(
    index: int, category: TopicCategory, ratings: tuple[int, int, int, int], prompt_version: str
) -> AssessmentRecord:
    relevance, impact, novelty, actionability = ratings
    return AssessmentRecord(
        assessment=ArticleAssessment(
            category=category,
            topic_relevance=relevance,
            business_impact=impact,
            novelty=novelty,
            actionability=actionability,
            confidence=0.9,
            summary=f"Summary {index}",
            why_it_matters=f"Why {index}",
        ),
        content_hash=f"contenthash-{index:02d}",
        model="gpt-4.1-mini",
        prompt_version=prompt_version,
        schema_version="1",
        created_at=NOW,
    )


@pytest.fixture
def database_url(tmp_path: Path) -> str:
    path = tmp_path / "newsletter.sqlite"
    with Database(path) as database:
        for source in SOURCES.values():
            database.upsert_source(source, now=NOW)
        for index, (source_id, category, ratings, version) in enumerate(CORPUS):
            article = make_article(index, source_id)
            database.save_articles([article])
            database.save_assessment(
                make_record(index, category, ratings, version), article_id=article.article_id
            )
    return f"sqlite:///{path.as_posix()}"


@pytest.fixture
def config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        sources=list(SOURCES.values()),
        newsletter=NewsletterSettings(
            min_score=62,
            coverage_floors={
                "own_beat": CoverageFloor(
                    categories=[
                        TopicCategory.YOUTUBE_PLATFORM,
                        TopicCategory.YOUTUBE_MONETIZATION,
                        TopicCategory.KIDS_CONTENT,
                    ],
                    minimum=4,
                )
            },
        ),
        runtime=RuntimeSettings(db_path=tmp_path / "newsletter.sqlite"),
    )


# --------------------------------------------------------------------------- #
# it reports what is there
# --------------------------------------------------------------------------- #


def test_the_prompt_version_filter_selects_one_rubric(database_url: str) -> None:
    """v2 and v3 have to be comparable, which means separable."""
    every = harness.score_everything(database_url)
    v2 = harness.score_everything(database_url, prompt_version="v2")
    v3 = harness.score_everything(database_url, prompt_version="v3")

    assert len(every) == len(CORPUS)
    assert sorted(a.final_score for a in v2) == sorted(V2_SCORES)
    assert [a.final_score for a in v3] == [94]


def test_the_summary_statistics_are_scores_that_exist(database_url: str) -> None:
    ranked = harness.score_everything(database_url, prompt_version="v2")
    measured = harness.measure([a.final_score for a in ranked], 62)

    assert (measured.count, measured.minimum, measured.maximum) == (5, 22, 97)
    # Nearest rank, never interpolation: a reported value is a real score.
    assert measured.median == 58
    assert measured.p90 == 97
    assert (measured.passing, round(measured.pass_rate, 2)) == (2, 0.4)


def test_the_histogram_covers_the_whole_range_and_closes_at_100(database_url: str) -> None:
    ranked = harness.score_everything(database_url, prompt_version="v2")
    bands = harness.measure([a.final_score for a in ranked], 62).histogram()

    assert len(bands) == 10
    assert bands[0][:2] == (0, 9)
    assert bands[-1][:2] == (90, 100), "the top band is closed, so a 100 is counted"
    assert sum(count for _, _, count in bands) == len(ranked)
    assert bands[9][2] == 1 and bands[7][2] == 1 and bands[2][2] == 1


def test_own_beat_headroom_is_reported_against_the_configured_floor(
    database_url: str, config: AppConfig
) -> None:
    """The recalibration target: how many beat stories a threshold leaves standing.

    ``selection.py`` seats a coverage-floor story only if it clears ``min_score``,
    so a threshold above the beat's mass starves the floor and prints short. At
    62 the corpus leaves two beat stories against a floor of four.
    """
    ranked = harness.score_everything(database_url, prompt_version="v2")
    report = harness.render(
        ranked, config=config, prompt_version="v2", threshold=62, with_categories=False
    )

    assert "own_beat (needs 4 per edition)" in report
    assert "STARVED" in report, "two beat stories against a floor of four is a starved floor"
    assert "total" in report and "2 of     3" in report


def test_the_category_breakdown_shows_the_inflation_tripwire(
    database_url: str, config: AppConfig
) -> None:
    """The share of general-AI rows rated relevant is what a loose rubric moves first."""
    ranked = harness.score_everything(database_url, prompt_version="v2")
    payload = harness.as_json(ranked, config=config, prompt_version="v2", threshold=62)

    assert payload["by_category"]["ai_business"] == {
        "count": 1,
        "median": 46,
        "passing": 0,
        "topic_relevance_ge_3": 1,
    }
    assert payload["by_category"]["ai_models"]["topic_relevance_ge_3"] == 0


# --------------------------------------------------------------------------- #
# it agrees with production
# --------------------------------------------------------------------------- #


def test_every_reported_score_is_the_production_score(database_url: str) -> None:
    """Computed both ways -- through the harness, and straight from ``scoring``.

    A calibration harness that drifts from ``compute_score`` recalibrates the
    threshold against a formula no edition uses, which is worse than not
    measuring at all.
    """
    for ranked in harness.score_everything(database_url):
        expected = compute_score(ranked.assessment, SOURCES[ranked.article.source_id])
        assert ranked.final_score == expected


def test_the_live_database_is_never_written_to(tmp_path: Path, database_url: str) -> None:
    """It is meant to be pointed at production, so it must open read-only."""
    path = tmp_path / "newsletter.sqlite"
    before = path.read_bytes()

    harness.score_everything(database_url)

    assert path.read_bytes() == before
    with Database(path, read_only=True) as database, pytest.raises(Exception, match="readonly"):
        database.upsert_source(SOURCES["beat"], now=NOW)
