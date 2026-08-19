"""End-to-end pipeline over three fake sources.

No network, no API key, no clock: the HTTP client is a fake driven by
``tests/fixtures/integration/``, the OpenAI SDK is a fake, and the run timestamp
is injected. This is the test behind AC2 and AC15 — a complete edition, produced
offline, reproducibly.

The fixture set is deliberately awkward, because a pipeline that only survives
tidy input is not worth much:

* **alpha** — an RSS feed with two publishable stories and one from last month;
* **beta** — a scraped index whose second story is alpha's article republished
  verbatim under a different headline and URL;
* **gamma** — a source that is simply down.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from newsletter.config import AppConfig, NewsletterSettings, RuntimeSettings
from newsletter.context import RunContext
from newsletter.ingestion.rss import RssAdapter
from newsletter.ingestion.scrapling import ScraplingAdapter
from newsletter.ingestion.submissions import create_submission
from newsletter.intelligence.analyzer import ArticleAnalyzer
from newsletter.intelligence.editor import EditorialPayload, NewsletterEditor, StoryPolish
from newsletter.intelligence.schemas import AssessmentPayload
from newsletter.models import (
    DateWindow,
    NewsletterEdition,
    SourceConfig,
    SubmissionStatus,
    TopicCategory,
)
from newsletter.persistence.sqlite import Database
from newsletter.pipeline import NothingToPublish, PipelineError, run_pipeline
from tests.conftest import (
    FakeHttpClient,
    FakeOpenAI,
    FakeResponse,
    make_client,
    refusal_response,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "integration"

NOW = datetime(2026, 8, 18, 9, 0, tzinfo=UTC)
WINDOW = DateWindow.from_dates("2026-08-11", "2026-08-17")

ALPHA_FEED = "https://alpha.example/feed.xml"
ALPHA_1 = "https://alpha.example/news/reasoning-model"
ALPHA_2 = "https://alpha.example/news/payout-tiers"
ALPHA_OLD = "https://alpha.example/news/old-announcement"
BETA_INDEX = "https://beta.example/research"
BETA_1 = "https://beta.example/research/video-model"
BETA_2 = "https://beta.example/research/reasoning-reprint"
GAMMA_FEED = "https://gamma.example/feed.xml"

BETA_SELECTORS = {
    "index_item": "article.card",
    "link": "a.card-link",
    "title": "h2.card-title",
    "date": "time.card-date",
    "date_attr": "datetime",
}


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def make_sources() -> list[SourceConfig]:
    return [
        SourceConfig(
            id="alpha",
            name="Alpha Wire",
            entrypoint=ALPHA_FEED,
            strategy="rss",
            priority=10,
            category_hint=TopicCategory.AI_MODELS,
        ),
        SourceConfig(
            id="beta",
            name="Beta Research",
            entrypoint=BETA_INDEX,
            strategy="scrapling_static",
            priority=7,
            category_hint=TopicCategory.AI_VIDEO,
            selectors=BETA_SELECTORS,
        ),
        SourceConfig(
            id="gamma",
            name="Gamma Daily",
            entrypoint=GAMMA_FEED,
            strategy="rss",
            priority=5,
            category_hint=TopicCategory.AI_BUSINESS,
        ),
    ]


def make_config(tmp_path: Path, **newsletter_overrides: Any) -> AppConfig:
    settings: dict[str, Any] = {
        "masthead": "AI & Digital Intelligence Weekly",
        "tagline": "Integration fixture edition",
        "max_items": 8,
        "min_score": 70,
        "section_limits": {
            TopicCategory.AI_MODELS: 3,
            TopicCategory.AI_VIDEO: 3,
            TopicCategory.YOUTUBE_MONETIZATION: 2,
        },
        "section_order": [
            TopicCategory.AI_MODELS,
            TopicCategory.YOUTUBE_MONETIZATION,
            TopicCategory.AI_VIDEO,
        ],
    }
    settings.update(newsletter_overrides)
    return AppConfig(
        sources=make_sources(),
        newsletter=NewsletterSettings(**settings),
        runtime=RuntimeSettings(output_dir=tmp_path / "output", db_path=tmp_path / "news.sqlite"),
    )


@pytest.fixture
def http() -> FakeHttpClient:
    """Alpha and beta serve fixtures; gamma is down."""
    return FakeHttpClient(
        {
            ALPHA_FEED: fixture("alpha_feed.xml"),
            ALPHA_1: fixture("alpha_article_1.html"),
            ALPHA_2: fixture("alpha_article_2.html"),
            BETA_INDEX: fixture("beta_index.html"),
            BETA_1: fixture("beta_article_1.html"),
            BETA_2: fixture("beta_article_2.html"),
        },
        failures={GAMMA_FEED: "connection refused"},
    )


def adapter_factory(http: FakeHttpClient):
    def build(source: SourceConfig):
        if source.strategy.value == "rss":
            return RssAdapter(source, http=http)
        return ScraplingAdapter(source, http=http)

    return build


# --------------------------------------------------------------------------- #
# mocked intelligence
# --------------------------------------------------------------------------- #


def assessment_for(title: str) -> AssessmentPayload:
    """A plausible, deterministic assessment keyed off the headline."""
    if "reasoning model" in title.lower():
        values = {
            "category": TopicCategory.AI_MODELS,
            "topic_relevance": 5,
            "business_impact": 5,
            "novelty": 5,
        }
        event = ("Example Labs", "released", "reasoning model", "2026-08-17")
    elif "open-weight" in title.lower():
        values = {
            "category": TopicCategory.AI_MODELS,
            "topic_relevance": 5,
            "business_impact": 4,
            "novelty": 5,
        }
        event = ("Independent lab", "published", "open-weight model", "2026-08-14")
    elif "payout" in title.lower():
        values = {
            "category": TopicCategory.YOUTUBE_MONETIZATION,
            "topic_relevance": 5,
            "business_impact": 4,
            "novelty": 4,
        }
        event = ("Partner Program", "changed", "payout tiers", "2026-08-16")
    else:
        values = {
            "category": TopicCategory.AI_VIDEO,
            "topic_relevance": 4,
            "business_impact": 4,
            "novelty": 4,
        }
        event = ("Research group", "opened", "video model", "2026-08-15")

    subject, action, obj, date = event
    return AssessmentPayload(
        actionability=3,
        confidence=0.9,
        summary=f"Factual summary of: {title}",
        why_it_matters="It changes what enterprise teams pay and plan for.",
        key_facts=["Available now", "Pricing disclosed"],
        event_subject=subject,
        event_action=action,
        event_object=obj,
        event_date=date,
        **values,
    )


class ScriptedAnalyzerClient:
    """Answers with an assessment chosen from the article title in the request."""

    def __init__(self) -> None:
        self.model = "fake-analyzer"
        self.calls: list[str] = []

    def parse(self, *, instructions: str, content: str, schema: Any) -> tuple[Any, int]:
        title = next(
            line.removeprefix("title: ")
            for line in content.splitlines()
            if line.startswith("title: ")
        )
        self.calls.append(title)
        return assessment_for(title), 1


def editorial_response(article_ids: list[str]) -> FakeResponse:
    return FakeResponse(
        output_parsed=EditorialPayload(
            executive_summary=[
                "Model pricing fell sharply this week.",
                "Creator payouts shift in October.",
            ],
            stories=[
                StoryPolish(
                    article_id=article_id,
                    headline=f"Edited headline for {article_id}",
                    why_it_matters="Sharpened interpretation for an enterprise reader.",
                )
                for article_id in article_ids
            ],
        )
    )


def run_fixture_pipeline(
    tmp_path: Path,
    http: FakeHttpClient,
    *,
    database: Database | None = None,
    **config_overrides: Any,
):
    config = make_config(tmp_path, **config_overrides)
    context = RunContext.create(config, WINDOW, now=NOW)
    analyzer = ArticleAnalyzer(ScriptedAnalyzerClient(), cache=database)

    # The editor is consulted after selection, so the ids are only known then;
    # _EchoEditor rewires its fake to answer with whatever it was sent.
    client, _, _ = make_client(FakeResponse(output_parsed=None))
    editor = _EchoEditor(client)

    return run_pipeline(
        context,
        analyzer=analyzer,
        editor=editor,
        database=database,
        adapter_factory=adapter_factory(http),
        submission_http=http,
        now=NOW,
    )


class _EchoEditor(NewsletterEditor):
    """A NewsletterEditor whose model always polishes exactly what it was sent."""

    def compose(self, selection, settings, window, *, now=None):  # type: ignore[override]
        ids = [ranked.article.article_id for ranked in selection.selected]
        self.client.client = FakeOpenAI(editorial_response(ids))
        return super().compose(selection, settings, window, now=now)


# --------------------------------------------------------------------------- #
# the happy path, end to end
# --------------------------------------------------------------------------- #


def test_the_full_pipeline_produces_an_edition(tmp_path: Path, http: FakeHttpClient) -> None:
    result = run_fixture_pipeline(tmp_path, http)

    assert result.succeeded
    assert isinstance(result.edition, NewsletterEdition)
    assert result.manifest.newsletter_generated is True


def test_all_five_artifacts_are_written(tmp_path: Path, http: FakeHttpClient) -> None:
    """AC11."""
    result = run_fixture_pipeline(tmp_path, http)
    directory = tmp_path / "output" / "2026-W34"

    for filename in (
        "newsletter.html",
        "newsletter.md",
        "newsletter.json",
        "selected_articles.json",
        "run_manifest.json",
    ):
        assert (directory / filename).is_file(), filename
        assert (directory / filename).stat().st_size > 0
    assert set(result.outputs) == {"html", "markdown", "json", "selected_articles", "run_manifest"}


def test_the_broken_source_does_not_stop_the_others(tmp_path: Path, http: FakeHttpClient) -> None:
    """AC10."""
    result = run_fixture_pipeline(tmp_path, http)
    manifest = result.manifest

    assert manifest.sources_attempted == 3
    assert manifest.sources_succeeded == 2
    assert manifest.sources_failed == 1
    failures = [error for error in manifest.errors if error.source_id == "gamma"]
    assert failures and "connection refused" in failures[0].message
    assert result.edition is not None


def test_the_out_of_window_article_never_reaches_the_edition(
    tmp_path: Path, http: FakeHttpClient
) -> None:
    """AC6."""
    result = run_fixture_pipeline(tmp_path, http)

    assert ALPHA_OLD not in [item.source_url for item in result.edition.all_items()]
    for item in result.edition.all_items():
        assert WINDOW.contains(item.published_at)


def test_the_syndicated_copy_is_collapsed_by_the_event_fingerprint(
    tmp_path: Path, http: FakeHttpClient
) -> None:
    """Beta republished alpha's body under a different headline and URL.

    That copy survives all three deterministic passes — different URL, different
    content hash (the headline is part of the extracted text), different title —
    which is exactly the gap the analyzer event fingerprint exists to close. Only
    the higher-priority original is published.
    """
    result = run_fixture_pipeline(tmp_path, http)
    urls = [item.source_url for item in result.edition.all_items()]

    assert result.manifest.articles_after_deduplication == 4  # deterministic passes keep both
    assert ALPHA_1 in urls
    assert BETA_2 not in urls
    assert len(result.selected) == 3


def test_every_published_story_traces_back_to_an_ingested_url(
    tmp_path: Path, http: FakeHttpClient
) -> None:
    """AC3."""
    result = run_fixture_pipeline(tmp_path, http)
    ingested = {ranked.article.canonical_url for ranked in result.selected}

    for item in result.edition.all_items():
        assert item.source_url in ingested
        assert item.source_url.startswith("https://")


def test_the_rendered_html_is_clickable_and_scriptless(
    tmp_path: Path, http: FakeHttpClient
) -> None:
    """AC4 and AC12, asserted on the generated file."""
    result = run_fixture_pipeline(tmp_path, http)
    html = (tmp_path / "output" / "2026-W34" / "newsletter.html").read_text(encoding="utf-8")

    for item in result.edition.all_items():
        assert f'href="{item.source_url}"' in html
    assert html.count("read-original") >= len(result.edition.all_items())
    assert "<script" not in html.lower()
    assert "Lo esencial de la semana" in html


def test_the_rendered_markdown_links_every_story(tmp_path: Path, http: FakeHttpClient) -> None:
    """AC5."""
    result = run_fixture_pipeline(tmp_path, http)
    markdown = (tmp_path / "output" / "2026-W34" / "newsletter.md").read_text(encoding="utf-8")

    for item in result.edition.all_items():
        assert f"]({item.source_url})" in markdown


def test_the_manifest_records_the_whole_run(tmp_path: Path, http: FakeHttpClient) -> None:
    result = run_fixture_pipeline(tmp_path, http)
    payload = json.loads(
        (tmp_path / "output" / "2026-W34" / "run_manifest.json").read_text(encoding="utf-8")
    )

    assert payload["run_id"] == result.manifest.run_id
    assert payload["sources_attempted"] == 3
    assert payload["articles_discovered"] >= 4
    assert payload["articles_selected"] == len(result.edition.all_items())
    assert payload["newsletter_generated"] is True
    assert payload["finished_at"] is not None
    assert payload["analyzer_model"] == "fake-analyzer"
    assert payload["errors"], "the gamma failure must be visible in the manifest"


def test_editorial_polish_reaches_the_edition(tmp_path: Path, http: FakeHttpClient) -> None:
    result = run_fixture_pipeline(tmp_path, http)
    assert result.edition.lead_story.headline.startswith("Edited headline for ")


def test_a_failing_editor_costs_polish_not_the_edition(
    tmp_path: Path, http: FakeHttpClient
) -> None:
    """The stories, links and order were fixed before the editor was consulted."""
    config = make_config(tmp_path)
    context = RunContext.create(config, WINDOW, now=NOW)
    client, _, _ = make_client(refusal_response(), max_attempts=1)

    result = run_pipeline(
        context,
        analyzer=ArticleAnalyzer(ScriptedAnalyzerClient()),
        editor=NewsletterEditor(client),
        adapter_factory=adapter_factory(http),
        now=NOW,
    )

    assert result.succeeded
    assert not result.edition.lead_story.headline.startswith("Edited headline")
    assert any(error.stage.value == "edit" for error in result.manifest.errors)


# --------------------------------------------------------------------------- #
# reproducibility (AC9)
# --------------------------------------------------------------------------- #


def test_two_identical_runs_produce_identical_artifacts(
    tmp_path: Path, http: FakeHttpClient
) -> None:
    first = run_fixture_pipeline(tmp_path / "run-a", http)
    second = run_fixture_pipeline(tmp_path / "run-b", http)

    assert [item.article_id for item in first.edition.all_items()] == [
        item.article_id for item in second.edition.all_items()
    ]

    for filename in ("newsletter.html", "newsletter.md", "newsletter.json"):
        left = (tmp_path / "run-a" / "output" / "2026-W34" / filename).read_text(encoding="utf-8")
        right = (tmp_path / "run-b" / "output" / "2026-W34" / filename).read_text(encoding="utf-8")
        assert left == right, filename


# --------------------------------------------------------------------------- #
# persistence
# --------------------------------------------------------------------------- #


def test_the_run_is_persisted_and_traceable(tmp_path: Path, http: FakeHttpClient) -> None:
    with Database(tmp_path / "news.sqlite") as database:
        result = run_fixture_pipeline(tmp_path, http, database=database)

        assert database.get_run(result.manifest.run_id) is not None
        assert database.get_edition("2026-W34") is not None
        assert len(database.list_sources()) == 3

        for article_id in database.get_edition_article_ids("2026-W34"):
            article = database.get_article(article_id)
            assert article is not None
            assert database.get_source(article.source_id) is not None


def test_the_second_run_reuses_cached_assessments(tmp_path: Path, http: FakeHttpClient) -> None:
    with Database(tmp_path / "news.sqlite") as database:
        first = run_fixture_pipeline(tmp_path / "one", http, database=database)
        second = run_fixture_pipeline(tmp_path / "two", http, database=database)

    assert first.manifest.llm_calls > 0
    assert second.manifest.llm_calls == 0
    assert second.manifest.llm_cache_hits == first.manifest.llm_calls


# --------------------------------------------------------------------------- #
# failure modes
# --------------------------------------------------------------------------- #


def test_a_week_with_nothing_worth_publishing_is_not_a_crash(
    tmp_path: Path, http: FakeHttpClient
) -> None:
    with pytest.raises(NothingToPublish, match="threshold"):
        run_fixture_pipeline(tmp_path, http, min_score=100)


def test_every_source_failing_stops_the_run(tmp_path: Path) -> None:
    dead = FakeHttpClient({}, failures={ALPHA_FEED: "down", BETA_INDEX: "down", GAMMA_FEED: "down"})
    with pytest.raises(PipelineError, match="no source returned"):
        run_fixture_pipeline(tmp_path, dead)


def test_a_dry_run_writes_nothing(tmp_path: Path, http: FakeHttpClient) -> None:
    config = make_config(tmp_path)
    context = RunContext.create(config, WINDOW, dry_run=True, now=NOW)

    result = run_pipeline(
        context,
        analyzer=ArticleAnalyzer(ScriptedAnalyzerClient()),
        adapter_factory=adapter_factory(http),
        now=NOW,
    )

    assert result.edition is None
    assert result.outputs == {}
    assert result.manifest.llm_calls == 0
    assert not (tmp_path / "output").exists()


# --------------------------------------------------------------------------- #
# reader submissions, end to end
# --------------------------------------------------------------------------- #

SUBMITTED = "https://reader.example/news/submitted-model-story"
SUBMITTED_JUNK = "https://reader.example/news/thin-page"


def submission_http(http: FakeHttpClient) -> FakeHttpClient:
    """The fixture web, plus a page a reader submitted."""
    http.pages[SUBMITTED] = fixture("submitted_article.html")
    http.pages[SUBMITTED_JUNK] = (
        "<html><head><title>Thin</title></head><body><p>Hi.</p></body></html>"
    )
    return http


def test_a_submitted_link_can_reach_the_edition(tmp_path: Path, http: FakeHttpClient) -> None:
    """The whole point of the feature: anyone can propose a story."""
    with Database(tmp_path / "news.sqlite") as database:
        database.save_submission(
            create_submission(SUBMITTED, submitted_by="Ana", now=NOW, check_address=False)
        )
        result = run_fixture_pipeline(tmp_path, submission_http(http), database=database)

        published = [item for item in result.edition.all_items() if item.source_url == SUBMITTED]
        assert published, "the submitted story was not published"
        assert published[0].source_name == "Reader submission"

        stored = database.list_submissions()[0]
        assert stored.status is SubmissionStatus.PUBLISHED
        assert "2026-W34" in stored.reason
        assert stored.article_id == published[0].article_id


def test_a_submission_that_cannot_be_read_is_rejected_with_a_reason(
    tmp_path: Path, http: FakeHttpClient
) -> None:
    with Database(tmp_path / "news.sqlite") as database:
        database.save_submission(
            create_submission(SUBMITTED_JUNK, submitted_by="Bo", now=NOW, check_address=False)
        )
        run_fixture_pipeline(tmp_path, submission_http(http), database=database)

        stored = database.list_submissions()[0]
        assert stored.status is SubmissionStatus.REJECTED
        assert "could not be fetched or read" in stored.reason


def test_a_submission_is_scored_like_everything_else(tmp_path: Path, http: FakeHttpClient) -> None:
    """Submitting buys consideration, not publication: raise the bar and it drops."""
    with Database(tmp_path / "news.sqlite") as database:
        database.save_submission(
            create_submission(SUBMITTED, submitted_by="Ana", now=NOW, check_address=False)
        )
        with pytest.raises(NothingToPublish):
            run_fixture_pipeline(tmp_path, submission_http(http), database=database, min_score=100)

        stored = database.list_submissions()[0]
        assert stored.status is SubmissionStatus.PENDING  # decided only once an edition exists


def test_submissions_are_ignored_when_the_feature_is_off(
    tmp_path: Path, http: FakeHttpClient
) -> None:
    config = make_config(tmp_path)
    config = config.model_copy(
        update={"submissions": config.submissions.model_copy(update={"enabled": False})}
    )
    context = RunContext.create(config, WINDOW, now=NOW)
    with_submissions = submission_http(http)

    with Database(tmp_path / "news.sqlite") as database:
        database.save_submission(create_submission(SUBMITTED, now=NOW, check_address=False))
        client, _, _ = make_client(FakeResponse(output_parsed=None))
        result = run_pipeline(
            context,
            analyzer=ArticleAnalyzer(ScriptedAnalyzerClient()),
            editor=_EchoEditor(client),
            database=database,
            adapter_factory=adapter_factory(with_submissions),
            submission_http=with_submissions,
            now=NOW,
        )

        assert SUBMITTED not in [item.source_url for item in result.edition.all_items()]
        assert database.list_submissions()[0].status is SubmissionStatus.PENDING
