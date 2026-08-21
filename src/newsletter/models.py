"""Domain models — the typed contracts between pipeline stages.

Everything that crosses a stage boundary is a Pydantic model. Value objects are
frozen and reject unknown fields so that a schema drift fails loudly instead of
propagating silently.

Two invariants are enforced here rather than in the stages that use them, because
they protect published output:

* every URL is ``http``/``https`` (a model can never introduce a link scheme);
* every timestamp is timezone-aware (the date window must be deterministic).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Annotated, Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from pydantic import AfterValidator, AwareDatetime, BaseModel, ConfigDict, Field, model_validator

# --------------------------------------------------------------------------- #
# enums — closed vocabularies
# --------------------------------------------------------------------------- #


class TopicCategory(StrEnum):
    """Closed taxonomy. The analyzer cannot invent a category."""

    YOUTUBE_PLATFORM = "youtube_platform"
    YOUTUBE_MONETIZATION = "youtube_monetization"
    #: Children's and family content: audience trends, regulation, formats.
    KIDS_CONTENT = "kids_content"
    AI_VIDEO = "ai_video"
    AI_MODELS = "ai_models"
    AI_BUSINESS = "ai_business"
    OTHER = "other"


#: ``other`` is a valid classification but is normally excluded from publication.
PUBLISHABLE_CATEGORIES: frozenset[TopicCategory] = frozenset(
    c for c in TopicCategory if c is not TopicCategory.OTHER
)


class FetchStrategy(StrEnum):
    """How a source is ingested. Static configuration -- never a model decision."""

    RSS = "rss"
    SCRAPLING_STATIC = "scrapling_static"
    SCRAPLING_DYNAMIC = "scrapling_dynamic"
    SCRAPLING_STEALTH = "scrapling_stealth"


class WindowMode(StrEnum):
    """How the default date window is derived from the execution time."""

    #: ``[now - N days, now)`` -- includes today, window moves with the clock.
    ROLLING = "rolling"
    #: ``[start_of_today - N days, start_of_today)`` -- whole local days only.
    COMPLETED_DAYS = "completed_days"


class SubmissionStatus(StrEnum):
    """Lifecycle of a reader-submitted link."""

    #: Received, not yet considered by a run.
    PENDING = "pending"
    #: Assessed and good enough to compete for a place in the edition.
    APPROVED = "approved"
    #: Assessed and turned down; ``reason`` says why.
    REJECTED = "rejected"
    #: Approved and actually printed in an edition.
    PUBLISHED = "published"


class PipelineStage(StrEnum):
    """Explicit state machine states, used for metrics and error attribution."""

    LOAD_CONFIG = "load_config"
    DISCOVER = "discover"
    FETCH = "fetch"
    NORMALIZE = "normalize"
    HARD_FILTER = "hard_filter"
    DEDUPLICATE = "deduplicate"
    ANALYZE = "analyze"
    SCORE = "score"
    SELECT = "select"
    EDIT = "edit"
    VALIDATE = "validate"
    RENDER = "render"
    PERSIST = "persist"


# --------------------------------------------------------------------------- #
# shared validated types
# --------------------------------------------------------------------------- #

ALLOWED_URL_SCHEMES: frozenset[str] = frozenset({"http", "https"})


def validate_public_url(value: str) -> str:
    """Accept only absolute ``http``/``https`` URLs.

    This is the single choke point that keeps ``javascript:``, ``data:``, ``file:``
    and relative junk out of published editions.
    """
    candidate = value.strip()
    if not candidate:
        raise ValueError("URL must not be empty")
    parsed = urlparse(candidate)
    scheme = parsed.scheme.lower()
    if scheme not in ALLOWED_URL_SCHEMES:
        raise ValueError(f"unsupported URL scheme {scheme or '(none)'!r}: {candidate!r}")
    if not parsed.netloc:
        raise ValueError(f"URL has no host: {candidate!r}")
    return candidate


#: A URL that is safe to publish as a hyperlink.
PublicUrl = Annotated[str, AfterValidator(validate_public_url)]

#: A 0-5 integer rubric rating produced by the analyzer.
Rating = Annotated[int, Field(ge=0, le=5)]


class ValueModel(BaseModel):
    """Immutable, strict base for objects that cross a stage boundary."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class MutableModel(BaseModel):
    """Strict but mutable base, for accumulators such as the run manifest."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)


# --------------------------------------------------------------------------- #
# configuration objects
# --------------------------------------------------------------------------- #


class SourceConfig(ValueModel):
    """One configured source. ``priority`` feeds the deterministic score directly."""

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$", max_length=64)
    name: str = Field(min_length=1, max_length=120)
    entrypoint: PublicUrl
    strategy: FetchStrategy
    priority: int = Field(ge=0, le=10)
    enabled: bool = True
    category_hint: TopicCategory = TopicCategory.OTHER
    #: CSS/XPath selectors for ``scrapling_*`` strategies.
    selectors: dict[str, str] = Field(default_factory=dict)
    #: Adapter-specific knobs (pagination, wait conditions, limits).
    options: dict[str, Any] = Field(default_factory=dict)
    #: Opt-in: name of the JSON key holding the publication date inside a
    #: ``<script>`` data payload, for sites that render from an embedded blob and
    #: state no date in the markup itself. Unset means "do not look", which is the
    #: default for every source. See
    #: :func:`newsletter.normalization.article.extract_embedded_date`.
    embedded_date_key: str | None = Field(
        default=None, pattern=r"^[A-Za-z_][A-Za-z0-9_-]*$", max_length=64
    )


class DateWindow(ValueModel):
    """Half-open publication window ``[start, end)``.

    The window is computed by Python and never by a model. Half-open bounds make
    consecutive windows partition time without overlap.
    """

    start: AwareDatetime
    end: AwareDatetime
    timezone: str = "UTC"

    @model_validator(mode="after")
    def _check_order(self) -> DateWindow:
        if self.end <= self.start:
            raise ValueError(f"window end {self.end.isoformat()} must be after start")
        return self

    def contains(self, moment: datetime) -> bool:
        """True when ``moment`` falls inside the window. Naive datetimes are rejected."""
        if moment.tzinfo is None or moment.utcoffset() is None:
            raise ValueError("cannot test a naive datetime against a date window")
        return self.start <= moment < self.end

    @property
    def days(self) -> float:
        return (self.end - self.start).total_seconds() / 86400.0

    @classmethod
    def last_days(
        cls,
        days: int,
        *,
        now: datetime,
        tz_name: str = "UTC",
        mode: WindowMode = WindowMode.ROLLING,
    ) -> DateWindow:
        """Build the default window ending at (or just before) ``now``."""
        if days <= 0:
            raise ValueError("window length must be at least one day")
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("execution time must be timezone-aware")
        local_now = now.astimezone(ZoneInfo(tz_name))
        end = (
            local_now
            if mode is WindowMode.ROLLING
            else local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        )
        return cls(start=end - timedelta(days=days), end=end, timezone=tz_name)

    @classmethod
    def from_dates(cls, start_date: str, end_date: str, *, tz_name: str = "UTC") -> DateWindow:
        """Build a window from two inclusive ``YYYY-MM-DD`` calendar dates.

        ``--from 2026-08-11 --to 2026-08-17`` covers all of the 17th, so the
        half-open end is midnight on the 18th.
        """
        tz = ZoneInfo(tz_name)
        try:
            start = datetime.fromisoformat(start_date).replace(tzinfo=tz)
            end = datetime.fromisoformat(end_date).replace(tzinfo=tz)
        except ValueError as exc:
            raise ValueError(f"dates must be YYYY-MM-DD: {exc}") from exc
        return cls(start=start, end=end + timedelta(days=1), timezone=tz_name)

    def issue_label(self) -> str:
        """ISO week label of the last covered day, e.g. ``2026-W34``."""
        last_day = self.end - timedelta(microseconds=1)
        year, week, _ = last_day.isocalendar()
        return f"{year}-W{week:02d}"


# --------------------------------------------------------------------------- #
# ingestion and normalization
# --------------------------------------------------------------------------- #


class DiscoveredArticle(ValueModel):
    """A candidate found on an index page or feed, before fetching."""

    source_id: str
    url: PublicUrl
    title_hint: str | None = None
    published_at_hint: AwareDatetime | None = None


class RawArticle(ValueModel):
    """Exactly what came back from the network. Untrusted data.

    Whitespace trimming from the base class is switched off deliberately: Stage 3
    parses and hashes this content, so it must stay byte-faithful to the response.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=False)

    source_id: str
    url: PublicUrl
    final_url: PublicUrl
    raw_content: str
    retrieved_at: AwareDatetime
    content_type: str = "text/html"
    http_metadata: dict[str, Any] = Field(default_factory=dict)
    #: A page this one links to, fetched during ingestion when the page itself
    #: carries too little text to judge -- a post announcing something usually
    #: links to the thing it announces. Untrusted, exactly like raw_content, and
    #: chosen by Python from the page's own markup, never by a model.
    linked_url: PublicUrl | None = None
    linked_text: str | None = None


