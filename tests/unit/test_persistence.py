"""SQLite persistence: round-trips, cache identity and traceability."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from newsletter.models import (
    ArticleAssessment,
    AssessmentRecord,
    NewsletterEdition,
    NewsletterItem,
    NewsletterSection,
    NormalizedArticle,
    PipelineStage,
    RunManifest,
    SourceConfig,
    TopicCategory,
)
from newsletter.persistence.sqlite import SCHEMA_VERSION, Database, PersistenceError

NOW = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)
PUBLISHED = datetime(2026, 8, 17, 10, 0, tzinfo=UTC)


@pytest.fixture
def db() -> Database:
    with Database(":memory:") as database:
        yield database


def make_source(source_id: str = "wire") -> SourceConfig:
    return SourceConfig(
        id=source_id,
        name="Wire Example",
        entrypoint="https://wire.example/feed",
        strategy="rss",
        priority=9,
        category_hint=TopicCategory.AI_MODELS,
    )


def make_article(article_id: str = "a1", **overrides: object) -> NormalizedArticle:
    values: dict[str, object] = {
        "article_id": article_id,
        "source_id": "wire",
        "canonical_url": f"https://wire.example/{article_id}",
        "title": "Example Labs ships a reasoning model",
        "published_at": PUBLISHED,
        "author": "Jane Doe",
        "clean_text": "A long enough body of article text for persistence testing.",
        "content_hash": f"contenthash-{article_id}",
        "retrieved_at": NOW,
    }
    values.update(overrides)
    return NormalizedArticle(**values)  # type: ignore[arg-type]


def make_assessment() -> ArticleAssessment:
    return ArticleAssessment(
        category=TopicCategory.AI_MODELS,
        topic_relevance=5,
        business_impact=4,
        novelty=5,
        actionability=3,
        confidence=0.91,
        summary="A model was released.",
        why_it_matters="It changes cost per token.",
        key_facts=["Cheaper", "Available today"],
    )


def make_record(**overrides: object) -> AssessmentRecord:
    values: dict[str, object] = {
        "assessment": make_assessment(),
        "content_hash": "contenthash-a1",
        "model": "gpt-4.1-mini",
        "prompt_version": "v1",
        "schema_version": "1",
        "created_at": NOW,
    }
    values.update(overrides)
    return AssessmentRecord(**values)  # type: ignore[arg-type]


def make_item(article_id: str = "a1", category: TopicCategory = TopicCategory.AI_MODELS):
    return NewsletterItem(
        article_id=article_id,
        headline="Example Labs ships a reasoning model",
        category=category,
        source_name="Wire Example",
        source_url=f"https://wire.example/{article_id}",
        published_at=PUBLISHED,
        summary="A model was released.",
        why_it_matters="It changes cost per token.",
        score=88,
    )


def make_edition() -> NewsletterEdition:
    return NewsletterEdition(
        edition_id="2026-W34",
        masthead="AI & Digital Intelligence Weekly",
        issue_label="2026-W34",
        period_start=NOW - timedelta(days=7),
        period_end=NOW,
        executive_summary=["One important thing happened."],
        lead_story=make_item("lead"),
        sections=[
            NewsletterSection(
                category=TopicCategory.AI_MODELS,
                title="AI Models & APIs",
                items=[make_item("lead"), make_item("second")],
            ),
            NewsletterSection(
                category=TopicCategory.AI_VIDEO,
                title="AI Video & Creative AI",
                items=[make_item("third", TopicCategory.AI_VIDEO)],
            ),
        ],
        generated_at=NOW,
    )


# --------------------------------------------------------------------------- #
# lifecycle
# --------------------------------------------------------------------------- #


def test_schema_is_created_and_versioned(db: Database) -> None:
    assert db.schema_version == SCHEMA_VERSION


def test_initialize_is_idempotent(db: Database) -> None:
    db.initialize()
    db.initialize()
    assert db.schema_version == SCHEMA_VERSION


def test_using_a_closed_database_fails_loudly() -> None:
    database = Database(":memory:")
    with pytest.raises(PersistenceError, match="not connected"):
        database.count_articles()


def test_a_file_database_persists_across_connections(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "newsletter.sqlite"
    with Database(path) as first:
        first.save_article(make_article())
    with Database(path) as second:
        assert second.get_article("a1") is not None
    assert path.exists()


# --------------------------------------------------------------------------- #
# round-trips
# --------------------------------------------------------------------------- #


def test_source_round_trips(db: Database) -> None:
    source = make_source()
    db.upsert_source(source, now=NOW)
    assert db.get_source("wire") == source


def test_source_upsert_updates_in_place(db: Database) -> None:
    db.upsert_source(make_source(), now=NOW)
    db.upsert_source(make_source().model_copy(update={"priority": 3}), now=NOW)
    assert db.get_source("wire").priority == 3
    assert len(db.list_sources()) == 1


def test_article_round_trips_exactly(db: Database) -> None:
    article = make_article()
    db.save_article(article, now=NOW)
    restored = db.get_article("a1")
    assert restored == article
    assert restored.published_at.tzinfo is not None


def test_saving_the_same_article_twice_reports_it_as_not_new(db: Database) -> None:
    assert db.save_article(make_article(), now=NOW) is True
    assert db.save_article(make_article(), now=NOW) is False
    assert db.count_articles() == 1


def test_save_articles_counts_only_new_ones(db: Database) -> None:
    db.save_article(make_article("a1"), now=NOW)
    new_count = db.save_articles([make_article("a1"), make_article("a2"), make_article("a3")])
    assert new_count == 2
    assert db.count_articles() == 3


def test_articles_can_be_found_by_content_hash(db: Database) -> None:
    db.save_article(make_article("a1"), now=NOW)
    assert db.find_article_by_hash("contenthash-a1").article_id == "a1"
    assert db.find_article_by_hash("nope") is None


def test_missing_records_return_none(db: Database) -> None:
    assert db.get_article("missing") is None
    assert db.get_source("missing") is None
    assert db.get_edition("missing") is None
    assert db.get_run("missing") is None


# --------------------------------------------------------------------------- #
# assessment cache — identity is content + prompt + schema + model
# --------------------------------------------------------------------------- #


def test_assessment_round_trips(db: Database) -> None:
    record = make_record()
    db.save_assessment(record, article_id="a1")
    restored = db.get_assessment(record.key)
    assert restored == record
    assert restored.assessment.category is TopicCategory.AI_MODELS


def test_cache_hit_requires_every_identity_component(db: Database) -> None:
    db.save_assessment(make_record(), article_id="a1")

    assert db.get_assessment(
        AssessmentRecord.cache_key("contenthash-a1", "v1", "1", "gpt-4.1-mini")
    )
    # A change in any component is a miss, so stale judgments are never reused.
    assert (
        db.get_assessment(AssessmentRecord.cache_key("other-hash", "v1", "1", "gpt-4.1-mini"))
        is None
    )
    assert (
        db.get_assessment(AssessmentRecord.cache_key("contenthash-a1", "v2", "1", "gpt-4.1-mini"))
        is None
    )
    assert (
        db.get_assessment(AssessmentRecord.cache_key("contenthash-a1", "v1", "2", "gpt-4.1-mini"))
        is None
    )
    assert (
        db.get_assessment(AssessmentRecord.cache_key("contenthash-a1", "v1", "1", "gpt-5")) is None
    )


def test_a_new_prompt_version_stores_a_second_entry(db: Database) -> None:
    db.save_assessment(make_record(prompt_version="v1"), article_id="a1")
    db.save_assessment(make_record(prompt_version="v2"), article_id="a1")
    assert db.get_assessment(make_record(prompt_version="v1").key) is not None
    assert db.get_assessment(make_record(prompt_version="v2").key) is not None


# --------------------------------------------------------------------------- #
# editions and traceability (AC3)
# --------------------------------------------------------------------------- #


def test_edition_round_trips(db: Database) -> None:
    edition = make_edition()
    db.save_edition(edition, output_paths={"html": "output/2026-W34/newsletter.html"})
    assert db.get_edition("2026-W34") == edition


def test_edition_items_preserve_the_story_list(db: Database) -> None:
    db.save_edition(make_edition())
    assert db.get_edition_article_ids("2026-W34") == ["lead", "second", "third"]


def test_resaving_an_edition_does_not_duplicate_its_items(db: Database) -> None:
    db.save_edition(make_edition())
    db.save_edition(make_edition())
    assert db.get_edition_article_ids("2026-W34") == ["lead", "second", "third"]


def test_every_published_story_can_be_traced_to_its_source(db: Database) -> None:
    """AC3: edition -> item -> article -> source, by join."""
    db.upsert_source(make_source(), now=NOW)
    for article_id in ("lead", "second", "third"):
        db.save_article(make_article(article_id), now=NOW)
    db.save_edition(make_edition())

    for article_id in db.get_edition_article_ids("2026-W34"):
        article = db.get_article(article_id)
        assert article is not None
        assert db.get_source(article.source_id) is not None
        assert article.canonical_url.startswith("https://")


# --------------------------------------------------------------------------- #
# run history
# --------------------------------------------------------------------------- #


def test_run_manifest_round_trips_with_its_errors(db: Database) -> None:
    manifest = RunManifest(run_id="run-1", started_at=NOW, articles_discovered=12)
    manifest.record_error(
        PipelineStage.FETCH, TimeoutError("slow source"), source_id="wire", now=NOW
    )
    manifest.finished_at = NOW + timedelta(minutes=2)

    db.save_run(manifest)
    restored = db.get_run("run-1")

    assert restored == manifest
    assert restored.errors[0].exception_class == "TimeoutError"
    assert restored.articles_discovered == 12


def test_saving_a_run_twice_updates_it(db: Database) -> None:
    manifest = RunManifest(run_id="run-1", started_at=NOW)
    db.save_run(manifest)
    manifest.newsletter_generated = True
    manifest.finished_at = NOW + timedelta(minutes=1)
    db.save_run(manifest)

    assert db.get_run("run-1").newsletter_generated is True
    assert len(db.recent_runs()) == 1


def test_recent_runs_are_newest_first(db: Database) -> None:
    for index in range(3):
        db.save_run(RunManifest(run_id=f"run-{index}", started_at=NOW + timedelta(hours=index)))
    assert [run.run_id for run in db.recent_runs(limit=2)] == ["run-2", "run-1"]
