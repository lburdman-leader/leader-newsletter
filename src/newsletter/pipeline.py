"""The pipeline: one explicit pass through the state machine.

    LOAD CONFIG -> DISCOVER -> FETCH -> NORMALIZE -> HARD FILTER -> DEDUPLICATE
    -> ANALYZE -> SCORE -> SELECT -> EDIT -> VALIDATE -> RENDER -> PERSIST

Control flow lives here, in plain Python. Each stage has typed inputs and outputs,
writes its counts into the `RunManifest`, and records rather than hides its
failures.

**What fails the whole run** (PRD section 34) and nothing else: invalid
configuration, persistence that will not initialise, no usable article data,
output that cannot be written, and an edition that fails link validation. A
broken source, an unreadable page, or an article the model cannot assess are all
recorded and survived.

A week where nothing clears the threshold is not a failure — it raises
:class:`NothingToPublish`, which the CLI reports with its own exit code so a
scheduled job can tell a quiet week from a broken one.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from newsletter.config import AppConfig
from newsletter.context import RunContext
from newsletter.ingestion.base import AdapterFactory, SourceAdapter, ingest_all
from newsletter.ingestion.http import HttpClient
from newsletter.ingestion.submissions import SubmissionAdapter
from newsletter.intelligence.analyzer import ArticleAnalyzer
from newsletter.intelligence.client import StructuredClient, build_openai_client
from newsletter.intelligence.editor import NewsletterEditor, build_edition
from newsletter.intelligence.fidelity import (
    EntityFidelityError,
    describe_violations,
    unsupported_in_assessment,
    unsupported_in_edition,
)
from newsletter.logging_setup import get_logger, report, report_failure
from newsletter.models import (
    NewsletterEdition,
    NormalizedArticle,
    PipelineStage,
    RankedArticle,
    RunManifest,
    SourceConfig,
    Submission,
    SubmissionStatus,
)
from newsletter.normalization.article import normalize_all
from newsletter.normalization.filtering import filter_by_window
from newsletter.persistence.base import PersistenceError, Storage
from newsletter.persistence.factory import create_storage
from newsletter.ranking.dedupe import PublishedKeys, deduplicate
from newsletter.ranking.scoring import rank_all
from newsletter.ranking.selection import (
    REASON_ALREADY_PUBLISHED,
    REASON_SIMILAR_EVENT,
    REASON_UNSUPPORTED_ENTITY,
    RejectedArticle,
    SelectionResult,
    select,
)
from newsletter.rendering.renderer import RenderError, write_edition

logger = get_logger("pipeline")


class PipelineError(Exception):
    """A condition that must stop the whole run."""


class NothingToPublish(PipelineError):
    """No story cleared the threshold. A legitimate outcome, not a defect."""


@dataclass
class PipelineResult:
    """Everything a caller needs to report on the run."""

    manifest: RunManifest
    edition: NewsletterEdition | None = None
    outputs: dict[str, Path] = field(default_factory=dict)
    selected: list[RankedArticle] = field(default_factory=list)
    submissions: list[Submission] = field(default_factory=list)

    @property
    def succeeded(self) -> bool:
        return self.edition is not None and bool(self.outputs)


def build_analyzer(config: AppConfig, cache: Storage | None) -> ArticleAnalyzer:
    """Construct the analyzer from configuration. Requires a credential."""
    if not config.runtime.has_openai_key:
        raise PipelineError(
            "OPENAI_API_KEY is not set. Use --dry-run, or add the key to .env for a live run."
        )
    assert config.runtime.openai_api_key is not None
    client = build_openai_client(
        config.runtime.openai_api_key.get_secret_value(),
        timeout=config.runtime.request_timeout_seconds,
    )
    return ArticleAnalyzer(
        StructuredClient(
            client,
            model=config.runtime.analyzer_model,
            timeout=config.runtime.request_timeout_seconds,
            max_attempts=config.runtime.max_retries + 1,
        ),
        cache=cache,
        concurrency=config.runtime.analysis_concurrency,
    )


def build_editor(config: AppConfig) -> NewsletterEditor:
    assert config.runtime.openai_api_key is not None
    client = build_openai_client(
        config.runtime.openai_api_key.get_secret_value(),
        timeout=config.runtime.request_timeout_seconds,
    )
    return NewsletterEditor(
        StructuredClient(
            client,
            model=config.runtime.editor_model,
            timeout=config.runtime.request_timeout_seconds,
            max_attempts=config.runtime.max_retries + 1,
            max_output_tokens=4000,
        )
    )


def open_database(config: AppConfig) -> Storage:
    """Open the configured database, whichever engine it names.

    The DSN decides; an unknown scheme has already been refused by configuration
    loading, and is refused again here rather than falling back to a local file.
    """
    try:
        return create_storage(config.runtime.database_url).connect()
    except PersistenceError as exc:
        raise PipelineError(f"persistence could not initialise: {exc}") from exc


def decide_submissions(
    adapter: SubmissionAdapter,
    *,
    normalized: Sequence[NormalizedArticle],
    in_window_ids: set[str],
    kept_ids: set[str],
    scores: Mapping[str, int],
    published_ids: set[str],
    min_score: int,
    issue_label: str,
    now: datetime,
) -> list[Submission]:
    """Explain, for every submission this run considered, what became of it.

    Each outcome carries a reason a submitter could read without knowing anything
    about the internals. A submission that was never reached, because the per-run
    cap was hit, is left pending rather than turned down.
    """
    article_by_url: dict[str, NormalizedArticle] = {}
    for article in normalized:
        if article.source_id != adapter.source.id:
            continue
        for key in (article.origin_url, article.canonical_url):
            if key:
                article_by_url[key] = article

    decisions: list[Submission] = []
    for submission in adapter.by_url.values():
        article = article_by_url.get(submission.url)

        if article is None:
            decisions.append(
                submission.decide(
                    SubmissionStatus.REJECTED,
                    "the page could not be fetched or read: no title, no publication date, "
                    "or too little text to assess",
                    now=now,
                )
            )
            continue

        article_id = article.article_id
        if article_id not in in_window_ids:
            outcome = (SubmissionStatus.REJECTED, "published outside the current edition window")
        elif article_id not in kept_ids:
            outcome = (SubmissionStatus.REJECTED, "duplicate of a story already in this edition")
        elif article_id not in scores:
            outcome = (SubmissionStatus.REJECTED, "could not be assessed")
        elif article_id in published_ids:
            outcome = (SubmissionStatus.PUBLISHED, f"published in issue {issue_label}")
        elif scores[article_id] < min_score:
            outcome = (
                SubmissionStatus.REJECTED,
                f"scored {scores[article_id]}, below the threshold of {min_score}",
            )
        else:
            outcome = (
                SubmissionStatus.APPROVED,
                f"scored {scores[article_id]} but did not fit this edition",
            )

        status, reason = outcome
        decisions.append(submission.decide(status, reason, now=now, article_id=article_id))

    return decisions


def drop_unsupported_stories(
    selection: SelectionResult, *, manifest: RunManifest, now: datetime
) -> int:
    """Remove selected stories whose analyst prose names an entity their source does not.

    A corrupted proper name is not a cosmetic defect — ``UTube`` is a company
    that does not exist — so the story goes rather than the edition, and the rest
    of the line-up is published as usual. Nothing is dropped quietly: each drop
    lands in the run manifest, on the console and in the selection's own
    rejection reasons. Returns how many stories were dropped.
    """
    kept: list[RankedArticle] = []
    for ranked in selection.selected:
        violations = unsupported_in_assessment(ranked)
        if not violations:
            kept.append(ranked)
            continue

        detail = describe_violations(violations)
        manifest.record_error(
            PipelineStage.VALIDATE,
            EntityFidelityError(f"{ranked.article.canonical_url}: {detail}"),
            source_id=ranked.article.source_id,
            now=now,
        )
        report_failure(
            f'story "{ranked.article.title}" dropped: {detail}, which its source never does'
        )
        selection.rejected.append(RejectedArticle(ranked=ranked, reason=REASON_UNSUPPORTED_ENTITY))

    dropped = len(selection.selected) - len(kept)
    if dropped:
        selection.selected = kept
        manifest.articles_selected = len(kept)
    return dropped


def run_pipeline(
    context: RunContext,
    *,
    analyzer: ArticleAnalyzer | None = None,
    editor: NewsletterEditor | None = None,
    database: Storage | None = None,
    adapter_factory: AdapterFactory | None = None,
    submission_http: HttpClient | None = None,
    now: datetime | None = None,
) -> PipelineResult:
    """Execute one full run. Injected collaborators keep this testable offline."""
    config = context.config
    manifest = context.manifest
    window = context.window
    stamp = now or datetime.now(UTC)
    sources = config.enabled_sources

    submission_adapter: SubmissionAdapter | None = None
    if config.submissions.enabled and database is not None:
        pending = database.pending_submissions(limit=config.submissions.max_per_run)
        if pending:
            submission_source = config.submissions.as_source()
            submission_adapter = SubmissionAdapter(
                submission_source,
                pending,
                http=submission_http,
                follow_links=config.submissions.follow_links,
                min_text_chars=config.submissions.min_text_chars,
                max_link_hops=config.submissions.max_link_hops,
                max_linked_chars=config.submissions.max_linked_chars,
                blocked_hosts=config.submissions.blocked_hosts,
            )
            sources = [*sources, submission_source]
            report(f"{len(pending)} reader submissions pending")

    report(f"Loaded {len(sources)} sources ({len(config.sources)} configured)")

    if database is not None:
        for source in sources:
            database.upsert_source(source, now=stamp)

    # -- discover + fetch --------------------------------------------------- #
    factory = _with_submissions(adapter_factory, submission_adapter)
    ingestion = ingest_all(
        sources,
        window,
        manifest=manifest,
        adapter_factory=factory,
        concurrency=config.runtime.fetch_concurrency,
    )
    report(f"{ingestion.discovered} articles discovered")
    for outcome in ingestion.failed:
        report_failure(f"source {outcome.source_id} failed: {outcome.reason}")
    if not ingestion.raw_articles:
        raise PipelineError("no source returned a usable article; nothing to work with")
    report(f"{len(ingestion.raw_articles)} articles fetched")

    # -- normalize ---------------------------------------------------------- #
    hints = {
        candidate.url: candidate
        for outcome in ingestion.outcomes
        for candidate in outcome.candidates
    }
    sources_by_id = {source.id: source for source in config.sources}
    if submission_adapter is not None:
        sources_by_id[submission_adapter.source.id] = submission_adapter.source
    normalized: list[NormalizedArticle] = normalize_all(
        ingestion.raw_articles, sources_by_id, manifest=manifest, hints=hints
    )
    report(f"{len(normalized)} articles normalized")
    if not normalized:
        raise PipelineError("no article could be normalized; nothing to work with")

    # -- hard filter (AC6) -------------------------------------------------- #
    in_window, outside = filter_by_window(normalized, window)
    manifest.articles_in_window = len(in_window)
    report(f"{len(in_window)} inside the date window ({len(outside)} outside)")
    if not in_window:
        raise NothingToPublish("no article falls inside the configured date window")

    # -- deduplicate -------------------------------------------------------- #
    priorities = {source.id: source.priority for source in sources_by_id.values()}
    deduped = deduplicate(in_window, priorities=priorities)
    manifest.articles_after_deduplication = len(deduped.kept)
    report(
        f"{len(deduped.kept)} after deterministic deduplication "
        f"({deduped.dropped_count} duplicates dropped)"
    )

    if database is not None:
        new_articles = database.save_articles(deduped.kept)
        logger.info("persisted %d articles (%d new)", len(deduped.kept), new_articles)

    if context.dry_run:
        report("Dry run: stopping before analysis. No OpenAI calls, no files written.")
        context.finish(now=stamp)
        return PipelineResult(manifest=manifest)

    # -- analyze ------------------------------------------------------------ #
    if analyzer is None:
        analyzer = build_analyzer(config, database)
    manifest.analyzer_model = analyzer.model
    manifest.analyzer_prompt_version = analyzer.prompt_version
    manifest.schema_version = analyzer.schema_version

    assessed = analyzer.analyze_all(deduped.kept, sources_by_id, manifest=manifest, now=stamp)
    report(f"{manifest.llm_cache_hits} assessments loaded from cache")
    report(f"{manifest.llm_calls} articles analyzed")
    if not assessed:
        raise PipelineError("no article could be assessed; the edition would be empty")

    # -- score and select --------------------------------------------------- #
    ranked = rank_all(assessed, sources_by_id)

    # "Every news should be posted only once." The keys are read here, where the
    # database lives, and handed to select(), which stays a pure function of its
    # arguments. The issue being produced now is excluded, so re-running the same
    # week reproduces it rather than suppressing everything it printed.
    published_keys: PublishedKeys | None = None
    if database is not None and config.newsletter.suppress_already_published:
        published_keys = database.published_identity_keys(
            exclude_edition_id=context.issue_label,
        )

    selection: SelectionResult = select(
        ranked, config.newsletter, manifest=manifest, published=published_keys
    )
    for suppressed in selection.rejections_for(REASON_ALREADY_PUBLISHED):
        report(f'"{suppressed.ranked.article.title}" not reprinted: {suppressed.detail}')
    for folded in selection.rejections_for(REASON_SIMILAR_EVENT):
        report(f'"{folded.ranked.article.title}" folded in: {folded.detail}')
    report(f"{selection.above_threshold} scored >= {config.newsletter.min_score}")
    report(f"{len(selection.selected)} selected")

    if selection.is_empty:
        context.finish(now=stamp)
        if database is not None:
            database.save_run(manifest)
        raise NothingToPublish(
            f"no story reached the threshold of {config.newsletter.min_score} "
            f"({selection.reasons()})"
        )

    # -- entity fidelity, before the editor sees anything ------------------- #
    if config.newsletter.check_entity_fidelity:
        drop_unsupported_stories(selection, manifest=manifest, now=stamp)
        if selection.is_empty:
            context.finish(now=stamp)
            if database is not None:
                database.save_run(manifest)
            raise NothingToPublish("every selected story named an entity its own source never does")

    # -- editorial synthesis ------------------------------------------------ #
    if editor is None:
        editor = build_editor(config)
    manifest.editor_model = editor.model
    manifest.editor_prompt_version = editor.prompt_version

    edition, editorial_error = editor.compose_or_fallback(
        selection, config.newsletter, window, now=stamp
    )
    if editorial_error is not None:
        manifest.record_error(PipelineStage.EDIT, editorial_error, now=stamp)
        report_failure("editorial synthesis failed; published the deterministic edition")
    else:
        report("Editorial synthesis complete")

    # -- entity fidelity, on what the editor wrote -------------------------- #
    # Only when there is polish to judge: a fallback edition prints the
    # ingested title and the configured source name, neither of which the
    # model wrote.
    if config.newsletter.check_entity_fidelity and editorial_error is None:
        violations = unsupported_in_edition(edition, selection.selected)
        if violations:
            fidelity_error = EntityFidelityError(
                f"editorial prose named unsupported entities: {describe_violations(violations)}"
            )
            manifest.record_error(PipelineStage.VALIDATE, fidelity_error, now=stamp)
            report_failure(
                f"editorial polish discarded: {describe_violations(violations)}, "
                "which no published source does"
            )
            edition = build_edition(selection, config.newsletter, window, now=stamp)

    # -- validate and render ------------------------------------------------ #
    allowed = {ranked_article.article.canonical_url for ranked_article in selection.selected}
    try:
        outputs = write_edition(
            edition,
            context.edition_dir,
            ranked=selection.selected,
            manifest=manifest,
            tagline=config.newsletter.tagline,
            submit_url=config.submissions.form_url,
            allowed_urls=allowed,
        )
    except RenderError as exc:
        manifest.record_error(PipelineStage.VALIDATE, exc, now=stamp)
        raise PipelineError(f"edition failed link validation: {exc}") from exc
    except OSError as exc:
        manifest.record_error(PipelineStage.RENDER, exc, now=stamp)
        raise PipelineError(f"could not write the edition: {exc}") from exc

    submission_decisions: list[Submission] = []
    if submission_adapter is not None and database is not None:
        submission_decisions = decide_submissions(
            submission_adapter,
            normalized=normalized,
            in_window_ids={article.article_id for article in in_window},
            kept_ids={article.article_id for article in deduped.kept},
            scores={item.article.article_id: item.final_score for item in ranked},
            published_ids={item.article_id for item in edition.all_items()},
            min_score=config.newsletter.min_score,
            issue_label=window.issue_label(),
            now=stamp,
        )
        for decision in submission_decisions:
            database.save_submission(decision)
        published = sum(1 for d in submission_decisions if d.status is SubmissionStatus.PUBLISHED)
        report(f"{published} of {len(submission_decisions)} reader submissions published")

    manifest.newsletter_generated = True
    manifest.output_paths = {name: str(path) for name, path in outputs.items()}
    report(f"Newspaper edition rendered to {context.edition_dir}")

    # -- persist ------------------------------------------------------------ #
    context.finish(now=stamp)
    if database is not None:
        database.save_edition(edition, output_paths=manifest.output_paths)
        database.save_run(manifest)
        report(f"Run {manifest.run_id} persisted")

    # The manifest is written twice on purpose: once with the render, and again
    # here so the file on disk includes the finished timestamp and any error
    # recorded after rendering.
    _rewrite_manifest(outputs, manifest)

    if manifest.errors:
        report_failure(f"{len(manifest.errors)} non-fatal errors recorded in the run manifest")

    return PipelineResult(
        manifest=manifest,
        edition=edition,
        outputs=outputs,
        selected=list(selection.selected),
        submissions=submission_decisions,
    )


def _rewrite_manifest(outputs: dict[str, Path], manifest: RunManifest) -> None:
    path = outputs.get("run_manifest")
    if path is not None:
        path.write_text(manifest.model_dump_json(indent=2), encoding="utf-8", newline="\n")


def _with_submissions(
    factory: AdapterFactory | None, submission_adapter: SubmissionAdapter | None
) -> AdapterFactory | None:
    """Route the synthetic submission source to its adapter; everything else as usual."""
    if submission_adapter is None:
        return factory

    from newsletter.ingestion.base import build_adapter

    inner = factory or build_adapter

    def build(source: SourceConfig) -> SourceAdapter:
        if source.id == submission_adapter.source.id:
            return submission_adapter
        return inner(source)

    return build
