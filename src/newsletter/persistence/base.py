"""The storage contract.

The pipeline does not care which database it is talking to; it cares that a
handful of operations exist and mean the same thing everywhere. That surface is
:class:`Storage`, a structural protocol in the same idiom as
``ingestion.base.SourceAdapter`` and ``ingestion.http.HttpClient``.

The protocol is deliberately *not* the whole of :class:`~newsletter.persistence.sqlite.Database`.
It is exactly what ``pipeline.py`` and ``cli.py`` call, so an alternative backend
is judged against what the application actually needs rather than against one
implementation's history. Read-back helpers such as ``get_article``,
``get_edition_article_ids`` or ``recent_runs`` are used by tests, scripts and
audits; concrete backends provide them, but the running edition does not depend
on them and the contract stays small enough to implement correctly.

Two rules bind every implementation:

* **Timestamps are aware.** Every datetime that goes in comes back with the same
  instant *and* a tzinfo. ``DateWindow.contains`` refuses naive input, so a
  backend that returns naive datetimes silently changes selection.
* **A payload round-trips into the same model.** Each table stores queryable
  columns *and* the validated payload, and reading it back must reconstruct the
  model that was written, field for field.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from types import TracebackType
from typing import Protocol, runtime_checkable

from newsletter.models import (
    AssessmentRecord,
    DateWindow,
    NewsletterEdition,
    NormalizedArticle,
    RunManifest,
    SourceConfig,
    Submission,
    SubmissionStatus,
)
from newsletter.ranking.dedupe import PublishedKeys


class PersistenceError(Exception):
    """The database could not be opened, migrated or written."""


@dataclass(frozen=True)
class StoredAssessment:
    """One cached assessment together with everything needed to score it again.

    The three parts are exactly the inputs of
    :func:`~newsletter.ranking.scoring.compute_score`: the assessment supplies
    the four ratings, the source supplies the priority, and the article says
    which source that is. Handing them back joined is what lets an offline
    measurement re-run the production formula instead of an approximation of it.
    """

    article: NormalizedArticle
    record: AssessmentRecord
    source: SourceConfig


@runtime_checkable
class Storage(Protocol):
    """What the pipeline and the CLI require of a database.

    A :class:`Storage` is also an ``intelligence.analyzer.AssessmentCache``: the
    two assessment methods below are that protocol, so the same object can be
    handed to the analyzer as its cache.
    """

    # -- lifecycle ---------------------------------------------------------- #

    def connect(self) -> Storage:
        """Open the connection and ensure the schema exists. Idempotent."""
        ...

    def close(self) -> None:
        """Release the connection. Safe to call when already closed."""
        ...

    def __enter__(self) -> Storage: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...

    # -- sources ------------------------------------------------------------ #

    def upsert_source(self, source: SourceConfig, *, now: datetime | None = None) -> None:
        """Record the configured source, overwriting the previous definition."""
        ...

    # -- articles ----------------------------------------------------------- #

    def save_articles(self, articles: Iterable[NormalizedArticle]) -> int:
        """Store many articles; returns how many had not been seen before."""
        ...

    def articles_in_window(self, window: DateWindow) -> list[NormalizedArticle]:
        """Every stored article published inside ``window``, oldest first, then by id.

        Part of the contract rather than a read-back helper, because the running
        edition depends on it: a feed carries only its last handful of items, so
        without recall a window more than a few days old can never see the
        articles the engine already ingested for it.

        The window bound is the same half-open ``[start, end)`` test the hard
        date filter applies, so a backend may not widen or narrow it, and the
        order must be total so the merged candidate pool is stable (AC9).
        """
        ...

    # -- assessments (the analyzer cache) ----------------------------------- #

    def get_assessment(self, cache_key: str) -> AssessmentRecord | None:
        """A cached assessment by its exact identity, or None."""
        ...

    def save_assessment(self, record: AssessmentRecord, *, article_id: str | None = None) -> None:
        """Store an assessment under its cache key."""
        ...

    def stored_assessments(self, *, prompt_version: str | None = None) -> list[StoredAssessment]:
        """Every cached assessment joined to its article and its source.

        Part of the contract rather than a read-back helper, because calibration
        depends on it: the thresholds the edition is rationed by -- ``min_score``
        above all -- are measured against the assessments already in the cache,
        and a measurement that reached into one backend's tables would only ever
        describe that backend. ``prompt_version`` narrows the join to one rubric,
        which is the whole point when two are being compared.

        An assessment whose article or whose source is no longer stored is
        omitted, because it cannot be scored: the priority that completes the
        formula is not there to read. Order is total -- article id, then cache
        key -- so two runs over the same rows agree.
        """
        ...

    # -- editions ----------------------------------------------------------- #

    def save_edition(
        self,
        edition: NewsletterEdition,
        *,
        output_paths: Mapping[str, str] | None = None,
    ) -> None:
        """Persist an edition and replace its story list."""
        ...

    def published_identity_keys(self, *, exclude_edition_id: str | None = None) -> PublishedKeys:
        """Identity keys of every story a previous edition already printed (AC9)."""
        ...

    def latest_issue_label(self) -> str | None:
        """The issue label of the most recently generated edition, or None.

        "Most recent" is the edition's own ``generated_at`` -- the instant the run
        recorded when it wrote the artifacts -- and not a file timestamp, which a
        copy, a restore or a checkout rewrites. The web reader turns the label
        into the one artifact path it is allowed to open, so a backend must
        return a label exactly as it was stored, never a path.
        """
        ...

    # -- reader submissions ------------------------------------------------- #

    def save_submission(self, submission: Submission) -> bool:
        """Store a submission; returns True when it had not been seen before."""
        ...

    def get_submission(self, submission_id: str) -> Submission | None: ...

    def list_submissions(
        self, *, status: SubmissionStatus | None = None, limit: int = 100
    ) -> list[Submission]:
        """Submissions, oldest first, optionally filtered by status."""
        ...

    def pending_submissions(self, limit: int = 100) -> list[Submission]: ...

    # -- run history -------------------------------------------------------- #

    def save_run(self, manifest: RunManifest) -> None:
        """Persist the run manifest, overwriting an earlier write of the same run."""
        ...
