"""The score formula (AC8): computed in Python, never by the model."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from newsletter.models import (
    ArticleAssessment,
    AssessmentRecord,
    NormalizedArticle,
    RankedArticle,
    SourceConfig,
    TopicCategory,
)
from newsletter.ranking.scoring import (
    MAX_SCORE,
    MIN_SCORE,
    compute_score,
    rank_all,
    rank_article,
    ranking_key,
    score_breakdown,
)

NOW = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)
PUBLISHED = datetime(2026, 8, 17, 10, 0, tzinfo=UTC)


def make_assessment(**overrides: Any) -> ArticleAssessment:
    values: dict[str, Any] = {
        "category": TopicCategory.AI_MODELS,
        "topic_relevance": 5,
        "business_impact": 4,
        "novelty": 5,
        "actionability": 3,
        "confidence": 0.9,
        "summary": "A model was released.",
        "why_it_matters": "It changes cost per token.",
    }
    values.update(overrides)
    return ArticleAssessment(**values)


def make_source(priority: int = 9, **overrides: Any) -> SourceConfig:
    values: dict[str, Any] = {
        "id": "wire",
        "name": "Wire Example",
        "entrypoint": "https://wire.example/feed",
        "strategy": "rss",
        "priority": priority,
    }
    values.update(overrides)
    return SourceConfig(**values)


def make_article(article_id: str = "a1", published_at: datetime = PUBLISHED) -> NormalizedArticle:
    return NormalizedArticle(
        article_id=article_id,
        source_id="wire",
        canonical_url=f"https://wire.example/{article_id}",
        title=f"Story {article_id}",
        published_at=published_at,
        clean_text="A long enough article body for scoring tests to be realistic.",
        content_hash=f"contenthash-{article_id}",
        retrieved_at=NOW,
    )


def make_record(assessment: ArticleAssessment | None = None) -> AssessmentRecord:
    return AssessmentRecord(
        assessment=assessment or make_assessment(),
        content_hash="contenthash-a1",
        model="gpt-4.1-mini",
        prompt_version="v1",
        schema_version="1",
        created_at=NOW,
    )


# --------------------------------------------------------------------------- #
# the formula
# --------------------------------------------------------------------------- #


def test_the_documented_worked_example() -> None:
    """5x6 + 4x5 + 5x4 + 3x3 + 9 = 30 + 20 + 20 + 9 + 9 = 88."""
    assert compute_score(make_assessment(), make_source(priority=9)) == 88


@pytest.mark.parametrize(
    ("ratings", "priority", "expected"),
    [
        ((0, 0, 0, 0), 0, 0),
        ((5, 5, 5, 5), 10, 100),
        ((5, 0, 0, 0), 0, 30),
        ((0, 5, 0, 0), 0, 25),
        ((0, 0, 5, 0), 0, 20),
        ((0, 0, 0, 5), 0, 15),
        ((0, 0, 0, 0), 7, 7),
        ((1, 1, 1, 1), 1, 19),
        ((3, 3, 3, 3), 5, 59),
    ],
)
def test_each_component_contributes_its_weight(
    ratings: tuple[int, int, int, int], priority: int, expected: int
) -> None:
    relevance, impact, novelty, actionability = ratings
    assessment = make_assessment(
        topic_relevance=relevance,
        business_impact=impact,
        novelty=novelty,
        actionability=actionability,
    )
    assert compute_score(assessment, make_source(priority=priority)) == expected


def test_the_score_stays_inside_its_documented_range() -> None:
    worst = compute_score(
        make_assessment(topic_relevance=0, business_impact=0, novelty=0, actionability=0),
        make_source(priority=0),
    )
    best = compute_score(
        make_assessment(topic_relevance=5, business_impact=5, novelty=5, actionability=5),
        make_source(priority=10),
    )
    assert (worst, best) == (MIN_SCORE, MAX_SCORE)


def test_relevance_outweighs_every_other_dimension() -> None:
    """The weights encode editorial policy; this pins the intended ordering."""
    relevance_only = compute_score(
        make_assessment(topic_relevance=5, business_impact=0, novelty=0, actionability=0),
        make_source(priority=0),
    )
    actionability_only = compute_score(
        make_assessment(topic_relevance=0, business_impact=0, novelty=0, actionability=5),
        make_source(priority=0),
    )
    assert relevance_only > actionability_only


def test_source_priority_shifts_the_score() -> None:
    assessment = make_assessment()
    assert (
        compute_score(assessment, make_source(priority=10))
        - compute_score(assessment, make_source(priority=3))
        == 7
    )


def test_the_score_is_a_pure_function() -> None:
    assessment, source = make_assessment(), make_source()
    assert compute_score(assessment, source) == compute_score(assessment, source)


# --------------------------------------------------------------------------- #
# breakdown
# --------------------------------------------------------------------------- #


def test_the_breakdown_always_agrees_with_the_score() -> None:
    for priority in (0, 5, 10):
        for rating in range(6):
            assessment = make_assessment(
                topic_relevance=rating,
                business_impact=rating,
                novelty=rating,
                actionability=rating,
            )
            source = make_source(priority=priority)
            assert score_breakdown(assessment, source).total == compute_score(assessment, source)


def test_the_breakdown_itemises_every_component() -> None:
    breakdown = score_breakdown(make_assessment(), make_source(priority=9)).as_dict()
    assert breakdown == {
        "topic_relevance": 30,
        "business_impact": 20,
        "novelty": 20,
        "actionability": 9,
        "source_priority": 9,
        "total": 88,
    }


# --------------------------------------------------------------------------- #
# the model never controls the score (AC8)
# --------------------------------------------------------------------------- #


def test_the_assessment_model_cannot_express_a_score() -> None:
    assert "final_score" not in ArticleAssessment.model_fields
    assert "score" not in ArticleAssessment.model_fields


def test_a_model_supplied_score_cannot_be_smuggled_in() -> None:
    with pytest.raises(ValueError):
        ArticleAssessment(**{**make_assessment().model_dump(), "final_score": 100})


def test_the_ranked_score_comes_from_the_formula_not_the_payload() -> None:
    ranked = rank_article(make_article(), make_assessment(), make_source(priority=9))
    assert ranked.final_score == compute_score(ranked.assessment, make_source(priority=9)) == 88


# --------------------------------------------------------------------------- #
# ranking
# --------------------------------------------------------------------------- #


def test_rank_article_carries_source_provenance() -> None:
    ranked = rank_article(make_article(), make_assessment(), make_source(priority=9))
    assert isinstance(ranked, RankedArticle)
    assert ranked.source_name == "Wire Example"
    assert ranked.source_priority == 9
    assert ranked.article.article_id == "a1"


def test_rank_all_orders_by_score_descending() -> None:
    low = (make_article("low"), make_record(make_assessment(topic_relevance=1, business_impact=1)))
    high = (make_article("high"), make_record(make_assessment()))
    ranked = rank_all([low, high], {"wire": make_source()})
    assert [r.article.article_id for r in ranked] == ["high", "low"]


def test_ties_are_broken_by_publication_then_id() -> None:
    earlier = (make_article("zzz", datetime(2026, 8, 15, tzinfo=UTC)), make_record())
    later = (make_article("aaa", datetime(2026, 8, 16, tzinfo=UTC)), make_record())
    ranked = rank_all([later, earlier], {"wire": make_source()})
    assert [r.article.article_id for r in ranked] == ["zzz", "aaa"]


def test_a_full_tie_falls_back_to_article_id() -> None:
    first = (make_article("bbb"), make_record())
    second = (make_article("aaa"), make_record())
    ranked = rank_all([first, second], {"wire": make_source()})
    assert [r.article.article_id for r in ranked] == ["aaa", "bbb"]


def test_ranking_is_independent_of_input_order() -> None:
    pairs = [
        (make_article("a"), make_record(make_assessment(topic_relevance=3))),
        (make_article("b"), make_record(make_assessment(topic_relevance=5))),
        (make_article("c"), make_record(make_assessment(topic_relevance=4))),
    ]
    forward = [r.article.article_id for r in rank_all(pairs, {"wire": make_source()})]
    backward = [
        r.article.article_id for r in rank_all(list(reversed(pairs)), {"wire": make_source()})
    ]
    assert forward == backward == ["b", "c", "a"]


def test_articles_from_an_unknown_source_are_skipped() -> None:
    assert rank_all([(make_article(), make_record())], {}) == []


def test_ranking_key_sorts_best_first() -> None:
    strong = rank_article(make_article("s"), make_assessment(), make_source(priority=10))
    weak = rank_article(
        make_article("w"), make_assessment(topic_relevance=0), make_source(priority=0)
    )
    assert ranking_key(strong) < ranking_key(weak)
