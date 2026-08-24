"""PostgreSQL persistence.

The same contract as ``persistence.sqlite``, against a data server rather than a
file, so the deployment target is a connection string instead of a filesystem
path. Behaviour is meant to be indistinguishable: same upsert semantics, same
ordering, same models in and out. Where the two engines genuinely differ, the
difference is handled here and named in a comment, because a storage backend
that is *almost* the same is worse than one that is obviously different.

The differences that mattered:

* **Placeholders** are ``%s``, not ``?``. Every statement was converted whole.
* **Payloads are ``JSONB``**, which makes them queryable, but they are read back
  with ``payload::text`` so the value is validated by exactly the same Pydantic
  entry point as SQLite uses. JSONB does not preserve key order or insignificant
  whitespace; re-validation does not care, and nothing else reads the column.
* **Timestamps are ``timestamptz``** rather than ISO strings. The session runs in
  UTC so a stored aware datetime comes back aware, at the same instant, with
  ``timezone.utc`` attached -- a naive datetime would be rejected downstream by
  ``DateWindow.contains``.
* **NULL ordering**: SQLite compares the ISO strings and puts a missing edition
  first because ``COALESCE(generated_at, '')`` sorts before any date. Postgres
  sorts NULLs last by default, so ``NULLS FIRST`` is spelled out; without it,
  ``published_identity_keys`` would attribute a story to the wrong issue.
* **Booleans** are real booleans, not ``0``/``1`` integers.

Known limitation, deliberately not hidden: PostgreSQL's JSON types reject the
NUL character (``\\u0000``) inside strings, while SQLite stores it happily. An
article whose scraped text contains a NUL byte fails the write here with an
explicit :class:`PersistenceError` rather than being silently rewritten.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from types import TracebackType
from typing import TYPE_CHECKING, Any

from newsletter.logging_setup import get_logger
from newsletter.models import (
    AssessmentRecord,
    DateWindow,
    IssueRef,
    NewsletterEdition,
    NormalizedArticle,
    RunManifest,
    SourceConfig,
    Submission,
    SubmissionStatus,
)
from newsletter.persistence.base import PersistenceError, Storage, StoredAssessment
from newsletter.persistence.sqlite import SCHEMA_VERSION
from newsletter.ranking.dedupe import PublishedKeys

if TYPE_CHECKING:  # pragma: no cover - typing only; the driver is an optional extra
    from psycopg import Connection

logger = get_logger("persistence.postgres")

#: One statement per element: psycopg speaks the extended query protocol, so a
#: multi-statement script is not portable the way ``executescript`` is.
SCHEMA: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS meta (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sources (
        id            TEXT PRIMARY KEY,
        name          TEXT NOT NULL,
        entrypoint    TEXT NOT NULL,
        strategy      TEXT NOT NULL,
        priority      INTEGER NOT NULL,
        category_hint TEXT NOT NULL,
        enabled       BOOLEAN NOT NULL,
        payload       JSONB NOT NULL,
        updated_at    TIMESTAMPTZ NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS articles (
        article_id    TEXT PRIMARY KEY,
        source_id     TEXT NOT NULL,
        canonical_url TEXT NOT NULL,
        title         TEXT NOT NULL,
        published_at  TIMESTAMPTZ NOT NULL,
        content_hash  TEXT NOT NULL,
        retrieved_at  TIMESTAMPTZ NOT NULL,
        first_seen_at TIMESTAMPTZ NOT NULL,
        payload       JSONB NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_articles_hash ON articles (content_hash)",
    "CREATE INDEX IF NOT EXISTS idx_articles_published ON articles (published_at)",
    "CREATE INDEX IF NOT EXISTS idx_articles_source ON articles (source_id)",
    """
    CREATE TABLE IF NOT EXISTS assessments (
        cache_key      TEXT PRIMARY KEY,
        article_id     TEXT,
        content_hash   TEXT NOT NULL,
        model          TEXT NOT NULL,
        prompt_version TEXT NOT NULL,
        schema_version TEXT NOT NULL,
        created_at     TIMESTAMPTZ NOT NULL,
        payload        JSONB NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_assessments_article ON assessments (article_id)",
    """
    CREATE TABLE IF NOT EXISTS newsletter_editions (
        edition_id   TEXT PRIMARY KEY,
        issue_label  TEXT NOT NULL,
        period_start TIMESTAMPTZ NOT NULL,
        period_end   TIMESTAMPTZ NOT NULL,
        generated_at TIMESTAMPTZ NOT NULL,
        output_paths JSONB NOT NULL,
        payload      JSONB NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS edition_items (
        edition_id TEXT NOT NULL,
        article_id TEXT NOT NULL,
        position   INTEGER NOT NULL,
        section    TEXT NOT NULL,
        is_lead    BOOLEAN NOT NULL DEFAULT FALSE,
        PRIMARY KEY (edition_id, article_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS submissions (
        submission_id TEXT PRIMARY KEY,
        url           TEXT NOT NULL,
        submitted_by  TEXT,
        submitted_at  TIMESTAMPTZ NOT NULL,
        status        TEXT NOT NULL,
        reason        TEXT,
        article_id    TEXT,
        decided_at    TIMESTAMPTZ,
        payload       JSONB NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_submissions_status ON submissions (status)",
    """
    CREATE TABLE IF NOT EXISTS run_history (
        run_id      TEXT PRIMARY KEY,
        started_at  TIMESTAMPTZ NOT NULL,
        finished_at TIMESTAMPTZ,
        payload     JSONB NOT NULL
    )
    """,
)


class PostgresStorage:
    """The storage contract against one PostgreSQL database.

    Usable as a context manager::

        with PostgresStorage("postgresql://user:pw@host/newsletter") as db:
            db.save_article(article)

    The driver is an optional extra (``pip install .[postgres]``) and is imported
    inside :meth:`connect`, so importing ``newsletter`` never requires it.
    """

    def __init__(self, dsn: str, *, read_only: bool = False) -> None:
        self.dsn = dsn
        #: The session refuses writes, and the schema is neither created nor
        #: stamped. The server enforces it, so an offline measurement cannot
        #: alter the database it is measuring even by accident.
        self.read_only = read_only
        self._connection: Connection[Any] | None = None

    # -- lifecycle ---------------------------------------------------------- #

    def connect(self) -> PostgresStorage:
        if self._connection is not None:
            return self
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:  # pragma: no cover - depends on the installation
            raise PersistenceError(
                "PostgreSQL support needs the psycopg driver; install it with "
                "`pip install 'weekly-intelligence-newspaper[postgres]'` "
                "or point NEWSLETTER_DATABASE_URL at a sqlite:/// URL"
            ) from exc

        try:
            # autocommit mirrors SQLite's commit-per-statement; the one multi
            # statement operation opens an explicit transaction of its own.
            connection = psycopg.connect(self.dsn, autocommit=True, row_factory=dict_row)
            # Aware in, aware out, at the same instant: with the session in UTC a
            # timestamptz comes back carrying `timezone.utc` rather than whatever
            # the server happens to be configured for.
            connection.execute("SET TIME ZONE 'UTC'")
            if self.read_only:
                connection.execute("SET default_transaction_read_only = on")
        except Exception as exc:  # psycopg.Error and DNS/socket failures alike
            raise PersistenceError(f"cannot open database {_safe(self.dsn)}: {exc}") from exc

        self._connection = connection
        if not self.read_only:
            self.initialize()
        return self

    def initialize(self) -> None:
        """Create the schema if needed and record the schema version."""
        connection = self._require_connection()
        try:
            with connection.transaction():
                for statement in SCHEMA:
                    connection.execute(statement)
                connection.execute(
                    "INSERT INTO meta (key, value) VALUES ('schema_version', %s) "
                    "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                    (str(SCHEMA_VERSION),),
                )
        except Exception as exc:
            raise PersistenceError(f"cannot initialize schema: {exc}") from exc

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def __enter__(self) -> PostgresStorage:
        return self.connect()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def _require_connection(self) -> Connection[Any]:
        if self._connection is None:
            raise PersistenceError("database is not connected; call connect() first")
        return self._connection

    def _execute(self, statement: str, parameters: Sequence[Any] = ()) -> Any:
        """Run one statement, turning driver failures into :class:`PersistenceError`.

        A JSONB column rejects a NUL character inside a string where SQLite would
        store it; that is the one write this converts from an obscure driver
        error into an explicit persistence failure.
        """
        try:
            return self._require_connection().execute(statement, parameters)
        except PersistenceError:
            raise
        except Exception as exc:
            raise PersistenceError(f"query failed: {exc}") from exc

    def _fetchone(self, statement: str, parameters: Sequence[Any] = ()) -> dict[str, Any] | None:
        return self._execute(statement, parameters).fetchone()

    def _fetchall(self, statement: str, parameters: Sequence[Any] = ()) -> list[dict[str, Any]]:
        return self._execute(statement, parameters).fetchall()

    @property
    def schema_version(self) -> int:
        row = self._fetchone("SELECT value FROM meta WHERE key = 'schema_version'")
        return int(row["value"]) if row else 0

    # -- sources ------------------------------------------------------------ #

    def upsert_source(self, source: SourceConfig, *, now: datetime | None = None) -> None:
        self._execute(
            """
            INSERT INTO sources
                (id, name, entrypoint, strategy, priority, category_hint, enabled, payload, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s)
            ON CONFLICT (id) DO UPDATE SET
                name = excluded.name,
                entrypoint = excluded.entrypoint,
                strategy = excluded.strategy,
                priority = excluded.priority,
                category_hint = excluded.category_hint,
                enabled = excluded.enabled,
                payload = excluded.payload,
                updated_at = excluded.updated_at
            """,
            (
                source.id,
                source.name,
                source.entrypoint,
                source.strategy.value,
                source.priority,
                source.category_hint.value,
                source.enabled,
                source.model_dump_json(),
                _stamp(now),
            ),
        )

    def get_source(self, source_id: str) -> SourceConfig | None:
        row = self._fetchone(
            "SELECT payload::text AS payload FROM sources WHERE id = %s", (source_id,)
        )
        return SourceConfig.model_validate_json(row["payload"]) if row else None

    def list_sources(self) -> list[SourceConfig]:
        rows = self._fetchall(
            "SELECT payload::text AS payload FROM sources ORDER BY priority DESC, id"
        )
        return [SourceConfig.model_validate_json(row["payload"]) for row in rows]

    # -- articles ----------------------------------------------------------- #

    def save_article(self, article: NormalizedArticle, *, now: datetime | None = None) -> bool:
        """Store an article. Returns True when it had not been seen before.

        ``first_seen_at`` is absent from the update clause on purpose: the first
        sighting is a fact about history, so a re-fetch refreshes everything else
        and leaves it alone.
        """
        existed = self.get_article(article.article_id) is not None
        self._execute(
            """
            INSERT INTO articles
                (article_id, source_id, canonical_url, title, published_at,
                 content_hash, retrieved_at, first_seen_at, payload)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
            ON CONFLICT (article_id) DO UPDATE SET
                canonical_url = excluded.canonical_url,
                title = excluded.title,
                published_at = excluded.published_at,
                content_hash = excluded.content_hash,
                retrieved_at = excluded.retrieved_at,
                payload = excluded.payload
            """,
            (
                article.article_id,
                article.source_id,
                article.canonical_url,
                article.title,
                article.published_at,
                article.content_hash,
                article.retrieved_at,
                _stamp(now),
                article.model_dump_json(),
            ),
        )
        return not existed

    def save_articles(self, articles: Iterable[NormalizedArticle]) -> int:
        """Store many articles; returns how many were new."""
        return sum(int(self.save_article(article)) for article in articles)

    def get_article(self, article_id: str) -> NormalizedArticle | None:
        row = self._fetchone(
            "SELECT payload::text AS payload FROM articles WHERE article_id = %s", (article_id,)
        )
        return NormalizedArticle.model_validate_json(row["payload"]) if row else None

    def find_article_by_hash(self, content_hash: str) -> NormalizedArticle | None:
        row = self._fetchone(
            "SELECT payload::text AS payload FROM articles WHERE content_hash = %s "
            "ORDER BY first_seen_at LIMIT 1",
            (content_hash,),
        )
        return NormalizedArticle.model_validate_json(row["payload"]) if row else None

    def articles_in_window(self, window: DateWindow) -> list[NormalizedArticle]:
        """Every stored article published inside ``window``, oldest first, then by id.

        ``published_at`` is a real ``timestamptz`` here, so the half-open window
        is expressed directly in SQL and the index does the work -- no widened
        bound and no second pass, which is the one place this backend is simpler
        than SQLite rather than merely different.
        """
        rows = self._fetchall(
            "SELECT payload::text AS payload FROM articles "
            "WHERE published_at >= %s AND published_at < %s "
            "ORDER BY published_at, article_id",
            (window.start, window.end),
        )
        return [NormalizedArticle.model_validate_json(row["payload"]) for row in rows]

    def count_articles(self) -> int:
        row = self._fetchone("SELECT COUNT(*) AS n FROM articles")
        return int(row["n"]) if row else 0

    # -- assessments (the Stage 4 cache) ------------------------------------ #

    def save_assessment(self, record: AssessmentRecord, *, article_id: str | None = None) -> None:
        self._execute(
            """
            INSERT INTO assessments
                (cache_key, article_id, content_hash, model, prompt_version,
                 schema_version, created_at, payload)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb)
            ON CONFLICT (cache_key) DO UPDATE SET
                article_id = excluded.article_id,
                payload = excluded.payload,
                created_at = excluded.created_at
            """,
            (
                record.key,
                article_id,
                record.content_hash,
                record.model,
                record.prompt_version,
                record.schema_version,
                record.created_at,
                record.model_dump_json(),
            ),
        )

    def get_assessment(self, cache_key: str) -> AssessmentRecord | None:
        """Look up a cached assessment by its exact identity.

        The key includes content hash, prompt version, schema version and model,
        so changing any of them is automatically a cache miss.
        """
        row = self._fetchone(
            "SELECT payload::text AS payload FROM assessments WHERE cache_key = %s", (cache_key,)
        )
        return AssessmentRecord.model_validate_json(row["payload"]) if row else None

    def stored_assessments(self, *, prompt_version: str | None = None) -> list[StoredAssessment]:
        """Assessments joined to their article and source. See :class:`Storage`."""
        statement = (
            "SELECT s.payload::text AS source_payload, r.payload::text AS article_payload, "
            "       a.payload::text AS record_payload "
            "FROM assessments a "
            "JOIN articles r ON r.article_id = a.article_id "
            "JOIN sources s ON s.id = r.source_id "
        )
        parameters: tuple[str, ...] = ()
        if prompt_version is not None:
            statement += "WHERE a.prompt_version = %s "
            parameters = (prompt_version,)
        statement += "ORDER BY a.article_id, a.cache_key"

        return [
            StoredAssessment(
                article=NormalizedArticle.model_validate_json(row["article_payload"]),
                record=AssessmentRecord.model_validate_json(row["record_payload"]),
                source=SourceConfig.model_validate_json(row["source_payload"]),
            )
            for row in self._fetchall(statement, parameters)
        ]

    # -- editions ----------------------------------------------------------- #

    def save_edition(
        self,
        edition: NewsletterEdition,
        *,
        output_paths: Mapping[str, str] | None = None,
    ) -> None:
        """Persist an edition and its story list, preserving traceability."""
        connection = self._require_connection()
        rows = [
            (
                edition.edition_id,
                item.article_id,
                position,
                item.category.value,
                item.article_id == edition.lead_story.article_id,
            )
            for position, item in enumerate(edition.all_items())
        ]
        try:
            # SQLite commits the three statements together; so does this.
            with connection.transaction():
                connection.execute(
                    """
                    INSERT INTO newsletter_editions
                        (edition_id, issue_label, period_start, period_end, generated_at,
                         output_paths, payload)
                    VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s::jsonb)
                    ON CONFLICT (edition_id) DO UPDATE SET
                        issue_label = excluded.issue_label,
                        period_start = excluded.period_start,
                        period_end = excluded.period_end,
                        generated_at = excluded.generated_at,
                        output_paths = excluded.output_paths,
                        payload = excluded.payload
                    """,
                    (
                        edition.edition_id,
                        edition.issue_label,
                        edition.period_start,
                        edition.period_end,
                        edition.generated_at,
                        json.dumps(dict(output_paths or {}), sort_keys=True),
                        edition.model_dump_json(),
                    ),
                )
                connection.execute(
                    "DELETE FROM edition_items WHERE edition_id = %s", (edition.edition_id,)
                )
                if rows:
                    connection.cursor().executemany(
                        "INSERT INTO edition_items "
                        "(edition_id, article_id, position, section, is_lead) "
                        "VALUES (%s, %s, %s, %s, %s)",
                        rows,
                    )
        except Exception as exc:
            raise PersistenceError(f"cannot save edition {edition.edition_id}: {exc}") from exc

    def get_edition(self, edition_id: str) -> NewsletterEdition | None:
        row = self._fetchone(
            "SELECT payload::text AS payload FROM newsletter_editions WHERE edition_id = %s",
            (edition_id,),
        )
        return NewsletterEdition.model_validate_json(row["payload"]) if row else None

    def latest_issue_label(self) -> str | None:
        """The issue label of the most recently generated edition, or None.

        ``edition_id`` breaks a tie so two editions written in the same instant
        still resolve to one answer rather than to whichever row the planner
        happened to return.
        """
        row = self._fetchone(
            "SELECT issue_label FROM newsletter_editions "
            "ORDER BY generated_at DESC, edition_id DESC LIMIT 1"
        )
        return str(row["issue_label"]) if row else None

    def generated_issues(self) -> list[IssueRef]:
        """Every issue an edition was generated for, one row per label.

        Semantically identical to the SQLite query. ``MIN(period_start)`` is
        repeated in the ``ORDER BY`` rather than referenced by its output name,
        which is ambiguous here with the input column of the same name.
        """
        rows = self._fetchall(
            "SELECT issue_label, MIN(period_start) AS period_start "
            "FROM newsletter_editions GROUP BY issue_label "
            "ORDER BY MIN(period_start), issue_label"
        )
        return [
            IssueRef(issue_label=str(row["issue_label"]), period_start=row["period_start"])
            for row in rows
        ]

    def get_edition_article_ids(self, edition_id: str) -> list[str]:
        rows = self._fetchall(
            "SELECT article_id FROM edition_items WHERE edition_id = %s ORDER BY position",
            (edition_id,),
        )
        return [row["article_id"] for row in rows]

    def published_identity_keys(self, *, exclude_edition_id: str | None = None) -> PublishedKeys:
        """Identity keys of every story a previous edition already printed.

        Semantically identical to the SQLite query, including both outer joins
        and the "oldest issue wins the label" ordering. One spelling had to
        change: SQLite orders by ``COALESCE(generated_at, '')``, which puts a
        missing edition row first because the empty string sorts before any ISO
        date. ``generated_at`` is a real timestamp here, so the same intent is
        written as ``NULLS FIRST`` -- Postgres would otherwise sort NULLs last
        and could credit a story to the wrong issue.
        """
        statement = [
            "SELECT i.article_id AS article_id,",
            "       a.content_hash AS content_hash,",
            "       COALESCE(e.issue_label, i.edition_id) AS issue_label",
            "FROM edition_items i",
            "LEFT JOIN articles a ON a.article_id = i.article_id",
            "LEFT JOIN newsletter_editions e ON e.edition_id = i.edition_id",
        ]
        parameters: tuple[str, ...] = ()
        if exclude_edition_id is not None:
            statement.append("WHERE i.edition_id <> %s")
            parameters = (exclude_edition_id,)
        statement.append("ORDER BY e.generated_at NULLS FIRST, i.edition_id, i.position")

        rows = self._fetchall("\n".join(statement), parameters)

        by_article_id: dict[str, str] = {}
        by_content_hash: dict[str, str] = {}
        for row in rows:
            issue = row["issue_label"]
            by_article_id.setdefault(row["article_id"], issue)
            if row["content_hash"]:
                by_content_hash.setdefault(row["content_hash"], issue)

        return PublishedKeys(by_article_id=by_article_id, by_content_hash=by_content_hash)

    # -- reader submissions ------------------------------------------------- #

    def save_submission(self, submission: Submission) -> bool:
        """Store a submission. Returns True when it had not been seen before.

        Resubmitting the same URL updates the existing record rather than
        creating a second one, because the id is derived from the URL.
        """
        existed = self.get_submission(submission.submission_id) is not None
        self._execute(
            """
            INSERT INTO submissions
                (submission_id, url, submitted_by, submitted_at, status, reason,
                 article_id, decided_at, payload)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
            ON CONFLICT (submission_id) DO UPDATE SET
                status = excluded.status,
                reason = excluded.reason,
                article_id = excluded.article_id,
                decided_at = excluded.decided_at,
                payload = excluded.payload
            """,
            (
                submission.submission_id,
                submission.url,
                submission.submitted_by,
                submission.submitted_at,
                submission.status.value,
                submission.reason,
                submission.article_id,
                submission.decided_at,
                submission.model_dump_json(),
            ),
        )
        return not existed

    def get_submission(self, submission_id: str) -> Submission | None:
        row = self._fetchone(
            "SELECT payload::text AS payload FROM submissions WHERE submission_id = %s",
            (submission_id,),
        )
        return Submission.model_validate_json(row["payload"]) if row else None

    def list_submissions(
        self, *, status: SubmissionStatus | None = None, limit: int = 100
    ) -> list[Submission]:
        """Submissions, oldest first, optionally filtered by status."""
        if status is None:
            rows = self._fetchall(
                "SELECT payload::text AS payload FROM submissions ORDER BY submitted_at LIMIT %s",
                (limit,),
            )
        else:
            rows = self._fetchall(
                "SELECT payload::text AS payload FROM submissions WHERE status = %s "
                "ORDER BY submitted_at LIMIT %s",
                (status.value, limit),
            )
        return [Submission.model_validate_json(row["payload"]) for row in rows]

    def pending_submissions(self, limit: int = 100) -> list[Submission]:
        return self.list_submissions(status=SubmissionStatus.PENDING, limit=limit)

    # -- run history -------------------------------------------------------- #

    def save_run(self, manifest: RunManifest) -> None:
        self._execute(
            """
            INSERT INTO run_history (run_id, started_at, finished_at, payload)
            VALUES (%s, %s, %s, %s::jsonb)
            ON CONFLICT (run_id) DO UPDATE SET
                finished_at = excluded.finished_at,
                payload = excluded.payload
            """,
            (
                manifest.run_id,
                manifest.started_at,
                manifest.finished_at,
                manifest.model_dump_json(),
            ),
        )

    def get_run(self, run_id: str) -> RunManifest | None:
        row = self._fetchone(
            "SELECT payload::text AS payload FROM run_history WHERE run_id = %s", (run_id,)
        )
        return RunManifest.model_validate_json(row["payload"]) if row else None

    def recent_runs(self, limit: int = 10) -> list[RunManifest]:
        rows = self._fetchall(
            "SELECT payload::text AS payload FROM run_history ORDER BY started_at DESC LIMIT %s",
            (limit,),
        )
        return [RunManifest.model_validate_json(row["payload"]) for row in rows]


def _stamp(now: datetime | None) -> datetime:
    return now or datetime.now(UTC)


def _safe(dsn: str) -> str:
    from newsletter.persistence.dsn import redact_dsn

    return redact_dsn(dsn)


if TYPE_CHECKING:  # pragma: no cover - a type-checker assertion, not runtime code
    _conforms: type[Storage] = PostgresStorage
