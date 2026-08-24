"""SQLite persistence.

Deliberately thin: `sqlite3` plus Pydantic JSON, no ORM. It exists for four
reasons named in the PRD -- avoid duplicate work, cache model assessments,
preserve auditability, and make failures inspectable.

Each table keeps queryable columns *and* the full validated payload as JSON, so
a record round-trips into exactly the model it came from while remaining
greppable with plain SQL.

Traceability (AC3) is a join: ``edition_items -> articles -> sources`` and
``articles -> assessments``.
"""

from __future__ import annotations

import json
import sqlite3
import urllib.parse
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING

from newsletter.logging_setup import get_logger
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
from newsletter.persistence.base import PersistenceError, Storage, StoredAssessment

# The identity keys a published story is remembered by are the deduplication
# keys, so they are defined once, in ranking.dedupe, and read here. The
# dependency runs one way only: ranking knows nothing about storage.
from newsletter.ranking.dedupe import PublishedKeys

logger = get_logger("persistence")

SCHEMA_VERSION = 2

#: How far the stored-article window query reaches past the window itself. Stored
#: timestamps carry their own UTC offset, which spans at most +/-14 hours, so a
#: day is comfortably more than a string comparison can be wrong by -- and the
#: exact bound is applied in Python afterwards.
_WINDOW_SLACK = timedelta(days=1)

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sources (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    entrypoint    TEXT NOT NULL,
    strategy      TEXT NOT NULL,
    priority      INTEGER NOT NULL,
    category_hint TEXT NOT NULL,
    enabled       INTEGER NOT NULL,
    payload       TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS articles (
    article_id    TEXT PRIMARY KEY,
    source_id     TEXT NOT NULL,
    canonical_url TEXT NOT NULL,
    title         TEXT NOT NULL,
    published_at  TEXT NOT NULL,
    content_hash  TEXT NOT NULL,
    retrieved_at  TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    payload       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_articles_hash ON articles (content_hash);
CREATE INDEX IF NOT EXISTS idx_articles_published ON articles (published_at);
CREATE INDEX IF NOT EXISTS idx_articles_source ON articles (source_id);

CREATE TABLE IF NOT EXISTS assessments (
    cache_key      TEXT PRIMARY KEY,
    article_id     TEXT,
    content_hash   TEXT NOT NULL,
    model          TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    payload        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_assessments_article ON assessments (article_id);

CREATE TABLE IF NOT EXISTS newsletter_editions (
    edition_id   TEXT PRIMARY KEY,
    issue_label  TEXT NOT NULL,
    period_start TEXT NOT NULL,
    period_end   TEXT NOT NULL,
    generated_at TEXT NOT NULL,
    output_paths TEXT NOT NULL,
    payload      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS edition_items (
    edition_id TEXT NOT NULL,
    article_id TEXT NOT NULL,
    position   INTEGER NOT NULL,
    section    TEXT NOT NULL,
    is_lead    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (edition_id, article_id)
);

CREATE TABLE IF NOT EXISTS submissions (
    submission_id TEXT PRIMARY KEY,
    url           TEXT NOT NULL,
    submitted_by  TEXT,
    submitted_at  TEXT NOT NULL,
    status        TEXT NOT NULL,
    reason        TEXT,
    article_id    TEXT,
    decided_at    TEXT,
    payload       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_submissions_status ON submissions (status);

CREATE TABLE IF NOT EXISTS run_history (
    run_id      TEXT PRIMARY KEY,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    payload     TEXT NOT NULL
);
"""


#: ``PersistenceError`` now lives in ``persistence.base`` so every backend raises
#: the same class; it stays importable from here, where callers already expect it.
__all__ = ["SCHEMA", "SCHEMA_VERSION", "Database", "PersistenceError"]


class Database:
    """Thin synchronous wrapper around one SQLite file.

    Usable as a context manager::

        with Database(path) as db:
            db.save_article(article)
    """

    def __init__(self, path: Path | str = ":memory:", *, read_only: bool = False) -> None:
        self.path = str(path)
        #: Opened through SQLite's ``mode=ro`` URI, which refuses every write at
        #: the engine rather than by convention. An offline measurement can then
        #: point at the live database without creating a journal beside it or
        #: rewriting the schema row -- the file it reports on is the file as it
        #: was found. ``:memory:`` has nothing to protect and ignores it.
        self.read_only = read_only and self.path != ":memory:"
        self._connection: sqlite3.Connection | None = None

    # -- lifecycle ---------------------------------------------------------- #

    def connect(self) -> Database:
        if self._connection is not None:
            return self
        if self.path != ":memory:" and not self.read_only:
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        try:
            if self.read_only:
                if not Path(self.path).is_file():
                    raise PersistenceError(f"cannot open database {self.path}: no such file")
                connection = sqlite3.connect(_read_only_uri(self.path), uri=True)
            else:
                connection = sqlite3.connect(self.path)
        except sqlite3.Error as exc:  # pragma: no cover - filesystem dependent
            raise PersistenceError(f"cannot open database {self.path}: {exc}") from exc
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        if not self.read_only:
            # WAL is a write; a read-only handle must not take one.
            connection.execute("PRAGMA journal_mode = WAL")
        self._connection = connection
        if not self.read_only:
            self.initialize()
        return self

    def initialize(self) -> None:
        """Create the schema if needed and record the schema version."""
        connection = self._require_connection()
        try:
            connection.executescript(SCHEMA)
            connection.execute(
                "INSERT INTO meta (key, value) VALUES ('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (str(SCHEMA_VERSION),),
            )
            connection.commit()
        except sqlite3.Error as exc:
            raise PersistenceError(f"cannot initialize schema: {exc}") from exc

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def __enter__(self) -> Database:
        return self.connect()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def _require_connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise PersistenceError("database is not connected; call connect() first")
        return self._connection

    @property
    def schema_version(self) -> int:
        row = (
            self._require_connection()
            .execute("SELECT value FROM meta WHERE key = 'schema_version'")
            .fetchone()
        )
        return int(row["value"]) if row else 0

    # -- sources ------------------------------------------------------------ #

    def upsert_source(self, source: SourceConfig, *, now: datetime | None = None) -> None:
        connection = self._require_connection()
        connection.execute(
            """
            INSERT INTO sources
                (id, name, entrypoint, strategy, priority, category_hint, enabled, payload, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
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
                int(source.enabled),
                source.model_dump_json(),
                _stamp(now),
            ),
        )
        connection.commit()

    def get_source(self, source_id: str) -> SourceConfig | None:
        row = (
            self._require_connection()
            .execute("SELECT payload FROM sources WHERE id = ?", (source_id,))
            .fetchone()
        )
        return SourceConfig.model_validate_json(row["payload"]) if row else None

    def list_sources(self) -> list[SourceConfig]:
        rows = (
            self._require_connection()
            .execute("SELECT payload FROM sources ORDER BY priority DESC, id")
            .fetchall()
        )
        return [SourceConfig.model_validate_json(row["payload"]) for row in rows]

    # -- articles ----------------------------------------------------------- #

    def save_article(self, article: NormalizedArticle, *, now: datetime | None = None) -> bool:
        """Store an article. Returns True when it had not been seen before."""
        connection = self._require_connection()
        existed = self.get_article(article.article_id) is not None
        connection.execute(
            """
            INSERT INTO articles
                (article_id, source_id, canonical_url, title, published_at,
                 content_hash, retrieved_at, first_seen_at, payload)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(article_id) DO UPDATE SET
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
                article.published_at.isoformat(),
                article.content_hash,
                article.retrieved_at.isoformat(),
                _stamp(now),
                article.model_dump_json(),
            ),
        )
        connection.commit()
        return not existed

    def save_articles(self, articles: Iterable[NormalizedArticle]) -> int:
        """Store many articles; returns how many were new."""
        return sum(int(self.save_article(article)) for article in articles)

    def get_article(self, article_id: str) -> NormalizedArticle | None:
        row = (
            self._require_connection()
            .execute("SELECT payload FROM articles WHERE article_id = ?", (article_id,))
            .fetchone()
        )
        return NormalizedArticle.model_validate_json(row["payload"]) if row else None

    def find_article_by_hash(self, content_hash: str) -> NormalizedArticle | None:
        row = (
            self._require_connection()
            .execute(
                "SELECT payload FROM articles WHERE content_hash = ? ORDER BY first_seen_at LIMIT 1",
                (content_hash,),
            )
            .fetchone()
        )
        return NormalizedArticle.model_validate_json(row["payload"]) if row else None

    def articles_in_window(self, window: DateWindow) -> list[NormalizedArticle]:
        """Every stored article published inside ``window``, oldest first, then by id.

        ``published_at`` is stored as an ISO string carrying *its own* UTC offset,
        so comparing strings is only an approximation of comparing instants: the
        same moment written ``+00:00`` and ``-03:00`` sorts three hours apart. The
        SQL bound is therefore widened by a day -- more than any real offset, and
        enough to keep the index useful on a growing table -- and the authoritative
        test is :meth:`DateWindow.contains`, the same one the hard date filter
        applies, run here in Python over the widened set.
        """
        low = (window.start - _WINDOW_SLACK).astimezone(UTC).isoformat()
        high = (window.end + _WINDOW_SLACK).astimezone(UTC).isoformat()
        rows = (
            self._require_connection()
            .execute(
                "SELECT payload FROM articles WHERE published_at >= ? AND published_at <= ? "
                "ORDER BY published_at, article_id",
                (low, high),
            )
            .fetchall()
        )
        articles = [NormalizedArticle.model_validate_json(row["payload"]) for row in rows]
        inside = [article for article in articles if window.contains(article.published_at)]
        # Re-sorted on the parsed instants: the SQL order is over strings whose
        # offsets differ, which is close but not the total order the pool needs.
        inside.sort(key=lambda article: (article.published_at, article.article_id))
        return inside

    def count_articles(self) -> int:
        return int(
            self._require_connection().execute("SELECT COUNT(*) AS n FROM articles").fetchone()["n"]
        )

    # -- assessments (the Stage 4 cache) ------------------------------------ #

    def save_assessment(self, record: AssessmentRecord, *, article_id: str | None = None) -> None:
        connection = self._require_connection()
        connection.execute(
            """
            INSERT INTO assessments
                (cache_key, article_id, content_hash, model, prompt_version,
                 schema_version, created_at, payload)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(cache_key) DO UPDATE SET
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
                record.created_at.isoformat(),
                record.model_dump_json(),
            ),
        )
        connection.commit()

    def get_assessment(self, cache_key: str) -> AssessmentRecord | None:
        """Look up a cached assessment by its exact identity.

        The key includes content hash, prompt version, schema version and model,
        so changing any of them is automatically a cache miss.
        """
        row = (
            self._require_connection()
            .execute("SELECT payload FROM assessments WHERE cache_key = ?", (cache_key,))
            .fetchone()
        )
        return AssessmentRecord.model_validate_json(row["payload"]) if row else None

    def stored_assessments(self, *, prompt_version: str | None = None) -> list[StoredAssessment]:
        """Assessments joined to their article and source. See :class:`Storage`.

        The join is inner on purpose: a row without an article or without a
        source cannot be scored, so it is left out rather than guessed at.
        """
        statement = (
            "SELECT s.payload AS source_payload, r.payload AS article_payload, "
            "       a.payload AS record_payload "
            "FROM assessments a "
            "JOIN articles r ON r.article_id = a.article_id "
            "JOIN sources s ON s.id = r.source_id "
        )
        parameters: tuple[str, ...] = ()
        if prompt_version is not None:
            statement += "WHERE a.prompt_version = ? "
            parameters = (prompt_version,)
        statement += "ORDER BY a.article_id, a.cache_key"

        rows = self._require_connection().execute(statement, parameters).fetchall()
        return [
            StoredAssessment(
                article=NormalizedArticle.model_validate_json(row["article_payload"]),
                record=AssessmentRecord.model_validate_json(row["record_payload"]),
                source=SourceConfig.model_validate_json(row["source_payload"]),
            )
            for row in rows
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
        connection.execute(
            """
            INSERT INTO newsletter_editions
                (edition_id, issue_label, period_start, period_end, generated_at,
                 output_paths, payload)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(edition_id) DO UPDATE SET
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
                edition.period_start.isoformat(),
                edition.period_end.isoformat(),
                edition.generated_at.isoformat(),
                json.dumps(dict(output_paths or {}), sort_keys=True),
                edition.model_dump_json(),
            ),
        )
        connection.execute("DELETE FROM edition_items WHERE edition_id = ?", (edition.edition_id,))
        rows = [
            (
                edition.edition_id,
                item.article_id,
                position,
                item.category.value,
                int(item.article_id == edition.lead_story.article_id),
            )
            for position, item in enumerate(edition.all_items())
        ]
        connection.executemany(
            "INSERT INTO edition_items (edition_id, article_id, position, section, is_lead) "
            "VALUES (?, ?, ?, ?, ?)",
            rows,
        )
        connection.commit()

    def get_edition(self, edition_id: str) -> NewsletterEdition | None:
        row = (
            self._require_connection()
            .execute("SELECT payload FROM newsletter_editions WHERE edition_id = ?", (edition_id,))
            .fetchone()
        )
        return NewsletterEdition.model_validate_json(row["payload"]) if row else None

    def latest_issue_label(self) -> str | None:
        """The issue label of the most recently generated edition, or None.

        ``edition_id`` breaks a tie so two editions written in the same instant
        still resolve to one answer rather than to whichever row the planner
        happened to return.
        """
        row = (
            self._require_connection()
            .execute(
                "SELECT issue_label FROM newsletter_editions "
                "ORDER BY generated_at DESC, edition_id DESC LIMIT 1"
            )
            .fetchone()
        )
        return str(row["issue_label"]) if row else None

    def get_edition_article_ids(self, edition_id: str) -> list[str]:
        rows = (
            self._require_connection()
            .execute(
                "SELECT article_id FROM edition_items WHERE edition_id = ? ORDER BY position",
                (edition_id,),
            )
            .fetchall()
        )
        return [row["article_id"] for row in rows]

    def published_identity_keys(self, *, exclude_edition_id: str | None = None) -> PublishedKeys:
        """Identity keys of every story a previous edition already printed.

        The join is ``edition_items -> articles`` for the identity and
        ``edition_items -> newsletter_editions`` for the issue that printed it, so
        a suppressed story can name the edition that already carried it. Both
        joins are outer joins on purpose: a story stays suppressed by article id
        even if its article row was later purged, and by its edition id even if
        the edition record is gone.

        ``exclude_edition_id`` leaves one issue out. The pipeline passes the issue
        it is currently producing, so re-running the same week reproduces that
        edition instead of suppressing everything it just published (AC9).

        A database with no editions yet returns empty mappings, which suppress
        nothing. No migration is needed: the query reads only tables the current
        schema already creates.

        The title is deliberately not read: a headline is evidence inside one run
        and a guess across editions, and a guess must not carry a permanent
        consequence. See :class:`PublishedKeys`.
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
            statement.append("WHERE i.edition_id <> ?")
            parameters = (exclude_edition_id,)
        # Oldest issue first, so the label recorded below is the edition that
        # printed the story *first*, and the result never depends on row order.
        statement.append("ORDER BY COALESCE(e.generated_at, ''), i.edition_id, i.position")

        rows = self._require_connection().execute("\n".join(statement), parameters).fetchall()

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
        connection = self._require_connection()
        existed = self.get_submission(submission.submission_id) is not None
        connection.execute(
            """
            INSERT INTO submissions
                (submission_id, url, submitted_by, submitted_at, status, reason,
                 article_id, decided_at, payload)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(submission_id) DO UPDATE SET
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
                submission.submitted_at.isoformat(),
                submission.status.value,
                submission.reason,
                submission.article_id,
                submission.decided_at.isoformat() if submission.decided_at else None,
                submission.model_dump_json(),
            ),
        )
        connection.commit()
        return not existed

    def get_submission(self, submission_id: str) -> Submission | None:
        row = (
            self._require_connection()
            .execute("SELECT payload FROM submissions WHERE submission_id = ?", (submission_id,))
            .fetchone()
        )
        return Submission.model_validate_json(row["payload"]) if row else None

    def list_submissions(
        self, *, status: SubmissionStatus | None = None, limit: int = 100
    ) -> list[Submission]:
        """Submissions, oldest first, optionally filtered by status."""
        connection = self._require_connection()
        if status is None:
            rows = connection.execute(
                "SELECT payload FROM submissions ORDER BY submitted_at LIMIT ?", (limit,)
            ).fetchall()
        else:
            rows = connection.execute(
                "SELECT payload FROM submissions WHERE status = ? ORDER BY submitted_at LIMIT ?",
                (status.value, limit),
            ).fetchall()
        return [Submission.model_validate_json(row["payload"]) for row in rows]

    def pending_submissions(self, limit: int = 100) -> list[Submission]:
        return self.list_submissions(status=SubmissionStatus.PENDING, limit=limit)

    # -- run history -------------------------------------------------------- #

    def save_run(self, manifest: RunManifest) -> None:
        connection = self._require_connection()
        connection.execute(
            """
            INSERT INTO run_history (run_id, started_at, finished_at, payload)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(run_id) DO UPDATE SET
                finished_at = excluded.finished_at,
                payload = excluded.payload
            """,
            (
                manifest.run_id,
                manifest.started_at.isoformat(),
                manifest.finished_at.isoformat() if manifest.finished_at else None,
                manifest.model_dump_json(),
            ),
        )
        connection.commit()

    def get_run(self, run_id: str) -> RunManifest | None:
        row = (
            self._require_connection()
            .execute("SELECT payload FROM run_history WHERE run_id = ?", (run_id,))
            .fetchone()
        )
        return RunManifest.model_validate_json(row["payload"]) if row else None

    def recent_runs(self, limit: int = 10) -> list[RunManifest]:
        rows = (
            self._require_connection()
            .execute("SELECT payload FROM run_history ORDER BY started_at DESC LIMIT ?", (limit,))
            .fetchall()
        )
        return [RunManifest.model_validate_json(row["payload"]) for row in rows]


def _stamp(now: datetime | None) -> str:
    from datetime import UTC

    return (now or datetime.now(UTC)).isoformat()


def _read_only_uri(path: str) -> str:
    """``file:`` URI for ``mode=ro``, with the characters a URI reserves escaped.

    A Windows path is the reason this is not string concatenation: it starts with
    a drive letter and uses backslashes, and ``C:\\Users\\...`` inside a URI is
    read as a scheme unless the whole path is quoted and rooted with a slash.
    """
    absolute = Path(path).resolve().as_posix()
    if not absolute.startswith("/"):  # a Windows drive-letter path
        absolute = "/" + absolute
    return f"file:{urllib.parse.quote(absolute)}?mode=ro"


if TYPE_CHECKING:  # pragma: no cover - a type-checker assertion, not runtime code
    # SQLite is the reference implementation of the storage contract: if this
    # line stops type-checking, either the contract or this class has drifted.
    _conforms: type[Storage] = Database
