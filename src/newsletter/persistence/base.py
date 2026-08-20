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
from datetime import datetime
from types import TracebackType
from typing import Protocol, runtime_checkable

from newsletter.models import (
    AssessmentRecord,
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

    # -- assessments (the analyzer cache) ----------------------------------- #

    def get_assessment(self, cache_key: str) -> AssessmentRecord | None:
        """A cached assessment by its exact identity, or None."""
        ...

    def save_assessment(self, record: AssessmentRecord, *, article_id: str | None = None) -> None:
        """Store an assessment under its cache key."""
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