class NormalizedArticle(ValueModel):
    """Cleaned, canonical form. The only article shape the rest of the pipeline sees."""

    article_id: str
    source_id: str
    canonical_url: PublicUrl
    #: The URL actually fetched, when it differs from the canonical one. Keeps a
    #: reader submission traceable to the exact link that was submitted.
    origin_url: PublicUrl | None = None
    title: str = Field(min_length=1)
    published_at: AwareDatetime
    author: str | None = None
    clean_text: str
    content_hash: str = Field(min_length=8)
    retrieved_at: AwareDatetime


# --------------------------------------------------------------------------- #
# intelligence
# --------------------------------------------------------------------------- #


class ArticleAssessment(ValueModel):
    """Strict analyzer output. This is the *only* thing the analyzer may return.

    It deliberately contains no score and no publication decision: those are
    computed by ``ranking.scoring`` and ``ranking.selection``.
    """

    category: TopicCategory
    topic_relevance: Rating
    business_impact: Rating
    novelty: Rating
    actionability: Rating
    confidence: float = Field(ge=0.0, le=1.0)
    summary: str = Field(min_length=1)
    why_it_matters: str = Field(min_length=1)
    key_facts: list[str] = Field(default_factory=list, max_length=8)
    #: Structured event fingerprint, used for semantic deduplication.
    event_subject: str | None = None
    event_action: str | None = None
    event_object: str | None = None
    event_date: str | None = None

    def event_fingerprint(self) -> str | None:
        """Lowercased ``subject|action|object|date``, or None when underspecified."""
        parts = [self.event_subject, self.event_action, self.event_object]
        if not all(parts):
            return None
        return "|".join(p.strip().lower() for p in [*parts, self.event_date or ""])


