"""Deterministic selection (AC9): same inputs and config, same edition."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from newsletter.config import NewsletterSettings
from newsletter.models import (
    ArticleAssessment,
    NormalizedArticle,
    RankedArticle,
    RunManifest,
    TopicCategory,
)
from newsletter.ranking.dedupe import PublishedKeys
from newsletter.ranking.selection import (
    REASON_ALREADY_PUBLISHED,
    REASON_BELOW_THRESHOLD,
    REASON_CATEGORY_LIMIT,
    REASON_DUPLICATE_EVENT,
    REASON_EXCLUDED_CATEGORY,
    REASON_MAX_ITEMS,
    REASON_SIMILAR_EVENT,
    REASON_SOURCE_LIMIT,
    REASON_SUBJECT_LIMIT,
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
    event: tuple[str | None, str | None, str | None, str | None] | None = None,
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


def test_the_threshold_is_inclusive_and_excludes_the_point_below() -> None:
    """``min_score`` is 70: 70 prints, 69 is rejected and says why."""
    result = select(
        [make_ranked("high", 88), make_ranked("exactly", 70), make_ranked("low", 69)], SETTINGS
    )

    assert [r.article.article_id for r in result.selected] == ["high", "exactly"]
    assert result.rejected[0].reason == REASON_BELOW_THRESHOLD
    assert result.rejected[0].ranked.article.article_id == "low"


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


def test_selection_is_ordered_by_score_and_the_lead_is_the_best_of_it() -> None:
    ranked = [make_ranked("mid", 80), make_ranked("top", 95), make_ranked("bottom", 72)]
    result = select(ranked, SETTINGS)

    assert [r.article.article_id for r in result.selected] == ["top", "mid", "bottom"]
    assert result.lead.article.article_id == "top"


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


#: The owner complaint: one launch ran as three stories in three sections. Each
#: outlet chose a different verb, and only one of them dated the announcement.
CHATGPT_FOR_TEENS = (
    ("OpenAI", "launches", "ChatGPT for Teens", None),
    ("OpenAI", "introduces", "chatgpt-for-teens", "2026-08-14"),
    ("openai", "announces", "The ChatGPT For Teens", None),
)


def test_one_launch_reported_three_ways_runs_as_a_single_story() -> None:
    """Collapse runs before the category caps, which would otherwise spread it.

    Three copies filed under three categories used to pass three different caps
    and print three times. Fed weakest-first, so the surviving copy is the best
    of the three rather than whichever one arrived first.
    """
    categories = [TopicCategory.AI_MODELS, TopicCategory.AI_VIDEO, TopicCategory.AI_BUSINESS]
    ranked = [
        make_ranked(f"teens{index}", 95 - index, category=category, event=event)
        for index, (event, category) in enumerate(zip(CHATGPT_FOR_TEENS, categories, strict=True))
    ]
    result = select(list(reversed(ranked)), SETTINGS)

    assert [r.article.article_id for r in result.selected] == ["teens0"]
    assert result.reasons() == {REASON_DUPLICATE_EVENT: 2}


def test_a_second_event_from_the_same_company_is_not_collapsed_into_the_first() -> None:
    """Aggressive collapse must not become "one story per company"."""
    ranked = [
        make_ranked("launch", 95, event=("OpenAI", "launches", "ChatGPT for Teens", None)),
        make_ranked("deal", 92, event=("OpenAI", "signs", "a data centre deal", "2026-08-15")),
    ]
    assert [r.article.article_id for r in select(ranked, SETTINGS).selected] == ["launch", "deal"]


# --------------------------------------------------------------------------- #
# across editions — every story is printed only once
# --------------------------------------------------------------------------- #


def test_a_story_an_earlier_edition_printed_is_not_reprinted() -> None:
    """And it says which issue carried it: a suppression that cannot explain
    itself is a silent failure."""
    published = PublishedKeys(by_article_id={"reprint": "2026-W33"})
    result = select(
        [make_ranked("reprint", 95), make_ranked("fresh", 80)], SETTINGS, published=published
    )

    assert [r.article.article_id for r in result.selected] == ["fresh"]
    assert result.reasons() == {REASON_ALREADY_PUBLISHED: 1}
    assert result.rejections_for(REASON_ALREADY_PUBLISHED)[0].detail == (
        "already published in 2026-W33"
    )


def test_the_same_text_republished_at_a_new_url_is_suppressed() -> None:
    published = PublishedKeys(by_content_hash={"contenthash-syndicated": "2026-W30"})
    result = select([make_ranked("syndicated", 95)], SETTINGS, published=published)

    assert result.is_empty
    assert result.reasons() == {REASON_ALREADY_PUBLISHED: 1}


def test_a_repeated_headline_does_not_suppress_a_new_story() -> None:
    """A recurring beat reuses its headline; a permanent block must not.

    "YouTube changes its monetization rules" can be March's story and September's.
    Inside one run the shared headline collapses them (see ``test_dedupe``); across
    editions it would kill September's forever, so the key is not carried here.
    """
    reused = make_ranked("rewritten-elsewhere", 95)
    published = PublishedKeys(by_article_id={"an-older-article": "2026-W29"})
    result = select([reused], SETTINGS, published=published)

    assert [r.article.article_id for r in result.selected] == ["rewritten-elsewhere"]


def test_a_follow_up_on_the_same_subject_and_object_is_still_published() -> None:
    """Suppression is on identity, never on topic.

    Next month's regulatory fight over ChatGPT for Teens is news, not a duplicate
    of last month's launch, and blocking the subject would kill it forever.
    """
    launch = make_ranked("launch", 95, event=("OpenAI", "launches", "ChatGPT for Teens", None))
    follow_up = make_ranked(
        "regulator", 90, event=("OpenAI", "is investigated over", "ChatGPT for Teens", "2026-08-15")
    )
    published = PublishedKeys(
        by_article_id={"launch": "2026-W29"},
        by_content_hash={"contenthash-launch": "2026-W29"},
    )
    result = select([launch, follow_up], SETTINGS, published=published)

    assert [r.article.article_id for r in result.selected] == ["regulator"]


def test_nothing_is_suppressed_when_no_edition_has_been_published() -> None:
    ranked = [make_ranked("a", 90), make_ranked("b", 88)]
    assert select(ranked, SETTINGS, published=PublishedKeys()).reasons() == {}
    assert len(select(ranked, SETTINGS).selected) == 2


# --------------------------------------------------------------------------- #
# per-subject cap — no one company takes over an edition
# --------------------------------------------------------------------------- #


SUBJECT_CAPPED = NewsletterSettings(
    max_items=8, min_score=70, collapse_events=False, max_per_subject=2
)


def make_about(subject: str, article_id: str, score: int) -> RankedArticle:
    return make_ranked(article_id, score, event=(subject, "announces", article_id, None))


def test_no_single_company_can_take_more_than_its_share() -> None:
    ranked = [make_about("OpenAI", f"thing{i}", 95 - i) for i in range(4)]
    result = select(ranked, SUBJECT_CAPPED)

    assert [r.article.article_id for r in result.selected] == ["thing0", "thing1"]
    assert result.reasons() == {REASON_SUBJECT_LIMIT: 2}
    # Nothing vanishes: every candidate is either selected or rejected with a reason.
    assert len(result.selected) + len(result.rejected) == len(ranked)


def test_a_capped_subject_leaves_room_for_another_company() -> None:
    ranked = [make_about("OpenAI", f"thing{i}", 95 - i) for i in range(3)]
    ranked.append(make_about("Google", "gemini", 71))

    selected = select(ranked, SUBJECT_CAPPED).selected

    assert [r.assessment.event_subject for r in selected] == ["OpenAI", "OpenAI", "Google"]


def test_an_article_whose_analyst_named_no_subject_is_never_capped() -> None:
    """Fail open: an unknown subject is not evidence of dominance."""
    ranked = [make_ranked(f"a{i}", 95 - i) for i in range(5)]
    assert len(select(ranked, SUBJECT_CAPPED).selected) == 5


def test_the_same_company_written_differently_still_counts_once() -> None:
    ranked = [
        make_about("OpenAI", "first", 95),
        make_about("openai", "second", 92),
        make_about("The OpenAI", "third", 90),
    ]
    result = select(ranked, SUBJECT_CAPPED)

    assert len(result.selected) == 2
    assert result.reasons() == {REASON_SUBJECT_LIMIT: 1}


def test_the_subject_cap_can_be_removed() -> None:
    settings = NewsletterSettings(
        max_items=8, min_score=70, collapse_events=False, max_per_subject=None
    )
    ranked = [make_about("OpenAI", f"thing{i}", 95 - i) for i in range(4)]
    assert len(select(ranked, settings).selected) == 4


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
    # Nothing vanishes: every candidate is either selected or rejected with a reason.
    assert len(result.selected) + len(result.rejected) == len(ranked)


def test_the_cap_keeps_the_best_of_each_source_and_leaves_room_for_others() -> None:
    """The point of the cap: room is left for other voices, at their own rank."""
    ranked = [
        make_from("loudest", "top", 95),
        make_from("loudest", "second", 90),
        make_from("loudest", "third", 85),
        make_from("quieter", "other", 80),
    ]
    result = select(ranked, CAPPED)

    assert [r.article.article_id for r in result.selected] == ["top", "second", "other"]


def test_no_cap_by_default() -> None:
    """Existing configurations keep working; the cap is opt-in."""
    ranked = [make_from("loudest", f"a{i}", 95 - i) for i in range(4)]
    settings = NewsletterSettings(max_items=8, min_score=70)

    assert settings.max_per_source is None
    assert len(select(ranked, settings).selected) == 4


# --------------------------------------------------------------------------- #
# the run manifest — the console is not an audit surface
# --------------------------------------------------------------------------- #


def test_a_suppressed_reprint_reaches_the_run_manifest() -> None:
    """Rule 7: a story that vanishes must be explainable from the artifact --
    and an omission is the system working, not a broken run."""
    manifest = RunManifest(run_id="r1", started_at=NOW)
    published = PublishedKeys(by_article_id={"reprint": "2026-W33"})

    select([make_ranked("reprint", 95)], SETTINGS, manifest=manifest, published=published)

    assert [(w.article_id, w.reason, w.detail) for w in manifest.withheld] == [
        ("reprint", REASON_ALREADY_PUBLISHED, "already published in 2026-W33")
    ]
    assert manifest.withheld[0].title == "Story reprint"
    assert manifest.withheld[0].url == "https://wire.example/reprint"
    assert not manifest.failed


def test_a_capped_subject_reaches_the_run_manifest_naming_the_subject() -> None:
    """The same detail an operator sees on the rejection reaches the artifact."""
    manifest = RunManifest(run_id="r1", started_at=NOW)
    ranked = [make_about("Northwind", f"n{i}", 95 - i) for i in range(3)]

    result = select(ranked, SUBJECT_CAPPED, manifest=manifest)

    assert [(w.article_id, w.reason) for w in manifest.withheld] == [("n2", REASON_SUBJECT_LIMIT)]
    assert manifest.withheld[0].detail == "2 stories already cover 'northwind'"
    assert result.rejections_for(REASON_SUBJECT_LIMIT)[0].detail == manifest.withheld[0].detail


def test_policy_arithmetic_stays_out_of_the_manifest() -> None:
    """A low score or a full section is re-derivable from the counts and config."""
    manifest = RunManifest(run_id="r1", started_at=NOW)
    ranked = [make_ranked("weak", 20), make_ranked("other", 99, category=TopicCategory.OTHER)]

    select(ranked, SETTINGS, manifest=manifest)

    assert manifest.withheld == []


# --------------------------------------------------------------------------- #
# the second collapse pass — one event, however the analyzer keyed it
# --------------------------------------------------------------------------- #


SIMILARITY = NewsletterSettings(max_items=8, min_score=70, collapse_events=False)


def make_reported(article_id: str, source_id: str, title: str, text: str, score: int):
    """One article of a multi-outlet story, with a body long enough to compare."""
    ranked = make_ranked(article_id, score)
    return ranked.model_copy(
        update={
            "article": ranked.article.model_copy(
                update={"source_id": source_id, "title": title, "clean_text": text}
            )
        }
    )


ONE_LAUNCH = (
    (
        "vendor-post",
        "vendor",
        "Introducing ChatGPT for Teens",
        "Introducing ChatGPT for Teens, built for learning and backed by parental "
        "controls. Teenagers aged 13 to 17 are moved into a separate ChatGPT "
        "experience with age prediction, parental controls and stricter safeguards "
        "around self-harm, disordered eating and romantic roleplay. A parent can "
        "link an account, set quiet hours and receive a notification when our "
        "systems detect a teenager in acute distress.",
        88,
    ),
    (
        "outlet-report",
        "outlet",
        "ChatGPT is getting a dedicated mode for teens",
        "ChatGPT is getting a dedicated mode for teens. The company said teenagers "
        "between 13 and 17 will be moved into a separate experience carrying "
        "parental controls, age prediction and tighter safeguards covering "
        "self-harm, disordered eating and romantic roleplay. Parents will be able "
        "to link an account, set quiet hours and get a notification if the company "
        "believes their teenager is in acute distress.",
        82,
    ),
    (
        "rival-report",
        "rival",
        "A safer ChatGPT for teens, years late",
        "A safer ChatGPT for teens arrives years after teenagers started using it "
        "anyway. Teen accounts for 13- to 17-year-olds bring parental controls, age "
        "prediction and new safeguards around self-harm, disordered eating and "
        "romantic roleplay. Parents get quiet hours and a distress notification. "
        "Critics point out that teenagers have been doing homework with the chatbot, "
        "largely unsupervised, since the day it launched.",
        80,
    ),
)


def test_three_reports_of_one_event_are_selected_as_one_story() -> None:
    """The owner's complaint: three of eight stories on one launch.

    A fold is a story the reader never sees, so the same event has to be
    explainable from the rejection *and* from the run manifest.
    """
    manifest = RunManifest(run_id="r1", started_at=NOW)
    result = select(
        [make_reported(*report) for report in ONE_LAUNCH], SIMILARITY, manifest=manifest
    )

    assert [r.article.article_id for r in result.selected] == ["vendor-post"]
    assert result.reasons() == {REASON_SIMILAR_EVENT: 2}
    assert all(
        item.detail == "same event as 'Introducing ChatGPT for Teens'"
        for item in result.rejections_for(REASON_SIMILAR_EVENT)
    )
    assert [w.article_id for w in manifest.withheld] == ["outlet-report", "rival-report"]
    assert all(w.reason == REASON_SIMILAR_EVENT for w in manifest.withheld)


def test_the_second_pass_can_be_switched_off() -> None:
    settings = SIMILARITY.model_copy(update={"collapse_similar_events": False})
    result = select([make_reported(*report) for report in ONE_LAUNCH], settings)

    assert len(result.selected) == 3
    assert result.reasons() == {}


def test_reports_below_the_floor_are_rejected_on_score_and_never_folded() -> None:
    """The same three reports, none publishable: the floor rejects them, not the fold.

    A candidate under ``min_score`` cannot reach the page whatever the similarity
    pass decides, and folding it would only put a wrong `similar_event` in the
    manifest. Every false positive measured on the real 2026-W34 edition sat here.
    """
    settings = SIMILARITY.model_copy(update={"min_score": 95})
    manifest = RunManifest(run_id="r1", started_at=NOW)
    result = select([make_reported(*report) for report in ONE_LAUNCH], settings, manifest=manifest)

    assert result.is_empty
    assert result.reasons() == {REASON_BELOW_THRESHOLD: 3}
    assert manifest.withheld == []


# --------------------------------------------------------------------------- #
# reserved slots — the reader asked for the link, so it does not have to win
# --------------------------------------------------------------------------- #

#: The synthetic source every reader submission is ingested through.
SUBMISSIONS = "reader-submissions"

#: Ten slots, no diversity caps: the caps have their own tests below.
BANK = NewsletterSettings(max_items=10, min_score=70)


def submitted(article_id: str, score: int, **overrides: Any) -> RankedArticle:
    """The same article, ingested through the reader-submission source."""
    ranked = make_ranked(article_id, score, **overrides)
    return ranked.model_copy(
        update={"article": ranked.article.model_copy(update={"source_id": SUBMISSIONS})}
    )


def reserving(ranked: list[RankedArticle], settings=BANK, **kwargs: Any):
    return select(ranked, settings, reserved_source_id=SUBMISSIONS, **kwargs)


def test_submissions_are_seated_first_and_the_rubric_fills_the_rest() -> None:
    """The owner's workflow: three submissions mean three reserved and seven earned.

    The submissions score below every story in the pool and are printed first
    anyway, which is the whole point: a reserved slot is not a competition.
    """
    manifest = RunManifest(run_id="r1", started_at=NOW)
    pool = [make_ranked(f"earned{index}", 95 - index) for index in range(12)]
    readers = [submitted(f"reader{index}", 71 + index) for index in range(3)]

    mixed = [*pool, *readers]
    result = reserving(mixed, manifest=manifest)

    assert [r.article.article_id for r in result.selected] == [
        "reader2",
        "reader1",
        "reader0",
        *[f"earned{index}" for index in range(7)],
    ]
    assert result.reserved == result.selected[:3]
    assert manifest.articles_reserved == 3
    assert manifest.articles_selected == 10
    # AC9: the mixed pool fed backwards produces the same ten in the same order.
    assert [r.article.article_id for r in reserving(list(reversed(mixed))).selected] == [
        r.article.article_id for r in result.selected
    ]


def test_a_reserved_slot_ignores_the_score_floor() -> None:
    """And the floor still describes what the *rubric* found: the count is honest."""
    result = reserving([submitted("reader", 3), make_ranked("earned", 90)])

    assert [r.article.article_id for r in result.selected] == ["reader", "earned"]
    assert result.above_threshold == 1


CAPPED_BANK = NewsletterSettings(
    max_items=10,
    min_score=70,
    max_per_source=2,
    max_per_subject=2,
    section_limits={TopicCategory.AI_MODELS: 2},
)


@pytest.mark.parametrize(
    "settings",
    [
        pytest.param(
            CAPPED_BANK.model_copy(update={"max_per_subject": None, "section_limits": {}}),
            id="max_per_source",
        ),
        pytest.param(
            CAPPED_BANK.model_copy(update={"max_per_source": None, "section_limits": {}}),
            id="max_per_subject",
        ),
        pytest.param(
            CAPPED_BANK.model_copy(update={"max_per_source": None, "max_per_subject": None}),
            id="section_limits",
        ),
    ],
)
def test_a_reserved_slot_bypasses_every_cap_that_rations_slots(
    settings: NewsletterSettings,
) -> None:
    """Submissions arrive as one source, one subject and one category at a time.

    Each cap alone would hold the edition to two submitted links, which would make
    the guarantee impossible to keep. Each parametrisation leaves exactly one cap
    armed, so a regression names the cap that came back.
    """
    readers = [
        submitted(f"reader{index}", 80 - index, event=("Northwind", "announces", f"n{index}", None))
        for index in range(4)
    ]

    result = reserving(readers, settings)

    assert [r.article.article_id for r in result.selected] == [f"reader{i}" for i in range(4)]


def test_a_reserved_story_still_counts_against_the_caps_for_everything_else() -> None:
    """Bypassing a cap is not the same as emptying it: the earned slots stay diverse."""
    readers = [
        submitted(f"reader{index}", 40, event=("Northwind", "announces", f"n{index}", None))
        for index in range(2)
    ]
    pool = [
        make_ranked(f"earned{index}", 90, event=("Northwind", "reports", f"e{index}", None))
        for index in range(2)
    ]

    settings = CAPPED_BANK.model_copy(update={"max_per_source": None, "section_limits": {}})
    result = reserving([*pool, *readers], settings)

    assert [r.article.article_id for r in result.selected] == ["reader0", "reader1"]
    assert result.reasons() == {REASON_SUBJECT_LIMIT: 2}


def test_more_submissions_than_slots_are_seated_in_ranking_order() -> None:
    """``max_items`` is never exceeded, and the order that decides is score-first.

    The two that miss out are recorded under the reason that actually applies to
    them; the run manifest is where they are marked as submissions.
    """
    readers = [submitted(f"reader{index:02d}", 60 + index) for index in range(12)]

    result = reserving(readers)
    reversed_result = reserving(list(reversed(readers)))

    expected = [f"reader{index:02d}" for index in range(11, 1, -1)]
    assert [r.article.article_id for r in result.selected] == expected
    assert [r.article.article_id for r in reversed_result.selected] == expected
    assert result.reasons() == {REASON_BELOW_THRESHOLD: 2}


def test_reserved_slots_break_ties_by_publication_then_id_like_everything_else() -> None:
    readers = [
        submitted("zzz", 40, published_at=datetime(2026, 8, 15, tzinfo=UTC)),
        submitted("aaa", 40, published_at=datetime(2026, 8, 16, tzinfo=UTC)),
        submitted("mmm", 40, published_at=datetime(2026, 8, 15, tzinfo=UTC)),
    ]
    result = reserving(readers, NewsletterSettings(max_items=2, min_score=70))

    assert [r.article.article_id for r in result.selected] == ["mmm", "zzz"]


@pytest.mark.parametrize(
    ("slots", "expected"),
    [
        (0, ["earned"]),
        (1, ["reader1", "earned"]),
        (None, ["reader1", "reader0", "earned"]),
    ],
)
def test_reserved_slots_bound_how_much_of_the_edition_is_given_away(
    slots: int | None, expected: list[str]
) -> None:
    pool = [make_ranked("earned", 90), submitted("reader0", 40), submitted("reader1", 41)]

    result = reserving(pool, reserved_slots=slots)

    assert [r.article.article_id for r in result.selected] == expected


def test_switching_reservation_off_reproduces_the_line_up_exactly() -> None:
    """``reserved_slots: 0`` is the old behaviour, down to the manifest."""
    pool = [
        make_ranked("earned", 90),
        submitted("strong", 88),
        submitted("weak", 40),
        make_ranked("capped", 85, event=("Northwind", "announces", "one", None)),
        make_ranked("capped2", 84, event=("Northwind", "announces", "two", None)),
        make_ranked("capped3", 83, event=("Northwind", "announces", "three", None)),
    ]
    settings = NewsletterSettings(max_items=10, min_score=70, max_per_subject=2)

    off_manifest = RunManifest(run_id="off", started_at=NOW)
    never_manifest = RunManifest(run_id="never", started_at=NOW)
    off = reserving(pool, settings, reserved_slots=0, manifest=off_manifest)
    never = select(pool, settings, manifest=never_manifest)

    assert [r.article.article_id for r in off.selected] == [
        r.article.article_id for r in never.selected
    ]
    assert off.reserved == []
    assert [(r.ranked.article.article_id, r.reason, r.detail) for r in off.rejected] == [
        (r.ranked.article.article_id, r.reason, r.detail) for r in never.rejected
    ]
    assert off_manifest.withheld == never_manifest.withheld
    assert off_manifest.articles_reserved == 0


def test_a_submission_in_an_excluded_category_is_never_reserved() -> None:
    """A slot is reserved for a story, not for a category the edition never prints."""
    result = reserving([submitted("reader", 99, category=TopicCategory.OTHER)])

    assert result.is_empty
    assert result.reasons() == {REASON_EXCLUDED_CATEGORY: 1}


def test_a_submission_an_earlier_edition_printed_is_still_suppressed() -> None:
    """ "Printed once" is a promise to the reader, not a cap on the submitter --
    and the manifest says the slot went empty because the story had already run."""
    manifest = RunManifest(run_id="r1", started_at=NOW)
    published = PublishedKeys(by_article_id={"reader": "2026-W33"})

    result = reserving([submitted("reader", 95)], manifest=manifest, published=published)

    assert result.is_empty
    assert [(w.article_id, w.reason, w.detail) for w in manifest.withheld] == [
        ("reader", REASON_ALREADY_PUBLISHED, "reader submission: already published in 2026-W33")
    ]


def test_a_submission_of_a_story_already_in_the_edition_prints_once() -> None:
    """The reader's copy is the one that survives: it is the copy holding the slot.

    Keeping the outlet's copy instead would put the story back into competition,
    where the score it happens to carry could lose it -- so the guarantee would
    quietly depend on which page a reader linked to.
    """
    event = ("Northwind", "launches", "a product", None)
    result = reserving(
        [make_ranked("outlet", 95, event=event), submitted("reader", 30, event=event)]
    )

    assert [r.article.article_id for r in result.selected] == ["reader"]
    assert result.reasons() == {REASON_DUPLICATE_EVENT: 1}


def test_a_submission_below_the_floor_still_folds_the_reports_it_duplicates() -> None:
    """The similarity pass ignores the floor for a reserved submission, and must.

    A sub-threshold submission is going to print, so leaving it out of the pass --
    which is right for every other sub-threshold candidate -- would let a reader's
    link and an outlet's account of the same launch run side by side.
    """
    vendor, outlet, rival = ONE_LAUNCH
    reader = make_reported(vendor[0], SUBMISSIONS, vendor[2], vendor[3], 30)
    others = [make_reported(*report) for report in (outlet, rival)]

    result = reserving([reader, *others], SIMILARITY)

    assert [r.article.article_id for r in result.selected] == ["vendor-post"]
    assert result.reasons() == {REASON_SIMILAR_EVENT: 2}


def test_a_reserved_story_leads_only_when_it_out_scores_the_field() -> None:
    """Seated first is not the same as best. The lead is still the best story."""
    weak = reserving([submitted("reader", 40), make_ranked("earned", 90)])
    strong = reserving([submitted("reader", 99), make_ranked("earned", 90)])

    assert weak.selected[0].article.article_id == "reader"
    assert weak.lead.article.article_id == "earned"
    assert strong.lead.article.article_id == "reader"
