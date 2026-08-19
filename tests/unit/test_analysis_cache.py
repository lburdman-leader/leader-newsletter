"""Assessment caching: identity, reuse and invalidation.

Uses the real SQLite database rather than a stub, because the cache contract is
exactly what `Database` implements. Still fully offline: the model is mocked.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from newsletter.intelligence.analyzer import ANALYZER_PROMPT_VERSION, ArticleAnalyzer
from newsletter.intelligence.schemas import ASSESSMENT_SCHEMA_VERSION
from newsletter.models import AssessmentRecord, NormalizedArticle, RunManifest, SourceConfig
from newsletter.persistence.sqlite import Database
from tests.unit.test_analyzer import FakeOpenAI, make_client, ok_response

NOW = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)
PUBLISHED = datetime(2026, 8, 17, 10, 0, tzinfo=UTC)


@pytest.fixture
def db() -> Database:
    with Database(":memory:") as database:
        yield database


def make_article(article_id: str = "a1", content_hash: str = "contenthash-a1") -> NormalizedArticle:
    return NormalizedArticle(
        article_id=article_id,
        source_id="wire",
        canonical_url=f"https://wire.example/{article_id}",
        title="Example Labs ships a reasoning model",
        published_at=PUBLISHED,
        clean_text="The company announced a model with a larger context window.",
        content_hash=content_hash,
        retrieved_at=NOW,
    )


def make_source() -> SourceConfig:
    return SourceConfig(
        id="wire",
        name="Wire Example",
        entrypoint="https://wire.example/feed",
        strategy="rss",
        priority=9,
    )


CLIENT_KWARGS = {"max_attempts", "model", "backoff_seconds", "timeout", "max_output_tokens"}


def analyzer_with(db: Database, *results: Any, **kwargs: Any) -> tuple[ArticleAnalyzer, FakeOpenAI]:
    """Build an analyzer backed by a mocked SDK and the real database as its cache."""
    client_kwargs = {k: v for k, v in kwargs.items() if k in CLIENT_KWARGS}
    analyzer_kwargs = {k: v for k, v in kwargs.items() if k not in CLIENT_KWARGS}
    client, fake, _ = make_client(*(results or (ok_response(),)), **client_kwargs)
    return ArticleAnalyzer(client, cache=db, **analyzer_kwargs), fake


# --------------------------------------------------------------------------- #
# cache identity
# --------------------------------------------------------------------------- #


def test_cache_key_combines_content_prompt_schema_and_model(db: Database) -> None:
    analyzer, _ = analyzer_with(db)
    key = analyzer.cache_key_for(make_article())
    assert key == AssessmentRecord.cache_key(
        "contenthash-a1", ANALYZER_PROMPT_VERSION, ASSESSMENT_SCHEMA_VERSION, "gpt-4.1-mini"
    )


# --------------------------------------------------------------------------- #
# reuse
# --------------------------------------------------------------------------- #


def test_a_second_analysis_of_the_same_article_hits_the_cache(db: Database) -> None:
    analyzer, fake = analyzer_with(db)
    article, source = make_article(), make_source()
    manifest = RunManifest(run_id="r1", started_at=NOW)

    first = analyzer.analyze(article, source, manifest=manifest, now=NOW)
    second = analyzer.analyze(article, source, manifest=manifest, now=NOW)

    assert second == first
    assert len(fake.responses.calls) == 1  # the model was asked exactly once
    assert manifest.llm_calls == 1
    assert manifest.llm_cache_hits == 1


def test_a_fresh_analyzer_reuses_a_persisted_assessment(db: Database) -> None:
    """Cache survives the process: re-running a week costs nothing."""
    first_analyzer, _ = analyzer_with(db)
    first_analyzer.analyze(make_article(), make_source(), now=NOW)

    second_analyzer, second_fake = analyzer_with(db)
    record = second_analyzer.analyze(make_article(), make_source(), now=NOW)

    assert record.assessment.summary == "Example Labs released a model."
    assert second_fake.responses.calls == []


def test_different_articles_do_not_share_a_cache_entry(db: Database) -> None:
    analyzer, fake = analyzer_with(db)
    manifest = RunManifest(run_id="r1", started_at=NOW)

    analyzer.analyze(
        make_article("a1", "contenthash-a1"), make_source(), manifest=manifest, now=NOW
    )
    analyzer.analyze(
        make_article("a2", "contenthash-a2"), make_source(), manifest=manifest, now=NOW
    )

    assert len(fake.responses.calls) == 2
    assert manifest.llm_cache_hits == 0


def test_edited_content_invalidates_the_cache(db: Database) -> None:
    analyzer, fake = analyzer_with(db)
    analyzer.analyze(make_article("a1", "contenthash-original"), make_source(), now=NOW)
    analyzer.analyze(make_article("a1", "contenthash-rewritten"), make_source(), now=NOW)
    assert len(fake.responses.calls) == 2


# --------------------------------------------------------------------------- #
# invalidation — a changed judgment process must not reuse old judgments
# --------------------------------------------------------------------------- #


def test_a_new_prompt_version_invalidates_cached_assessments(db: Database) -> None:
    old_analyzer, _ = analyzer_with(db)
    old_analyzer.analyze(make_article(), make_source(), now=NOW)

    # Same article, same model, new prompt: the judgment must be redone.
    new_client, new_fake, _ = make_client(ok_response(topic_relevance=2))
    new_analyzer = ArticleAnalyzer(new_client, cache=db, prompt_version="v1", schema_version="2")
    record = new_analyzer.analyze(make_article(), make_source(), now=NOW)

    assert len(new_fake.responses.calls) == 1
    assert record.assessment.topic_relevance == 2
    assert record.schema_version == "2"


def test_a_new_model_invalidates_cached_assessments(db: Database) -> None:
    first, _ = analyzer_with(db)
    first.analyze(make_article(), make_source(), now=NOW)

    other_client, other_fake, _ = make_client(ok_response(), model="gpt-5")
    other_analyzer = ArticleAnalyzer(other_client, cache=db)
    record = other_analyzer.analyze(make_article(), make_source(), now=NOW)

    assert len(other_fake.responses.calls) == 1
    assert record.model == "gpt-5"


def test_both_generations_of_an_assessment_are_retained(db: Database) -> None:
    """Auditability: an old judgment stays inspectable after a prompt change."""
    old_analyzer, _ = analyzer_with(db)
    old_record = old_analyzer.analyze(make_article(), make_source(), now=NOW)

    new_client, _, _ = make_client(ok_response(topic_relevance=1))
    new_analyzer = ArticleAnalyzer(new_client, cache=db, schema_version="2")
    new_record = new_analyzer.analyze(make_article(), make_source(), now=NOW)

    assert db.get_assessment(old_record.key).assessment.topic_relevance == 5
    assert db.get_assessment(new_record.key).assessment.topic_relevance == 1


# --------------------------------------------------------------------------- #
# behaviour without a cache
# --------------------------------------------------------------------------- #


def test_an_analyzer_without_a_cache_always_calls_the_model() -> None:
    client, fake, _ = make_client(ok_response())
    analyzer = ArticleAnalyzer(client)

    analyzer.analyze(make_article(), make_source(), now=NOW)
    analyzer.analyze(make_article(), make_source(), now=NOW)

    assert len(fake.responses.calls) == 2


def test_a_failed_analysis_is_not_cached(db: Database) -> None:
    """A refusal must not be remembered as if it were a judgment."""
    from newsletter.intelligence.client import ModelRefusal
    from tests.unit.test_analyzer import refusal_response

    analyzer, _ = analyzer_with(db, refusal_response(), max_attempts=1)
    with pytest.raises(ModelRefusal):
        analyzer.analyze(make_article(), make_source(), now=NOW)

    assert db.get_assessment(analyzer.cache_key_for(make_article())) is None


def test_a_cached_assessment_is_linked_to_its_article(db: Database) -> None:
    analyzer, _ = analyzer_with(db)
    analyzer.analyze(make_article(), make_source(), now=NOW)

    row = db._require_connection().execute("SELECT article_id FROM assessments").fetchone()
    assert row["article_id"] == "a1"