class AssessmentRecord(ValueModel):
    """A validated assessment plus the provenance that defines its cache identity."""

    assessment: ArticleAssessment
    content_hash: str
    model: str
    prompt_version: str
    schema_version: str
    created_at: AwareDatetime

    @staticmethod
    def cache_key(content_hash: str, prompt_version: str, schema_version: str, model: str) -> str:
        return f"{content_hash}:{prompt_version}:{schema_version}:{model}"

    @property
    def key(self) -> str:
        return self.cache_key(
            self.content_hash, self.prompt_version, self.schema_version, self.model
        )


class RankedArticle(ValueModel):
    """An analyzed article with the score computed in Python."""

    article: NormalizedArticle
    assessment: ArticleAssessment
    source_name: str
    source_priority: int = Field(ge=0, le=10)
    final_score: int = Field(ge=0, le=100)


# --------------------------------------------------------------------------- #
# publication
# --------------------------------------------------------------------------- #


class NewsletterItem(ValueModel):
    """A publication-ready story. ``article_id`` preserves traceability to ingestion."""

    article_id: str
    headline: str = Field(min_length=1)
    category: TopicCategory
    source_name: str
    source_url: PublicUrl
    published_at: AwareDatetime
    summary: str = Field(min_length=1)
    why_it_matters: str = Field(min_length=1)
    key_facts: list[str] = Field(default_factory=list)
    score: int = Field(ge=0, le=100)


class NewsletterSection(ValueModel):
    """A named publication section holding already-selected stories."""

    category: TopicCategory
    title: str = Field(min_length=1)
    items: list[NewsletterItem] = Field(min_length=1)


class NewsletterEdition(ValueModel):
    """The complete structured edition. HTML, Markdown and JSON all render from this."""

    edition_id: str
    masthead: str = Field(min_length=1)
    issue_label: str = Field(min_length=1)
    period_start: AwareDatetime
    period_end: AwareDatetime
    executive_summary: list[str] = Field(min_length=1, max_length=6)
    lead_story: NewsletterItem
    sections: list[NewsletterSection] = Field(default_factory=list)
    generated_at: AwareDatetime

    def all_items(self) -> list[NewsletterItem]:
        """Lead story first, then every sectioned story, deduplicated by article id."""
        seen: set[str] = set()
        ordered: list[NewsletterItem] = []
        for item in [self.lead_story, *(i for s in self.sections for i in s.items)]:
            if item.article_id not in seen:
                seen.add(item.article_id)
                ordered.append(item)
        return ordered


# --------------------------------------------------------------------------- #
# reader submissions
# --------------------------------------------------------------------------- #


