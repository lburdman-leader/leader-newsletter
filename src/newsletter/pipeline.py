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
from functools import partial
from pathlib import Path

from newsletter.config import AppConfig, NewsletterSettings
from newsletter.context import RunContext
from newsletter.ingestion.base import AdapterFactory, SourceAdapter, build_adapter, ingest_all
from newsletter.ingestion.http import HttpClient, UrllibHttpClient, build_ssl_context
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
    AssessmentRecord,
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
from newsletter.ranking.pool import merge_stored, round_robin, split_reserved
from newsletter.ranking.scoring import rank_all
from newsletter.ranking.selection import (
    REASON_ALREADY_PUBLISHED,
    REASON_COVERAGE_FLOOR,
    REASON_SIMILAR_EVENT,
    REASON_UNSUPPORTED_ENTITY,
    RejectedArticle,
    SelectionResult,
    select,
    submitted_detail,
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
    min_score: int | None,
    issue_label: str,
    now: datetime,
) -> list[Submission]:
    """Explain, for every submission this run considered, what became of it.

    Each outcome carries a reason a submitter could read without knowing anything
    about the internals. A submission that was never reached, because the per-run
    cap was hit, is left pending rather than turned down.

    ``min_score`` is ``None`` when the run reserved slots for submissions: the
    floor did not apply, so it cannot be the reason one was turned down, and a
    submission that still did not print simply did not fit this edition.
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
        elif min_score is not None and scores[article_id] < min_score:
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


@dataclass(frozen=True)
class AnalysisBudget:
    """Everything the adaptive assessment loop needs to decide when to stop.

    Bundled rather than passed as eight keyword arguments: the loop's whole job is
    to run :func:`select` repeatedly, and it must run it with *exactly* the
    arguments the real selection will use, or it would stop on an edition the run
    then fails to produce.
    """

    settings: NewsletterSettings
    sources: Mapping[str, SourceConfig]
    published: PublishedKeys | None
    reserved_source_id: str | None
    reserved_slots: int | None
    priorities: Mapping[str, int]

    def probe(self, assessed: Sequence[tuple[NormalizedArticle, AssessmentRecord]]) -> bool:
        """Could this edition be published as it stands?

        The stopping rule, and the reason the two features ship together: an
        edition of ten stories from the wrong beat is *not* finished, so a run
        that stopped on "ten of anything" would make a coverage floor
        unsatisfiable whenever the qualifying stories sat deep in the pool. No
        manifest is passed: a probe must leave no trace.
        """
        return select(
            rank_all(assessed, self.sources),
            self.settings,
            published=self.published,
            reserved_source_id=self.reserved_source_id,
            reserved_slots=self.reserved_slots,
        ).is_complete


def analyze_pool(
    analyzer: ArticleAnalyzer,
    candidates: Sequence[NormalizedArticle],
    *,
    budget: AnalysisBudget,
    manifest: RunManifest,
    now: datetime,
) -> list[tuple[NormalizedArticle, AssessmentRecord]]:
    """Assess as much of the pool as the edition actually needs, and no more.

    Assessment is the expensive stage -- one model call per candidate, and a real
    week offers well over a hundred for an edition of ten -- so the pool is
    bounded and worked through in batches, stopping as soon as the edition could
    be published. Two rules keep the bound from quietly changing what is
    published:

    * **Reader submissions are assessed first and outside the budget.** A link a
      reader was promised a slot for is never crowded out by a cost ceiling.
    * **The rest is ordered round-robin across sources** (see
      :mod:`newsletter.ranking.pool`), so the cap trims the eleventh story from
      one outlet rather than every story from a whole beat.
    * **Only a cache miss spends the budget.** The ceiling exists to bound model
      calls, and a cached assessment is not one. Counting hits against it would
      make recall self-defeating: a pool of articles the engine already paid to
      assess -- all free -- would exhaust the ceiling before a single new story
      was read.

    ``analysis_pool_max`` of ``0`` or ``None`` restores the exhaustive behaviour
    exactly: one pass over the pool in its original order, with no probing.
    """
    manifest.articles_available = len(candidates)
    cap = budget.settings.analysis_pool_cap
    if cap is None:
        assessed = analyzer.analyze_all(candidates, budget.sources, manifest=manifest, now=now)
        manifest.articles_analyzed = len(candidates)
        return assessed

    reserved, rest = split_reserved(candidates, reserved_source_id=budget.reserved_source_id)
    # Both halves are ordered, not only the budgeted one: assessment order decides
    # the order failures reach the manifest and assessments reach the cache, and
    # neither may depend on the order ingestion happened to return (AC9).
    reserved = round_robin(reserved, priorities=budget.priorities)
    ordered = round_robin(rest, priorities=budget.priorities)

    assessed: list[tuple[NormalizedArticle, AssessmentRecord]] = []
    attempted = 0
    if reserved:
        assessed.extend(analyzer.analyze_all(reserved, budget.sources, manifest=manifest, now=now))
        attempted += len(reserved)
        report(f"{len(reserved)} reader submissions assessed ahead of the pool")

    size = max(budget.settings.analysis_pool_min, 1)
    spent = 0
    start = 0
    while start < len(ordered) and spent < cap:
        end = min(start + size, len(ordered))
        called = manifest.llm_calls
        assessed.extend(
            analyzer.analyze_all(ordered[start:end], budget.sources, manifest=manifest, now=now)
        )
        spent += manifest.llm_calls - called
        attempted += end - start
        start = end
        if budget.probe(assessed):
            break

    manifest.articles_analyzed = attempted
    report(f"{attempted} of {len(candidates)} candidates assessed ({spent} of {cap} calls spent)")
    return assessed


def drop_unsupported_stories(
    selection: SelectionResult,
    *,
    manifest: RunManifest,
    min_items: int = 0,
    now: datetime,
) -> int:
    """Remove selected stories whose analyst prose names an entity their source does not.

    A corrupted proper name is not a cosmetic defect — ``UTube`` is a company
    that does not exist — so the story goes rather than the edition, and the rest
    of the line-up is published as usual. A reserved slot buys no exemption:
    corrupted prose is a defect whoever proposed the link.

    Nothing is dropped quietly: each drop lands in the run manifest twice over —
    as the error that caused it, and as the story it cost, which is the record
    that says whether a reader's reserved slot went empty — and on the console
    and in the selection's own rejection reasons. Returns how many stories were
    dropped.

    ``min_items`` is passed so the manifest's shortfall stays true: a guard that
    drops two stories from a line-up of six has made the edition short, and the
    number recorded at selection time no longer describes what is printed. The
    guard never relaxes anything to make up the loss -- corrupted prose is a
    defect, and a headcount is not a reason to print one.
    """
    kept: list[RankedArticle] = []
    reserved_ids = selection.reserved_ids
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
        manifest.record_withheld(
            article_id=ranked.article.article_id,
            url=ranked.article.canonical_url,
            title=ranked.article.title,
            reason=REASON_UNSUPPORTED_ENTITY,
            detail=(
                submitted_detail(detail) if ranked.article.article_id in reserved_ids else detail
            ),
        )
        report_failure(
            f'story "{ranked.article.title}" dropped: {detail}, which its source never does'
        )
        selection.rejected.append(RejectedArticle(ranked=ranked, reason=REASON_UNSUPPORTED_ENTITY))

    dropped = len(selection.selected) - len(kept)
    if dropped:
        surviving = {ranked.article.article_id for ranked in kept}
        selection.selected = kept
        selection.reserved = [
            ranked for ranked in selection.reserved if ranked.article.article_id in surviving
        ]
        manifest.articles_selected = len(kept)
        manifest.articles_reserved = len(selection.reserved)
        selection.items_short = max(min_items - len(kept), 0)
        manifest.min_items_unmet = selection.items_short
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

    # One transport for the whole run, so the configured TLS trust applies to
    # every fetch rather than to whichever adapter remembered to ask for it.
    # An injected factory or client still wins: that is how tests stay offline.
    default_http = _default_http_client(config)

    submission_adapter: SubmissionAdapter | None = None
    if config.submissions.enabled and database is not None:
        pending = database.pending_submissions(limit=config.submissions.max_per_run)
        if pending:
            submission_source = config.submissions.as_source()
            submission_adapter = SubmissionAdapter(
                submission_source,
                pending,
                http=submission_http or default_http,
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
    if adapter_factory is None and default_http is not None:
        adapter_factory = partial(build_adapter, http=default_http)
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
    fetched_in_window = len(in_window)
    report(f"{fetched_in_window} inside the date window ({len(outside)} outside)")

    # -- recall (the pool is not only what today's feeds still carry) ------- #
    # A feed holds its last ten to fifty items, so any window older than a few
    # days is starved by construction however well the fetch works. An in-window
    # article the engine already ingested is a legitimate candidate, and one it
    # already assessed costs nothing to reconsider, so storage joins the pool
    # unconditionally. The three deduplication passes below own the overlap.
    if database is not None:
        in_window = merge_stored(in_window, database.articles_in_window(window))
    manifest.articles_recalled = len(in_window) - fetched_in_window
    manifest.articles_in_window = len(in_window)
    if manifest.articles_recalled:
        report(f"{manifest.articles_recalled} further in-window articles recalled from storage")

    if not in_window:
        raise NothingToPublish("no article falls inside the configured date window")

    # -- deduplicate -------------------------------------------------------- #
    # While slots are reserved, the reader's copy wins a collision: the submitted
    # link is the one holding the slot, and keeping the configured source's copy
    # instead would quietly turn a guaranteed story back into one that has to earn
    # its place. With reservation off, `reserved_source` is None and the ordering
    # is priority alone, exactly as before.
    reserved_source = (
        config.submissions.source_id
        if submission_adapter is not None and config.submissions.reserved_slots != 0
        else None
    )
    priorities = {source.id: source.priority for source in sources_by_id.values()}
    deduped = deduplicate(in_window, priorities=priorities, preferred_source_id=reserved_source)
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

    # "Every news should be posted only once." The keys are read here, where the
    # database lives, and handed to select(), which stays a pure function of its
    # arguments. The issue being produced now is excluded, so re-running the same
    # week reproduces it rather than suppressing everything it printed. They are
    # read before assessment because the adaptive loop asks the same question the
    # real selection will, and a probe that ignored suppression would stop on an
    # edition of stories last week already printed.
    published_keys: PublishedKeys | None = None
    if database is not None and config.newsletter.suppress_already_published:
        published_keys = database.published_identity_keys(
            exclude_edition_id=context.issue_label,
        )

    assessed = analyze_pool(
        analyzer,
        deduped.kept,
        budget=AnalysisBudget(
            settings=config.newsletter,
            sources=sources_by_id,
            published=published_keys,
            reserved_source_id=reserved_source,
            reserved_slots=config.submissions.reserved_slots,
            priorities=priorities,
        ),
        manifest=manifest,
        now=stamp,
    )
    report(f"{manifest.llm_cache_hits} assessments loaded from cache")
    report(f"{manifest.llm_calls} articles analyzed")
    if not assessed:
        raise PipelineError("no article could be assessed; the edition would be empty")

    # -- score and select --------------------------------------------------- #
    ranked = rank_all(assessed, sources_by_id)

    selection: SelectionResult = select(
        ranked,
        config.newsletter,
        manifest=manifest,
        published=published_keys,
        reserved_source_id=reserved_source,
        reserved_slots=config.submissions.reserved_slots,
    )
    for suppressed in selection.rejections_for(REASON_ALREADY_PUBLISHED):
        report(f'"{suppressed.ranked.article.title}" not reprinted: {suppressed.detail}')
    for folded in selection.rejections_for(REASON_SIMILAR_EVENT):
        report(f'"{folded.ranked.article.title}" folded in: {folded.detail}')
    report(f"{selection.above_threshold} scored >= {config.newsletter.min_score}")
    for lost in selection.rejections_for(REASON_COVERAGE_FLOOR):
        report(f'"{lost.ranked.article.title}" gave up its slot: {lost.detail}')
    for name, short in selection.floors_unmet.items():
        report_failure(
            f"coverage floor {name!r} unmet: {short} short. "
            "The week offered no further qualifying story; nothing was padded."
        )
    if selection.relaxed_settings is not None:
        report(
            f"rationing caps relaxed {selection.relaxation_steps} step(s) to reach "
            f"min_items {config.newsletter.min_items}: sections "
            f"{ {c.value: n for c, n in selection.relaxed_settings.section_limits.items()} }, "
            f"per source {selection.relaxed_settings.max_per_source}, "
            f"per subject {selection.relaxed_settings.max_per_subject}. "
            "No score, category or duplicate rule moved."
        )
    if selection.items_short:
        report_failure(
            f"edition is {selection.items_short} short of min_items "
            f"{config.newsletter.min_items}. The week offered no further qualifying "
            "story; nothing was padded."
        )
    if selection.reserved:
        report(
            f"{len(selection.reserved)} reader submissions took a reserved slot; "
            f"{len(selection.selected) - len(selection.reserved)} stories earned theirs"
        )
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
        drop_unsupported_stories(
            selection, manifest=manifest, min_items=config.newsletter.min_items, now=stamp
        )
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
            min_score=None if reserved_source is not None else config.newsletter.min_score,
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


def _default_http_client(config: AppConfig) -> UrllibHttpClient | None:
    """The run's shared transport, or None when the defaults already do.

    ``build_ssl_context`` returns None unless TLS trust is actually configured,
    and in that case an adapter left to build its own client behaves identically
    -- so nothing is constructed, and the untouched path stays untouched.
    """
    context = build_ssl_context(
        ca_bundle=config.runtime.tls.ca_bundle,
        relax_x509_strict=config.runtime.tls.relax_x509_strict,
    )
    return None if context is None else UrllibHttpClient(ssl_context=context)


def _with_submissions(
    factory: AdapterFactory | None, submission_adapter: SubmissionAdapter | None
) -> AdapterFactory | None:
    """Route the synthetic submission source to its adapter; everything else as usual."""
    if submission_adapter is None:
        return factory

    inner = factory or build_adapter

    def build(source: SourceConfig) -> SourceAdapter:
        if source.id == submission_adapter.source.id:
            return submission_adapter
        return inner(source)

    return build
