"""Configuration loading and validation.

Behaviour lives in YAML (``config/*.yaml``); only secrets, paths and model names
come from the environment. Loading is strict: an invalid configuration aborts the
run before any network call, because a bad config is one of the few conditions
that must fail the whole run.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import Field, SecretStr, ValidationError, field_validator, model_validator

from newsletter.concurrency import (
    DEFAULT_ANALYSIS_CONCURRENCY,
    DEFAULT_FETCH_CONCURRENCY,
)
from newsletter.models import (
    PUBLISHABLE_CATEGORIES,
    FetchStrategy,
    SourceConfig,
    TopicCategory,
    ValueModel,
    WindowMode,
    validate_public_url,
)
from newsletter.persistence.dsn import (
    DEFAULT_DATABASE_URL,
    dsn_for_sqlite_path,
    is_sqlite,
    sqlite_path_from_dsn,
    validate_dsn,
)

DEFAULT_CONFIG_DIR = Path("config")
SOURCES_FILE = "sources.yaml"
NEWSLETTER_FILE = "newsletter.yaml"

#: Section headings as they are printed. The newsletter is published in Spanish
#: for a Latin American audience; the code around it stays in English.
DEFAULT_SECTION_TITLES: dict[TopicCategory, str] = {
    TopicCategory.YOUTUBE_PLATFORM: "YouTube: la plataforma",
    TopicCategory.YOUTUBE_MONETIZATION: "YouTube: monetización",
    TopicCategory.KIDS_CONTENT: "Contenido infantil y familiar",
    TopicCategory.AI_VIDEO: "IA para video y creatividad",
    TopicCategory.AI_MODELS: "Herramientas y modelos de IA",
    TopicCategory.AI_BUSINESS: "IA en los negocios",
    TopicCategory.OTHER: "También pasó",
}


class ConfigError(Exception):
    """Raised when configuration is missing, unparseable or invalid."""


# --------------------------------------------------------------------------- #
# settings models
# --------------------------------------------------------------------------- #


class CoverageFloor(ValueModel):
    """A named group of categories the edition must carry a minimum of.

    A *group* rather than three per-category minima on purpose: the requirement
    is "at least four stories from the company's own beat", and one-of-each would
    be a strictly stronger rule that the owner did not ask for and that a thin
    week could not satisfy. The sibling of ``section_limits``: that caps a
    category from above, this holds a group up from below.

    A floor changes *which* stories are seated, never how many, and never what
    counts as publishable: a floor story still clears ``min_score`` and still
    obeys every cap. Nothing is ever padded to reach a minimum.
    """

    #: The categories that satisfy this floor. Any one of them counts.
    categories: list[TopicCategory] = Field(min_length=1)
    #: How many stories the edition must carry from that group, best-effort
    #: against whatever slots reserved submissions leave free.
    minimum: int = Field(ge=0, le=50)


class NewsletterSettings(ValueModel):
    """Editorial policy. Cadence and thresholds are configuration, not architecture."""

    masthead: str = Field(default="AI & Digital Intelligence Weekly", min_length=1)
    tagline: str = ""
    timezone: str = "UTC"

    window_days: int = Field(default=7, ge=1, le=90)
    window_mode: WindowMode = WindowMode.ROLLING

    max_items: int = Field(default=8, ge=1, le=50)
    #: The smallest edition worth printing. When the line-up comes up short, the
    #: caps that *ration* slots -- ``section_limits``, ``max_per_source`` and
    #: ``max_per_subject`` -- are relaxed one step at a time and the edition is
    #: re-seated, until it reaches this many stories or no rationed story is left
    #: to admit. Nothing that protects correctness or quality is ever relaxed:
    #: ``min_score``, ``excluded_categories``, deduplication, both collapse
    #: passes, cross-edition suppression and the entity-fidelity guard all stand.
    #: A week that genuinely offers less publishes short and records the
    #: shortfall. ``0`` switches the minimum off and restores the caps' authority
    #: absolutely.
    min_items: int = Field(default=6, ge=0, le=50)
    min_score: int = Field(default=70, ge=0, le=100)
    #: Collapse several articles covering one event, using the analyzer fingerprint.
    collapse_events: bool = True
    #: Second collapse pass, on the article text rather than the fingerprint, for
    #: the reports of one event whose analyzer keys disagree. Within a run only,
    #: and only over candidates that reach ``min_score``: nothing under the floor
    #: can print, so folding it changes no edition and can only be wrong.
    collapse_similar_events: bool = True
    #: Cosine similarity at or above which two candidates are one event. Tuned on
    #: a real edition: three outlets on one launch scored 0.28-0.38, while the
    #: closest genuinely distinct pair in that edition scored 0.14. Raising it
    #: prints the same story twice; lowering it starts folding together stories
    #: that merely share an industry.
    similar_event_threshold: float = Field(default=0.21, gt=0.0, le=1.0)
    #: Never reprint a story an earlier edition already carried. Matched on
    #: identity (article id, content hash, title) and never on topic, so a
    #: follow-up on the same subject is still publishable. Off only for
    #: debugging: a reader who sees last week's story again stops trusting the
    #: edition.
    suppress_already_published: bool = True
    #: Check reader-visible prose for named entities its own source never uses
    #: ("UTube" where the article said "YouTube"). Deterministic, no model call.
    #: Off only for debugging: a corrupted brand name is an error in print.
    check_entity_fidelity: bool = True
    #: Per-category caps that prevent one topic monopolising the edition.
    section_limits: dict[TopicCategory, int] = Field(default_factory=dict)
    #: Named coverage floors: groups of categories the edition must carry a
    #: minimum of, so a week of general AI news cannot leave the company's own
    #: beat unrepresented. The mirror image of ``section_limits``.
    coverage_floors: dict[str, CoverageFloor] = Field(default_factory=dict)
    #: How many candidates one run assesses before it first asks whether the
    #: edition is finished. Also the size of every later batch.
    analysis_pool_min: int = Field(default=20, ge=1, le=500)
    #: The hard ceiling on assessed candidates, reader submissions excluded --
    #: a reserved link is never crowded out by a budget. ``None`` or ``0``
    #: removes the ceiling and restores exhaustive assessment of the whole pool.
    analysis_pool_max: int | None = Field(default=50, ge=0, le=5_000)
    #: Cap per source, so no single publication can fill the edition. None means
    #: no cap; the category limits and max_items still apply.
    max_per_source: int | None = Field(default=None, ge=1, le=50)
    #: Cap per event subject, so no single company can fill the edition even when
    #: its stories are genuinely distinct events. An article whose analyst named
    #: no subject is uncapped. None removes the cap entirely.
    max_per_subject: int | None = Field(default=2, ge=1, le=50)
    #: Publication order of the sections.
    section_order: list[TopicCategory] = Field(default_factory=list)
    section_titles: dict[TopicCategory, str] = Field(default_factory=dict)
    #: Categories never published, even when they score well.
    excluded_categories: list[TopicCategory] = Field(default_factory=lambda: [TopicCategory.OTHER])

    @model_validator(mode="before")
    @classmethod
    def _fit_default_minimum(cls, data: Any) -> Any:
        """A *default* minimum never exceeds the edition it is a minimum for.

        ``min_items`` defaults to six, but an edition deliberately configured
        smaller than six is not misconfigured -- it simply cannot carry six, and
        "as many as fit" is the only reading of the default that makes sense. An
        explicitly configured ``min_items`` above ``max_items`` is a different
        thing and is still refused below, because nobody means it.
        """
        if not isinstance(data, dict) or data.get("min_items") is not None:
            return data
        max_items = data.get("max_items")
        default = cls.model_fields["min_items"].default
        if isinstance(max_items, int) and max_items < default:
            return {**data, "min_items": max_items}
        return data

    @model_validator(mode="after")
    def _check(self) -> NewsletterSettings:
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown timezone {self.timezone!r}: {exc}") from exc
        for category, limit in self.section_limits.items():
            if limit < 0:
                raise ValueError(f"section_limits[{category.value}] must be >= 0")

        if self.min_items > self.max_items:
            # Otherwise every edition is permanently short and every run relaxes
            # its caps to the maximum trying to reach a number it cannot reach.
            raise ValueError(f"min_items {self.min_items} exceeds max_items {self.max_items}")

        excluded = set(self.excluded_categories)
        for name, floor in self.coverage_floors.items():
            if not name.strip():
                raise ValueError("coverage_floors: a floor must have a name")
            forbidden = sorted(c.value for c in floor.categories if c in excluded)
            if forbidden:
                # Otherwise the floor asks for stories the edition may never
                # print, and would be permanently and inexplicably unmet.
                raise ValueError(
                    f"coverage_floors[{name}]: {', '.join(forbidden)} "
                    "is in excluded_categories and can never satisfy a floor"
                )
            if floor.minimum > self.max_items:
                raise ValueError(
                    f"coverage_floors[{name}]: minimum {floor.minimum} "
                    f"exceeds max_items {self.max_items}"
                )

        if self.analysis_pool_max and self.analysis_pool_max < self.analysis_pool_min:
            raise ValueError(
                f"analysis_pool_max {self.analysis_pool_max} is below "
                f"analysis_pool_min {self.analysis_pool_min}"
            )
        return self

    @property
    def analysis_pool_cap(self) -> int | None:
        """The assessed-candidate ceiling, or ``None`` for "assess everything".

        ``0`` and ``None`` are one setting spelled two ways -- YAML has no tidy
        way to say "off" for a number -- and both restore the exhaustive
        behaviour this engine had before the pool was bounded.
        """
        return self.analysis_pool_max or None

    def relaxed(self, step: int) -> NewsletterSettings:
        """This policy with every *rationing* cap raised by ``step``.

        The caps exist to ration scarce slots between competing stories, and they
        were calibrated for a smaller edition than the one now published. When a
        week's line-up falls below ``min_items``, raising them by one is the
        smallest editorial concession available: it admits one more story per
        category, per source and per company, and it admits nothing that was not
        already publishable.

        **Exactly three fields move.** ``min_score``, ``excluded_categories``,
        ``max_items``, ``coverage_floors`` and every collapse and suppression
        switch are copied through untouched, so no amount of relaxation can print
        a story the rubric refused, a category the owner excluded, or an eleventh
        item. A cap never rises above ``max_items``, which is the point at which
        it stops constraining anything at all -- so :meth:`relaxed` reaches a
        fixed point and the caller's loop is guaranteed to terminate.
        """
        if step <= 0:
            return self
        ceiling = self.max_items
        return self.model_copy(
            update={
                "section_limits": {
                    category: min(limit + step, ceiling)
                    for category, limit in self.section_limits.items()
                },
                "max_per_source": (
                    None
                    if self.max_per_source is None
                    else min(self.max_per_source + step, ceiling)
                ),
                "max_per_subject": (
                    None
                    if self.max_per_subject is None
                    else min(self.max_per_subject + step, ceiling)
                ),
            }
        )

    def title_for(self, category: TopicCategory) -> str:
        return self.section_titles.get(category) or DEFAULT_SECTION_TITLES[category]

    def limit_for(self, category: TopicCategory) -> int:
        """Cap for a category; absent means only the global ``max_items`` applies."""
        return self.section_limits.get(category, self.max_items)

    def ordered_categories(self) -> list[TopicCategory]:
        """Publication order: configured order first, then remaining publishable ones."""
        excluded = set(self.excluded_categories)
        ordered = [c for c in self.section_order if c not in excluded]
        rest = [c for c in PUBLISHABLE_CATEGORIES if c not in ordered and c not in excluded]
        return ordered + sorted(rest, key=lambda c: c.value)


class SubmissionSettings(ValueModel):
    """Policy for reader-submitted links.

    A submission is ingested, scored and checked exactly like any other article;
    these settings decide who may propose one, how many are considered per run,
    and how many of the edition's slots submissions hold by right.
    """

    enabled: bool = True
    #: Submissions appear as one synthetic source, so they score like any other.
    source_id: str = Field(default="reader-submissions", pattern=r"^[a-z0-9][a-z0-9-]*$")
    source_name: str = Field(default="Reader submission", min_length=1)
    #: Priority added to the score. Deliberately modest: a submitted link should
    #: not outrank a trusted publication merely by having been submitted.
    priority: int = Field(default=4, ge=0, le=10)
    #: How many pending submissions one run will consider.
    max_per_run: int = Field(default=20, ge=1, le=200)
    #: How many of the edition's slots submissions take before the rubric fills
    #: the rest. A reserved slot is *guaranteed*: it bypasses ``min_score`` and
    #: the three caps that ration scarce slots between competing stories --
    #: ``max_per_source``, ``max_per_subject`` and ``section_limits`` -- because a
    #: submission is not competing for its slot. It bypasses nothing that
    #: protects correctness: deduplication, event and similarity collapse,
    #: cross-edition suppression, the entity-fidelity guard,
    #: ``excluded_categories`` and ``max_items`` all still apply.
    #:
    #: ``None`` reserves a slot for every submission the run has, bounded by
    #: ``max_items``. ``0`` switches reservation off, and a submitted link is back
    #: to competing on its score like everything else.
    reserved_slots: int | None = Field(default=None, ge=0, le=50)
    require_https: bool = True
    #: Hosts that may never be submitted; a bare domain also blocks subdomains.
    blocked_hosts: list[str] = Field(default_factory=list)

    #: Where readers can propose a link -- the address ``newsletter serve``
    #: answers on, once it is reachable from wherever the edition is read. The
    #: rendered edition links to it, so it is the one URL in the page that does
    #: not come from ingestion and it is validated here, at load time, rather
    #: than in a template. ``None`` (the default) prints no call to action at
    #: all: an intake nobody can reach is worse than no invitation.
    form_url: str | None = None

    #: A post is often a pointer, not the story. When a submitted page carries
    #: less than `min_text_chars` of text, follow its own outbound link and
    #: attach what it points at, so the analyst judges the announcement rather
    #: than the 300 characters announcing it.
    follow_links: bool = True
    min_text_chars: int = Field(default=600, ge=0, le=20_000)
    max_link_hops: int = Field(default=3, ge=1, le=10)
    max_linked_chars: int = Field(default=8_000, ge=500, le=50_000)

    #: A post is often a pointer, not the story. When a submitted page carries
    #: less than `min_text_chars` of text, follow its own outbound link and
    #: attach what it points at, so the analyst judges the announcement rather
    #: than the 300 characters announcing it.
    follow_links: bool = True
    min_text_chars: int = Field(default=600, ge=0, le=20_000)
    max_link_hops: int = Field(default=3, ge=1, le=10)
    max_linked_chars: int = Field(default=8_000, ge=500, le=50_000)

    @field_validator("form_url", mode="before")
    @classmethod
    def _check_form_url(cls, value: Any) -> str | None:
        """An empty value means "no form"; anything else must be publishable."""
        if value is None:
            return None
        candidate = str(value).strip()
        if not candidate:
            return None
        return validate_public_url(candidate)

    def as_source(self) -> SourceConfig:
        """The synthetic source record submissions are ingested through."""
        return SourceConfig(
            id=self.source_id,
            name=self.source_name,
            entrypoint="https://submissions.invalid/",
            strategy=FetchStrategy.RSS,  # unused: the adapter is supplied directly
            priority=self.priority,
            enabled=self.enabled,
            category_hint=TopicCategory.OTHER,
            options={"max_articles": self.max_per_run},
        )


class RuntimeSettings(ValueModel):
    """Paths, models and credentials. Populated from the environment."""

    #: Where the database lives, as a connection string, so moving the engine to
    #: a server is a deployment variable rather than a code change.
    database_url: str = DEFAULT_DATABASE_URL
    #: The historical filesystem form, kept working. It stays in step with
    #: ``database_url``: see :meth:`_reconcile_database_location`.
    db_path: Path = Path("newsletter.sqlite")
    output_dir: Path = Path("output")
    analyzer_model: str = "gpt-4.1-mini"
    editor_model: str = "gpt-4.1"
    log_level: str = "INFO"
    request_timeout_seconds: float = Field(default=60.0, gt=0)
    max_retries: int = Field(default=2, ge=0, le=5)
    #: How many article assessments may be in flight at once. The whole of a call
    #: is spent waiting on the API, so this is the single biggest lever on how
    #: long a run takes. ``1`` restores the fully sequential behaviour, which is
    #: also how the equivalence tests prove concurrency changes no artifact.
    analysis_concurrency: int = Field(default=DEFAULT_ANALYSIS_CONCURRENCY, ge=1, le=32)
    #: How many articles of one source may be fetched at once. Lower than the
    #: model's, because these requests all land on a single origin.
    fetch_concurrency: int = Field(default=DEFAULT_FETCH_CONCURRENCY, ge=1, le=16)
    openai_api_key: SecretStr | None = None

    @model_validator(mode="before")
    @classmethod
    def _reconcile_database_location(cls, data: Any) -> Any:
        """Keep ``database_url`` and ``db_path`` describing the same database.

        Two spellings of one setting is a trap unless the tie-break is stated:

        * ``database_url`` wins when it is given, because it is the only spelling
          that can name a server. For a ``sqlite://`` URL, ``db_path`` is
          rewritten from it so code reading the path still opens the right file.
        * ``db_path`` alone is promoted to ``sqlite:///<path>``, so every
          existing configuration keeps working untouched.
        * Neither given leaves both at their (matching) defaults.

        Layering is resolved before this runs -- see ``_runtime_from_env`` -- so
        an environment variable still beats a YAML value of the other spelling.
        """
        if not isinstance(data, dict):  # pragma: no cover - pydantic passes a mapping
            return data
        values = dict(data)
        url = str(values.get("database_url") or "").strip()
        if url:
            values["database_url"] = validate_dsn(url)
            if is_sqlite(values["database_url"]):
                values["db_path"] = Path(sqlite_path_from_dsn(values["database_url"]))
        else:
            # An empty value is "unset", exactly as it is for every other override.
            values.pop("database_url", None)
            if values.get("db_path") is not None:
                values["database_url"] = dsn_for_sqlite_path(values["db_path"])
        return values

    @model_validator(mode="after")
    def _check_log_level(self) -> RuntimeSettings:
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        if self.log_level.upper() not in allowed:
            raise ValueError(f"log_level must be one of {sorted(allowed)}")
        return self

    @property
    def has_openai_key(self) -> bool:
        return bool(self.openai_api_key and self.openai_api_key.get_secret_value().strip())


class AppConfig(ValueModel):
    """Everything the pipeline needs, validated."""

    sources: list[SourceConfig]
    newsletter: NewsletterSettings
    runtime: RuntimeSettings
    submissions: SubmissionSettings = SubmissionSettings()

    @model_validator(mode="after")
    def _check_sources(self) -> AppConfig:
        seen: set[str] = set()
        for source in self.sources:
            if source.id in seen:
                raise ValueError(f"duplicate source id {source.id!r}")
            seen.add(source.id)
        if not self.sources:
            raise ValueError("no sources configured")
        if not any(s.enabled for s in self.sources):
            raise ValueError("every configured source is disabled")
        return self

    @property
    def enabled_sources(self) -> list[SourceConfig]:
        """Enabled sources, highest priority first, then by id for determinism."""
        return sorted(
            (s for s in self.sources if s.enabled),
            key=lambda s: (-s.priority, s.id),
        )

    def source_by_id(self, source_id: str) -> SourceConfig:
        for source in self.sources:
            if source.id == source_id:
                return source
        raise KeyError(f"unknown source id {source_id!r}")


# --------------------------------------------------------------------------- #
# loading
# --------------------------------------------------------------------------- #


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ConfigError(f"missing configuration file: {path}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path} is not valid YAML: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must contain a mapping at the top level")
    return data


def _runtime_from_env(env: Mapping[str, str], overrides: dict[str, Any]) -> RuntimeSettings:
    """Environment wins over YAML defaults; empty values are treated as unset."""
    values = dict(overrides)

    def take(key: str, field: str, cast: type | None = None) -> None:
        raw = env.get(key, "").strip()
        if raw:
            values[field] = cast(raw) if cast else raw

    take("NEWSLETTER_DATABASE_URL", "database_url")
    take("NEWSLETTER_DB_PATH", "db_path", Path)
    take("NEWSLETTER_OUTPUT_DIR", "output_dir", Path)
    take("OPENAI_ANALYZER_MODEL", "analyzer_model")
    take("OPENAI_EDITOR_MODEL", "editor_model")
    take("LOG_LEVEL", "log_level")

    def take_int(key: str, field: str) -> None:
        """An integer override, refused rather than coerced when it is not one."""
        raw = env.get(key, "").strip()
        if not raw:
            return
        try:
            values[field] = int(raw)
        except ValueError as exc:
            raise ConfigError(f"{key} must be a whole number, got {raw!r}") from exc

    take_int("NEWSLETTER_ANALYSIS_CONCURRENCY", "analysis_concurrency")
    take_int("NEWSLETTER_FETCH_CONCURRENCY", "fetch_concurrency")

    # The database has two spellings, so the layers have to be untangled before
    # the model's own tie-break runs: whichever spelling the *environment* gives
    # names the database, and the other spelling from YAML is discarded rather
    # than silently overriding it. Within one layer, `database_url` wins,
    # because it is the only spelling that can name a server.
    if env.get("NEWSLETTER_DATABASE_URL", "").strip():
        values.pop("db_path", None)
    elif env.get("NEWSLETTER_DB_PATH", "").strip():
        values.pop("database_url", None)

    api_key = env.get("OPENAI_API_KEY", "").strip()
    if api_key:
        values["openai_api_key"] = SecretStr(api_key)

    if "log_level" in values:
        values["log_level"] = str(values["log_level"]).upper()

    try:
        return RuntimeSettings(**values)
    except ValidationError as exc:
        raise ConfigError(f"invalid runtime settings: {_format_errors(exc)}") from exc


def _format_errors(exc: ValidationError) -> str:
    parts = []
    for error in exc.errors():
        location = ".".join(str(p) for p in error["loc"]) or "(root)"
        parts.append(f"{location}: {error['msg']}")
    return "; ".join(parts)


def load_config(
    config_dir: Path | str = DEFAULT_CONFIG_DIR,
    *,
    env: Mapping[str, str] | None = None,
) -> AppConfig:
    """Load and validate the full application configuration.

    Raises :class:`ConfigError` with an actionable message; never returns a
    partially valid configuration.
    """
    environment = os.environ if env is None else env
    directory = Path(config_dir)

    sources_raw = _read_yaml(directory / SOURCES_FILE)
    newsletter_raw = _read_yaml(directory / NEWSLETTER_FILE)

    entries = sources_raw.get("sources")
    if not isinstance(entries, list):
        raise ConfigError(f"{directory / SOURCES_FILE} must contain a 'sources' list")

    sources: list[SourceConfig] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ConfigError(f"{SOURCES_FILE}: source #{index + 1} must be a mapping")
        try:
            sources.append(SourceConfig(**entry))
        except ValidationError as exc:
            label = entry.get("id", f"#{index + 1}")
            raise ConfigError(f"{SOURCES_FILE}: source {label}: {_format_errors(exc)}") from exc

    try:
        newsletter = NewsletterSettings(**(newsletter_raw.get("newsletter") or {}))
    except ValidationError as exc:
        raise ConfigError(f"{NEWSLETTER_FILE}: newsletter: {_format_errors(exc)}") from exc

    runtime = _runtime_from_env(environment, newsletter_raw.get("runtime") or {})

    try:
        submissions = SubmissionSettings(**(newsletter_raw.get("submissions") or {}))
    except ValidationError as exc:
        raise ConfigError(f"{NEWSLETTER_FILE}: submissions: {_format_errors(exc)}") from exc

    try:
        return AppConfig(
            sources=sources, newsletter=newsletter, runtime=runtime, submissions=submissions
        )
    except ValidationError as exc:
        raise ConfigError(f"invalid configuration: {_format_errors(exc)}") from exc