class Submission(ValueModel):
    """A link somebody proposed for the newsletter.

    A submission is fetched, normalized, deduplicated, assessed and scored exactly
    like an article from a configured source, and -- while
    ``submissions.reserved_slots`` is on -- it then takes one of the edition's
    slots by right rather than by score. ``note`` is for humans only and is never
    shown to a model: otherwise submitting a link would be a way to write the
    analyst's prompt.
    """

    submission_id: str
    url: PublicUrl
    submitted_at: AwareDatetime
    submitted_by: str | None = None
    note: str | None = Field(default=None, max_length=500)
    status: SubmissionStatus = SubmissionStatus.PENDING
    #: Why it was rejected, or which edition published it.
    reason: str | None = None
    #: Set once the submission has been normalized into an article.
    article_id: str | None = None
    decided_at: AwareDatetime | None = None

    def decide(
        self, status: SubmissionStatus, reason: str, *, now: datetime, article_id: str | None = None
    ) -> Submission:
        """Return a copy carrying a decision. Submissions are never mutated."""
        return self.model_copy(
            update={
                "status": status,
                "reason": reason,
                "decided_at": now,
                "article_id": article_id or self.article_id,
            }
        )


# --------------------------------------------------------------------------- #
# observability
# --------------------------------------------------------------------------- #


class RunError(ValueModel):
    """A recorded failure. Nothing is ever dropped silently."""

    stage: PipelineStage
    exception_class: str
    message: str
    timestamp: AwareDatetime
    source_id: str | None = None
    retry_count: int = 0


class WithheldStory(ValueModel):
    """One candidate the edition left out for a reason a reader could ask about.

    Not a failure -- a suppressed reprint, a capped subject and a collapsed report
    of an already-covered event are all the system working -- so these are kept
    apart from :class:`RunError`, which marks the run as failed. They belong in the
    manifest all the same: a story that vanishes between the candidate pool and the
    printed page must be explainable afterwards, from the artifact, not from a
    console line nobody kept.
    """

    article_id: str
    url: PublicUrl
    title: str
    reason: str
    #: The specific circumstance: which issue printed it, which subject filled up,
    #: which story it was folded into. Absent when the reason says it all.
    detail: str | None = None


class RunManifest(MutableModel):
    """Machine-readable record of one run."""

    run_id: str
    started_at: AwareDatetime
    finished_at: AwareDatetime | None = None
    window_start: AwareDatetime | None = None
    window_end: AwareDatetime | None = None
    dry_run: bool = False

    sources_attempted: int = 0
    sources_succeeded: int = 0
    sources_failed: int = 0
    articles_discovered: int = 0
    articles_in_window: int = 0
    articles_after_deduplication: int = 0
    llm_cache_hits: int = 0
    llm_calls: int = 0
    articles_above_threshold: int = 0
    articles_selected: int = 0
    #: How many of ``articles_selected`` hold a reserved slot -- a reader
    #: submission printed because it was submitted, not because it out-scored
    #: anything. ``articles_selected - articles_reserved`` is what the rubric
    #: earned, so an operator can read the split without re-deriving it.
    articles_reserved: int = 0
    #: How many candidates the run actually assessed, and how many it had. The
    #: pool is bounded (``newsletter.analysis_pool_max``), so these differ, and
    #: the gap is the sampling an operator has to be able to see.
    articles_analyzed: int = 0
    articles_available: int = 0
    #: Coverage floors the week could not fill: floor name -> how many stories
    #: short. The one omission :class:`WithheldStory` cannot express, because a
    #: story that was never in the pool has no id, url or title to record.
    coverage_floors_unmet: dict[str, int] = Field(default_factory=dict)
    newsletter_generated: bool = False

    analyzer_model: str | None = None
    editor_model: str | None = None
    analyzer_prompt_version: str | None = None
    editor_prompt_version: str | None = None
    schema_version: str | None = None

    errors: list[RunError] = Field(default_factory=list)
    #: Stories the selection withheld on purpose, each with the reason and the
    #: detail behind it. A count alone cannot answer "why is this week thin?".
    withheld: list[WithheldStory] = Field(default_factory=list)
    output_paths: dict[str, str] = Field(default_factory=dict)

    def record_withheld(
        self,
        *,
        article_id: str,
        url: str,
        title: str,
        reason: str,
        detail: str | None = None,
    ) -> WithheldStory:
        """Append a deliberate omission to the manifest and return it."""
        withheld = WithheldStory(
            article_id=article_id, url=url, title=title, reason=reason, detail=detail
        )
        self.withheld = [*self.withheld, withheld]
        return withheld

    def record_error(
        self,
        stage: PipelineStage,
        exc: BaseException,
        *,
        source_id: str | None = None,
        retry_count: int = 0,
        now: datetime | None = None,
    ) -> RunError:
        """Append a failure to the manifest and return it."""
        error = RunError(
            stage=stage,
            exception_class=type(exc).__name__,
            message=str(exc)[:1000],
            timestamp=now or datetime.now(UTC),
            source_id=source_id,
            retry_count=retry_count,
        )
        self.errors = [*self.errors, error]
        return error

    @property
    def failed(self) -> bool:
        return bool(self.errors)
