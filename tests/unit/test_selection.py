"""Deterministic selection (AC9): same inputs and config, same edition."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from newsletter.config import NewsletterSettings
from newsletter.models import (
    ArticleAssessment,
    NormalizedArticle,
    RankedArticle,
    RunManifest,
    TopicCategory,
)
from newsletter.ranking.selection import (
    REASON_BELOW_THRESHOLD,
    REASON_CATEGORY_LIMIT,
    REASON_DUPLICATE_EVENT,
    REASON_EXCLUDED_CATEGORY,
    REASON_MAX_ITEMS,
    REASON_SOURCE_LIMIT,
    select,
)

NOW = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)
PUBLISHED = datetime(2026, 8, 17, 10, 0, tzinfo=UTC)

SETTINGS = NewsletterSettings(
    max_items=8,
    min_score=70,
    section_limits={
        TopicCategory.YOUTUBE_PLATFORM: 2,
        TopicCategory.YOUTUBE_MONETIZATION: 2,
        TopicCategory.AI_MODELS: 3,
        TopicCategory.AI_VIDEO: 3,
        TopicCategory.AI_BUSINESS: 2,
    },
    section_order=[
        TopicCategory.AI_MODELS,
        TopicCategory.YOUTUBE_PLATFORM,
        TopicCategory.YOUTUBE_MONETIZATION,
        TopicCategory.AI_VIDEO,
        TopicCategory.AI_BUSINESS,
    ],
)


def make_ranked(
    article_id: str,
    score: int,
    *,
    category: TopicCategory = TopicCategory.AI_MODELS,
    published_at: datetime = PUBLISHED,
    event: tuple[str, str, str, str] | None = None,
    **assessment_overrides: Any,
) -> RankedArticle:
    values: dict[str, Any] = {
        "category": category,
        "topic_relevance": 5,
        "business_impact": 4,
        "novelty": 5,
        "actionability": 3,
        "confidence": 0.9,
        "summary": f"Summary for {article_id}.",
        "why_it_matters": "It matters for enterprise readers.",
    }
    if event is not None:
        subject, action, obj, date = event
        values.update(event_subject=subject, event_action=action, event_object=obj, event_date=date)
    values.update(assessment_overrides)

    return RankedArticle(
        article=NormalizedArticle(
            article_id=article_id,
            source_id="wire",
            canonical_url=f"https://wire.example/{article_id}",
            title=f"Story {article_id}",
            published_at=published_at,
            clean_text="A long enough article body for selection tests to be realistic.",
            content_hash=f"contenthash-{article_id}",
            retrieved_at=NOW,
        ),
        assessment=ArticleAssessment(**values),
        source_name="Wire Example",
        source_priority=9,
        final_score=score,
    )


# --------------------------------------------------------------------------- #
# threshold
# --------------------------------------------------------------------------- #


def test_articles_below_the_threshold_are_excluded() -> None:
    result = select([make_ranked("high", 88), make_ranked("low", 69)], SETTINGS)

    assert [r.article.article_id for r in result.selected] == ["high"]
    assert result.rejected[0].reason == REASON_BELOW_THRESHOLD
    assert result.rejected[0].ranked.article.article_id == "low"


def test_the_threshold_is_inclusive() -> None:
    result = select([make_ranked("exactly", 70)], SETTINGS)
    assert [r.article.article_id for r in result.selected] == ["exactly"]


def test_an_edition_can_legitimately_be_empty() -> None:
    result = select([make_ranked("weak", 10), make_ranked("weaker", 5)], SETTINGS)
    assert result.is_empty
    assert result.lead is None
    assert result.reasons() == {REASON_BELOW_THRESHOLD: 2}


def test_above_threshold_is_counted_separately_from_selected() -> None:
    ranked = [make_ranked(f"a{i}", 90 - i) for i in range(12)]
    result = select(ranked, NewsletterSettings(max_items=3, min_score=70))

    assert len(result.selected) == 3
    assert result.above_threshold == 12


# --------------------------------------------------------------------------- #
# ordering and the lead story
# --------------------------------------------------------------------------- #


def test_selection_is_ordered_by_score() -> None:
    ranked = [make_ranked("mid", 80), make_ranked("top", 95), make_ranked("bottom", 72)]
    result = select(ranked, SETTINGS)
    assert [r.article.article_id for r in result.selected] == ["top", "mid", "bottom"]


def test_the_lead_is_the_highest_scoring_story() -> None:
    result = select([make_ranked("second", 80), make_ranked("first", 99)], SETTINGS)
    assert result.lead.article.article_id == "first"


def test_ties_are_broken_by_publication_then_id() -> None:
    ranked = [
        make_ranked("zzz", 88, published_at=datetime(2026, 8, 15, tzinfo=UTC)),
        make_ranked("aaa", 88, published_at=datetime(2026, 8, 16, tzinfo=UTC)),
        make_ranked("mmm", 88, published_at=datetime(2026, 8, 15, tzinfo=UTC)),
    ]
    result = select(ranked, SETTINGS)
    assert [r.article.article_id for r in result.selected] == ["mmm", "zzz", "aaa"]


def test_the_same_dataset_always_produces_the_same_selection() -> None:
    ranked = [
        make_ranked("a", 91, category=TopicCategory.AI_MODELS),
        make_ranked("b", 85, category=TopicCategory.AI_VIDEO),
        make_ranked("c", 85, category=TopicCategory.AI_MODELS),
        make_ranked("d", 72, category=TopicCategory.AI_BUSINESS),
        make_ranked("e", 69, category=TopicCategory.AI_MODELS),
    ]
    first = [r.article.article_id for r in select(ranked, SETTINGS).selected]
    second = [r.article.article_id for r in select(list(reversed(ranked)), SETTINGS).selected]
    assert first == second == ["a", "b", "c", "d"]


# --------------------------------------------------------------------------- #
# limits — no category may monopolise the edition
# --------------------------------------------------------------------------- #


def test_the_per_category_limit_is_respected() -> None:
    ranked = [make_ranked(f"m{i}", 95 - i, category=TopicCategory.AI_MODELS) for i in range(6)]
    result = select(ranked, SETTINGS)

    assert len(result.selected) == 3  # ai_models limit
    assert [r.article.article_id for r in result.selected] == ["m0", "m1", "m2"]
    assert result.reasons() == {REASON_CATEGORY_LIMIT: 3}


def test_a_capped_category_does_not_block_others() -> None:
    ranked = [
        *[make_ranked(f"m{i}", 95 - i, category=TopicCategory.AI_MODELS) for i in range(5)],
        make_ranked("v1", 75, category=TopicCategory.AI_VIDEO),
        make_ranked("b1", 74, category=TopicCategory.AI_BUSINESS),
    ]
    result = select(ranked, SETTINGS)
    categories = [r.assessment.category for r in result.selected]

    assert categories.count(TopicCategory.AI_MODELS) == 3
    assert TopicCategory.AI_VIDEO in categories
    assert TopicCategory.AI_BUSINESS in categories


def test_max_items_caps_the_whole_edition() -> None:
    ranked = [make_ranked(f"m{i}", 95 - i, category=TopicCategory.AI_MODELS) for i in range(3)] + [
        make_ranked(f"v{i}", 90 - i, category=TopicCategory.AI_VIDEO) for i in range(3)
    ]
    result = select(ranked, NewsletterSettings(max_items=4, min_score=70))

    assert len(result.selected) == 4
    assert result.reasons()[REASON_MAX_ITEMS] == 2


def test_a_category_without_a_configured_limit_uses_max_items() -> None:
    settings = NewsletterSettings(max_items=2, min_score=70, section_limits={})
    ranked = [make_ranked(f"a{i}", 90 - i) for i in range(5)]
    assert len(select(ranked, settings).selected) == 2


def test_excluded_categories_never_appear() -> None:
    ranked = [
        make_ranked("keep", 88, category=TopicCategory.AI_MODELS),
        make_ranked("drop", 99, category=TopicCategory.OTHER),
    ]
    result = select(ranked, SETTINGS)

    assert [r.article.article_id for r in result.selected] == ["keep"]
    assert result.rejected[0].reason == REASON_EXCLUDED_CATEGORY


def test_a_high_scoring_excluded_article_is_not_counted_above_threshold() -> None:
    result = select([make_ranked("drop", 99, category=TopicCategory.OTHER)], SETTINGS)
    assert result.above_threshold == 0


# --------------------------------------------------------------------------- #
# event collapse
# --------------------------------------------------------------------------- #


EVENT = ("Example Labs", "released", "Reasoning model", "2026-08-17")


def test_two_articles_about_one_event_collapse_to_the_better_one() -> None:
    ranked = [
        make_ranked("weaker", 80, event=EVENT),
        make_ranked("stronger", 92, event=EVENT),
    ]
    result = select(ranked, SETTINGS)

    assert [r.article.article_id for r in result.selected] == ["stronger"]
    assert result.rejected[0].reason == REASON_DUPLICATE_EVENT


def test_different_events_are_kept_apart() -> None:
    other = ("Other Corp", "acquired", "A startup", "2026-08-16")
    result = select(
        [make_ranked("a", 90, event=EVENT), make_ranked("b", 88, event=other)], SETTINGS
    )
    assert len(result.selected) == 2


def test_articles_without_a_complete_fingerprint_are_never_collapsed() -> None:
    partial = ("Example Labs", "released", "", "")
    ranked = [make_ranked("a", 90, event=partial), make_ranked("b", 88, event=partial)]
    assert len(select(ranked, SETTINGS).selected) == 2


def test_event_collapse_can_be_switched_off() -> None:
    settings = NewsletterSettings(max_items=8, min_score=70, collapse_events=False)
    ranked = [make_ranked("a", 90, event=EVENT), make_ranked("b", 88, event=EVENT)]
    assert len(select(ranked, settings).selected) == 2


# --------------------------------------------------------------------------- #
# sections
# --------------------------------------------------------------------------- #


def test_sections_follow_the_configured_order() -> None:
    ranked = [
        make_ranked("lead", 99, category=TopicCategory.AI_BUSINESS),
        make_ranked("video", 90, category=TopicCategory.AI_VIDEO),
        make_ranked("model", 88, category=TopicCategory.AI_MODELS),
        make_ranked("yt", 85, category=TopicCategory.YOUTUBE_PLATFORM),
    ]
    result = select(ranked, SETTINGS)
    assert [category for category, _ in result.sections(SETTINGS)] == [
        TopicCategory.AI_MODELS,
        TopicCategory.YOUTUBE_PLATFORM,
        TopicCategory.AI_VIDEO,
    ]


def test_the_lead_story_is_not_repeated_in_its_section() -> None:
    ranked = [
        make_ranked("lead", 99, category=TopicCategory.AI_MODELS),
        make_ranked("second", 88, category=TopicCategory.AI_MODELS),
    ]
    result = select(ranked, SETTINGS)
    sections = dict(result.sections(SETTINGS))

    assert result.lead.article.article_id == "lead"
    assert [r.article.article_id for r in sections[TopicCategory.AI_MODELS]] == ["second"]


def test_a_section_holding_only_the_lead_disappears() -> None:
    ranked = [
        make_ranked("lead", 99, category=TopicCategory.AI_MODELS),
        make_ranked("other", 88, category=TopicCategory.AI_VIDEO),
    ]
    sections = select(ranked, SETTINGS).sections(SETTINGS)
    assert [category for category, _ in sections] == [TopicCategory.AI_VIDEO]


def test_stories_inside_a_section_stay_in_score_order() -> None:
    ranked = [
        make_ranked("lead", 99, category=TopicCategory.AI_VIDEO),
        make_ranked("mid", 85, category=TopicCategory.AI_MODELS),
        make_ranked("top", 92, category=TopicCategory.AI_MODELS),
    ]
    sections = dict(select(ranked, SETTINGS).sections(SETTINGS))
    assert [r.article.article_id for r in sections[TopicCategory.AI_MODELS]] == ["top", "mid"]


def test_an_empty_selection_has_no_sections() -> None:
    assert select([make_ranked("weak", 10)], SETTINGS).sections(SETTINGS) == []


# --------------------------------------------------------------------------- #
# reporting
# --------------------------------------------------------------------------- #


def test_the_manifest_records_selection_counts() -> None:
    manifest = RunManifest(run_id="r1", started_at=NOW)
    ranked = [make_ranked(f"a{i}", 90 - i) for i in range(5)] + [make_ranked("weak", 40)]

    select(ranked, NewsletterSettings(max_items=2, min_score=70), manifest=manifest)

    assert manifest.articles_above_threshold == 5
    assert manifest.articles_selected == 2


def test_every_rejection_carries_a_reason() -> None:
    ranked = [
        *[make_ranked(f"m{i}", 95 - i, category=TopicCategory.AI_MODELS) for i in range(5)],
        make_ranked("weak", 20),
        make_ranked("other", 99, category=TopicCategory.OTHER),
    ]
    result = select(ranked, SETTINGS)

    assert {item.reason for item in result.rejected} == {
        REASON_CATEGORY_LIMIT,
        REASON_BELOW_THRESHOLD,
        REASON_EXCLUDED_CATEGORY,
    }
    assert len(result.selected) + len(result.rejected) == len(ranked)


# --------------------------------------------------------------------------- #
# per-source cap — no one publication takes over an edition
# --------------------------------------------------------------------------- #


def make_from(source_id: str, article_id: str, score: int, category=TopicCategory.AI_MODELS):
    ranked = make_ranked(article_id, score, category=category)
    return ranked.model_copy(
        update={"article": ranked.article.model_copy(update={"source_id": source_id})}
    )


CAPPED = NewsletterSettings(max_items=8, min_score=70, max_per_source=2)


def test_one_source_cannot_fill_the_edition() -> None:
    ranked = [make_from("loudest", f"a{i}", 95 - i) for i in range(5)]
    result = select(ranked, CAPPED)

    assert len(result.selected) == 2
    assert result.reasons() == {REASON_SOURCE_LIMIT: 3}


def test_the_cap_keeps_the_best_from_each_source() -> None:
    ranked = [
        make_from("loudest", "top", 95),
        make_from("loudest", "second", 90),
        make_from("loudest", "third", 85),
        make_from("quieter", "other", 80),
    ]
    result = select(ranked, CAPPED)

    assert [r.article.article_id for r in result.selected] == ["top", "second", "other"]


def test_a_capped_source_does_not_block_a_lower_scoring_one() -> None:
    """The point of the cap: room is left for other voices."""
    ranked = [make_from("loudest", f"a{i}", 95 - i) for i in range(4)]
    ranked.append(make_from("quieter", "quiet", 71))

    selected = select(ranked, CAPPED).selected

    assert {r.article.source_id for r in selected} == {"loudest", "quieter"}


def test_no_cap_by_default() -> None:
    """Existing configurations keep working; the cap is opt-in."""
    ranked = [make_from("loudest", f"a{i}", 95 - i) for i in range(4)]
    settings = NewsletterSettings(max_items=8, min_score=70)

    assert settings.max_per_source is None
    assert len(select(ranked, settings).selected) == 4


def test_the_cap_is_reported_like_every_other_rejection() -> None:
    ranked = [make_from("loudest", f"a{i}", 95 - i) for i in range(4)]
    result = select(ranked, CAPPED)

    assert len(result.selected) + len(result.rejected) == len(ranked)
    assert all(item.reason == REASON_SOURCE_LIMIT for item in result.rejected)
