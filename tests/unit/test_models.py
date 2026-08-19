"""Unit tests for the domain models.

These guard invariants the published edition depends on: closed taxonomies,
publishable URL schemes, timezone-aware timestamps and deterministic windows.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from newsletter.models import (
    PUBLISHABLE_CATEGORIES,
    ArticleAssessment,
    AssessmentRecord,
    DateWindow,
    FetchStrategy,
    NewsletterEdition,
    NewsletterItem,
    NewsletterSection,
    PipelineStage,
    RunManifest,
    SourceConfig,
    TopicCategory,
    WindowMode,
    validate_public_url,
)

NOW = datetime(2026, 8, 18, 12, 30, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# taxonomy
# --------------------------------------------------------------------------- #


def test_topic_taxonomy_is_closed() -> None:
    assert [c.value for c in TopicCategory] == [
        "youtube_platform",
        "youtube_monetization",
        "kids_content",
        "ai_video",
        "ai_models",
        "ai_business",
        "other",
    ]


def test_other_is_not_publishable() -> None:
    assert TopicCategory.OTHER not in PUBLISHABLE_CATEGORIES
    assert len(PUBLISHABLE_CATEGORIES) == len(TopicCategory) - 1


def test_fetch_strategies_are_closed() -> None:
    assert {s.value for s in FetchStrategy} == {
        "rss",
        "scrapling_static",
        "scrapling_dynamic",
        "scrapling_stealth",
    }


# --------------------------------------------------------------------------- #
# URL validation — the gate that keeps unsafe links out of publication
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/article",
        "http://example.com",
        "https://example.com/a?b=1#c",
    ],
)
def test_public_url_accepts_http_and_https(url: str) -> None:
    assert validate_public_url(url) == url


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "data:text/html,<script>x</script>",
        "file:///etc/passwd",
        "ftp://example.com/x",
        "/relative/path",
        "example.com/no-scheme",
        "",
        "   ",
        "https://",
    ],
)
def test_public_url_rejects_everything_else(url: str) -> None:
    with pytest.raises(ValueError):
        validate_public_url(url)


def test_public_url_strips_surrounding_whitespace() -> None:
    assert validate_public_url("  https://example.com/a  ") == "https://example.com/a"


# --------------------------------------------------------------------------- #
# SourceConfig
# --------------------------------------------------------------------------- #


def _source(**overrides: object) -> SourceConfig:
    values: dict[str, object] = {
        "id": "example-source",
        "name": "Example Source",
        "entrypoint": "https://example.com/feed",
        "strategy": "rss",
        "priority": 7,
    }
    values.update(overrides)
    return SourceConfig(**values)  # type: ignore[arg-type]


def test_source_config_defaults() -> None:
    source = _source()
    assert source.enabled is True
    assert source.category_hint is TopicCategory.OTHER
    assert source.selectors == {}
    assert source.strategy is FetchStrategy.RSS


@pytest.mark.parametrize("bad_id", ["Bad_ID", "-leading", "with space", "UPPER", ""])
def test_source_config_rejects_malformed_ids(bad_id: str) -> None:
    with pytest.raises(ValidationError):
        _source(id=bad_id)


@pytest.mark.parametrize("priority", [-1, 11])
def test_source_config_bounds_priority(priority: int) -> None:
    with pytest.raises(ValidationError):
        _source(priority=priority)


def test_source_config_rejects_unknown_fields_and_strategies() -> None:
    with pytest.raises(ValidationError):
        _source(typo_field="x")
    with pytest.raises(ValidationError):
        _source(strategy="curl")


def test_source_config_rejects_non_http_entrypoint() -> None:
    with pytest.raises(ValidationError):
        _source(entrypoint="javascript:alert(1)")


def test_value_models_are_frozen() -> None:
    source = _source()
    with pytest.raises(ValidationError):
        source.priority = 3  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# DateWindow
# --------------------------------------------------------------------------- #


def test_window_rejects_inverted_and_empty_ranges() -> None:
    with pytest.raises(ValidationError):
        DateWindow(start=NOW, end=NOW - timedelta(days=1))
    with pytest.raises(ValidationError):
        DateWindow(start=NOW, end=NOW)


def test_window_rejects_naive_bounds() -> None:
    with pytest.raises(ValidationError):
        DateWindow(start=datetime(2026, 8, 11), end=datetime(2026, 8, 18))


def test_window_is_half_open() -> None:
    window = DateWindow(start=NOW - timedelta(days=7), end=NOW)
    assert window.contains(window.start) is True
    assert window.contains(window.end) is False
    assert window.contains(window.end - timedelta(microseconds=1)) is True
    assert window.contains(window.start - timedelta(microseconds=1)) is False


def test_window_contains_rejects_naive_datetime() -> None:
    window = DateWindow(start=NOW - timedelta(days=7), end=NOW)
    with pytest.raises(ValueError, match="naive"):
        window.contains(datetime(2026, 8, 15))


def test_window_compares_across_timezones() -> None:
    window = DateWindow(start=NOW - timedelta(days=7), end=NOW)
    inside = datetime(2026, 8, 18, 8, 0, tzinfo=timezone(timedelta(hours=-4)))
    assert window.contains(inside) is True


def test_last_days_rolling_ends_at_execution_time() -> None:
    window = DateWindow.last_days(7, now=NOW, mode=WindowMode.ROLLING)
    assert window.end == NOW
    assert window.start == NOW - timedelta(days=7)
    assert window.days == pytest.approx(7.0)


def test_last_days_completed_days_uses_whole_local_days() -> None:
    window = DateWindow.last_days(7, now=NOW, mode=WindowMode.COMPLETED_DAYS)
    assert (window.end.hour, window.end.minute, window.end.second) == (0, 0, 0)
    assert window.contains(NOW) is False  # today is not a completed day
    assert window.days == pytest.approx(7.0)


def test_last_days_is_deterministic_for_a_fixed_clock() -> None:
    a = DateWindow.last_days(7, now=NOW, tz_name="Europe/Madrid")
    b = DateWindow.last_days(7, now=NOW, tz_name="Europe/Madrid")
    assert a == b


def test_last_days_validates_inputs() -> None:
    with pytest.raises(ValueError, match="at least one day"):
        DateWindow.last_days(0, now=NOW)
    with pytest.raises(ValueError, match="timezone-aware"):
        DateWindow.last_days(7, now=datetime(2026, 8, 18))


def test_from_dates_end_is_inclusive_of_the_last_day() -> None:
    window = DateWindow.from_dates("2026-08-11", "2026-08-17")
    assert window.start == datetime(2026, 8, 11, tzinfo=UTC)
    assert window.end == datetime(2026, 8, 18, tzinfo=UTC)
    assert window.contains(datetime(2026, 8, 17, 23, 59, tzinfo=UTC)) is True
    assert window.contains(datetime(2026, 8, 18, 0, 0, tzinfo=UTC)) is False


def test_from_dates_rejects_bad_format() -> None:
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        DateWindow.from_dates("11/08/2026", "17/08/2026")


def test_issue_label_uses_iso_week_of_last_covered_day() -> None:
    assert DateWindow.from_dates("2026-08-11", "2026-08-17").issue_label() == "2026-W34"


# --------------------------------------------------------------------------- #
# ArticleAssessment
# --------------------------------------------------------------------------- #


def _assessment(**overrides: object) -> ArticleAssessment:
    values: dict[str, object] = {
        "category": TopicCategory.AI_MODELS,
        "topic_relevance": 5,
        "business_impact": 4,
        "novelty": 5,
        "actionability": 3,
        "confidence": 0.91,
        "summary": "A model was released.",
        "why_it_matters": "It changes cost per token.",
    }
    values.update(overrides)
    return ArticleAssessment(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "field", ["topic_relevance", "business_impact", "novelty", "actionability"]
)
@pytest.mark.parametrize("value", [-1, 6, 2.5])
def test_ratings_are_bounded_integers(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        _assessment(**{field: value})


@pytest.mark.parametrize("confidence", [-0.01, 1.01])
def test_confidence_is_bounded(confidence: float) -> None:
    with pytest.raises(ValidationError):
        _assessment(confidence=confidence)


def test_assessment_rejects_invented_category_and_extra_fields() -> None:
    with pytest.raises(ValidationError):
        _assessment(category="quantum_computing")
    with pytest.raises(ValidationError):
        _assessment(final_score=88)


def test_assessment_carries_no_score() -> None:
    """The model must never emit the final score."""
    assert "final_score" not in ArticleAssessment.model_fields
    assert "score" not in ArticleAssessment.model_fields


def test_event_fingerprint_requires_a_complete_event() -> None:
    partial = _assessment(event_subject="OpenAI", event_action="released")
    assert partial.event_fingerprint() is None

    complete = _assessment(
        event_subject="OpenAI",
        event_action="Released",
        event_object="GPT-5",
        event_date="2026-08-17",
    )
    assert complete.event_fingerprint() == "openai|released|gpt-5|2026-08-17"


def test_cache_key_changes_with_prompt_version() -> None:
    record = AssessmentRecord(
        assessment=_assessment(),
        content_hash="abc12345",
        model="gpt-4.1-mini",
        prompt_version="v1",
        schema_version="1",
        created_at=NOW,
    )
    assert record.key == "abc12345:v1:1:gpt-4.1-mini"
    assert record.key != AssessmentRecord.cache_key("abc12345", "v2", "1", "gpt-4.1-mini")


# --------------------------------------------------------------------------- #
# publication models
# --------------------------------------------------------------------------- #


def _item(article_id: str = "a1", **overrides: object) -> NewsletterItem:
    values: dict[str, object] = {
        "article_id": article_id,
        "headline": "Headline",
        "category": TopicCategory.AI_MODELS,
        "source_name": "Example",
        "source_url": "https://example.com/a",
        "published_at": NOW,
        "summary": "Summary.",
        "why_it_matters": "Matters.",
        "score": 88,
    }
    values.update(overrides)
    return NewsletterItem(**values)  # type: ignore[arg-type]


def test_newsletter_item_requires_a_publishable_url() -> None:
    with pytest.raises(ValidationError):
        _item(source_url="javascript:alert(1)")
    with pytest.raises(ValidationError):
        _item(source_url="not-a-url")


@pytest.mark.parametrize("score", [-1, 101])
def test_newsletter_item_score_is_bounded(score: int) -> None:
    with pytest.raises(ValidationError):
        _item(score=score)


def test_section_requires_at_least_one_item() -> None:
    with pytest.raises(ValidationError):
        NewsletterSection(category=TopicCategory.AI_MODELS, title="AI", items=[])


def test_edition_all_items_puts_lead_first_and_deduplicates() -> None:
    lead = _item("lead")
    edition = NewsletterEdition(
        edition_id="2026-W34",
        masthead="Weekly",
        issue_label="2026-W34",
        period_start=NOW - timedelta(days=7),
        period_end=NOW,
        executive_summary=["One thing happened."],
        lead_story=lead,
        sections=[
            NewsletterSection(
                category=TopicCategory.AI_MODELS,
                title="AI Models & APIs",
                items=[lead, _item("second")],
            )
        ],
        generated_at=NOW,
    )
    assert [i.article_id for i in edition.all_items()] == ["lead", "second"]


def test_edition_requires_an_executive_summary() -> None:
    with pytest.raises(ValidationError):
        NewsletterEdition(
            edition_id="2026-W34",
            masthead="Weekly",
            issue_label="2026-W34",
            period_start=NOW - timedelta(days=7),
            period_end=NOW,
            executive_summary=[],
            lead_story=_item(),
            sections=[],
            generated_at=NOW,
        )


# --------------------------------------------------------------------------- #
# observability
# --------------------------------------------------------------------------- #


def test_manifest_records_errors_without_losing_them() -> None:
    manifest = RunManifest(run_id="r1", started_at=NOW)
    assert manifest.failed is False

    error = manifest.record_error(
        PipelineStage.FETCH,
        TimeoutError("source timed out"),
        source_id="openai-news",
        retry_count=2,
        now=NOW,
    )

    assert error.exception_class == "TimeoutError"
    assert error.source_id == "openai-news"
    assert error.retry_count == 2
    assert error.stage is PipelineStage.FETCH
    assert manifest.errors == [error]
    assert manifest.failed is True


def test_manifest_truncates_huge_error_messages() -> None:
    manifest = RunManifest(run_id="r1", started_at=NOW)
    manifest.record_error(PipelineStage.FETCH, ValueError("x" * 5000), now=NOW)
    assert len(manifest.errors[0].message) == 1000


def test_manifest_is_serializable() -> None:
    manifest = RunManifest(run_id="r1", started_at=NOW)
    manifest.record_error(PipelineStage.RENDER, OSError("disk full"), now=NOW)
    payload = manifest.model_dump(mode="json")
    assert payload["run_id"] == "r1"
    assert payload["errors"][0]["stage"] == "render"
