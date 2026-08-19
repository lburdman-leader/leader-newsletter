"""NewsletterEditor — presentation only.

The line-up is already fixed by ``ranking.selection`` before this module runs. The
editor may sharpen a headline, rewrite "why it matters" and write the executive
brief. It cannot add, remove, reorder or re-link anything, and that is enforced
structurally rather than by asking nicely:

* the wire schema has no field for a URL, a source, a date or a score;
* the edition is assembled **in Python** from the selected `RankedArticle`
  objects, so every link and date comes from ingestion (AC13);
* polish is matched by ``article_id`` against the selection; an unknown or
  duplicate id is discarded with a warning;
* any polish that fails validation is dropped and the deterministic original is
  used instead — cosmetics never fail an edition.

:func:`build_edition` is a pure function and needs no model at all, which is what
makes the offline fixture pipeline and ``--dry-run`` possible.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from newsletter.config import NewsletterSettings
from newsletter.intelligence.client import ModelError, StructuredClient
from newsletter.logging_setup import get_logger
from newsletter.models import (
    DateWindow,
    NewsletterEdition,
    NewsletterItem,
    NewsletterSection,
    RankedArticle,
)
from newsletter.ranking.selection import SelectionResult

logger = get_logger("intelligence.editor")

PROMPTS_DIR = Path(__file__).parent / "prompts"
EDITOR_PROMPT_VERSION = "v2"
EDITOR_SCHEMA_VERSION = "2"

MAX_HEADLINE_CHARS = 140
MAX_WHY_CHARS = 400
MAX_BRIEF_BULLETS = 6

#: Markup or links in editorial text mean the model strayed outside its remit.
_FORBIDDEN_IN_TEXT = re.compile(r"https?://|<[a-z/][^>]*>|\]\(", re.IGNORECASE)


class StoryPolish(BaseModel):
    """Editorial wording for one already-selected story."""

    model_config = ConfigDict(extra="forbid")

    article_id: str = Field(description="Exactly the id you were given for this story.")
    headline: str = Field(
        description="Polished headline. Newspaper style, active voice, no links, no markup."
    )
    why_it_matters: str = Field(
        description="One or two sentences of interpretation. No new facts, no links."
    )


class EditorialPayload(BaseModel):
    """Everything the editor model is allowed to return.

    Note the absence of URLs, sources, dates, scores, ordering and any way to
    name a story that was not selected.
    """

    model_config = ConfigDict(extra="forbid")

    executive_summary: list[str] = Field(
        description="Three to five short bullets summarising the week. No numbering."
    )
    stories: list[StoryPolish] = Field(
        description="One entry per story you were given, in the same order."
    )


@lru_cache(maxsize=8)
def load_editor_prompt(version: str = EDITOR_PROMPT_VERSION) -> str:
    path = PROMPTS_DIR / f"newsletter_editor_{version}.md"
    if not path.is_file():
        raise FileNotFoundError(f"editor prompt {version} not found at {path}")
    return path.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# validation of editorial text
# --------------------------------------------------------------------------- #


def clean_editorial_text(value: str, *, limit: int) -> str | None:
    """Return usable editorial prose, or None when it must be rejected."""
    text = " ".join(value.split())
    if not text or len(text) > limit:
        return None
    if _FORBIDDEN_IN_TEXT.search(text):
        return None
    return text


def usable_polish(payload: EditorialPayload, allowed_ids: Sequence[str]) -> dict[str, StoryPolish]:
    """Keep only polish that refers to a selected story exactly once and is clean."""
    allowed = set(allowed_ids)
    seen: set[str] = set()
    usable: dict[str, StoryPolish] = {}

    for story in payload.stories:
        if story.article_id not in allowed:
            logger.warning("editor returned an unknown article id %r; ignoring", story.article_id)
            continue
        if story.article_id in seen:
            logger.warning("editor returned %r twice; ignoring the repeat", story.article_id)
            continue
        seen.add(story.article_id)

        headline = clean_editorial_text(story.headline, limit=MAX_HEADLINE_CHARS)
        why = clean_editorial_text(story.why_it_matters, limit=MAX_WHY_CHARS)
        if headline is None and why is None:
            logger.warning("editor polish for %r rejected entirely", story.article_id)
            continue

        usable[story.article_id] = StoryPolish(
            article_id=story.article_id,
            headline=headline or "",
            why_it_matters=why or "",
        )

    return usable


def usable_brief(payload: EditorialPayload) -> list[str]:
    """Clean brief bullets; an unusable brief becomes an empty list."""
    bullets = []
    for raw in payload.executive_summary:
        bullet = clean_editorial_text(raw, limit=MAX_WHY_CHARS)
        if bullet:
            bullets.append(bullet)
    return bullets[:MAX_BRIEF_BULLETS]


# --------------------------------------------------------------------------- #
# deterministic assembly
# --------------------------------------------------------------------------- #


def fallback_brief(selected: Sequence[RankedArticle]) -> list[str]:
    """A brief written by Python, used when no editor result is available.

    Plain and factual rather than clever: an edition without a model is still a
    usable edition.
    """
    bullets = [
        f"{ranked.article.title} ({ranked.source_name})" for ranked in selected[:MAX_BRIEF_BULLETS]
    ]
    return bullets or ["No stories met the publication threshold this week."]


def build_item(ranked: RankedArticle, polish: StoryPolish | None) -> NewsletterItem:
    """Assemble one publication item. URL, date and source come only from ingestion."""
    headline = (polish.headline if polish and polish.headline else "") or ranked.article.title
    why = (
        polish.why_it_matters if polish and polish.why_it_matters else ""
    ) or ranked.assessment.why_it_matters

    return NewsletterItem(
        article_id=ranked.article.article_id,
        headline=headline,
        category=ranked.assessment.category,
        source_name=ranked.source_name,
        source_url=ranked.article.canonical_url,
        published_at=ranked.article.published_at,
        summary=ranked.assessment.summary,
        why_it_matters=why,
        key_facts=list(ranked.assessment.key_facts),
        score=ranked.final_score,
    )


def build_edition(
    selection: SelectionResult,
    settings: NewsletterSettings,
    window: DateWindow,
    *,
    polish: Mapping[str, StoryPolish] | None = None,
    brief: Sequence[str] | None = None,
    now: datetime | None = None,
) -> NewsletterEdition:
    """Assemble the edition deterministically. Pure function; no model involved."""
    if selection.is_empty:
        raise ValueError("cannot build an edition with no selected stories")

    polish_map = dict(polish or {})
    lead_ranked = selection.lead
    assert lead_ranked is not None  # guaranteed by the emptiness check above

    lead = build_item(lead_ranked, polish_map.get(lead_ranked.article.article_id))

    sections: list[NewsletterSection] = []
    for category, articles in selection.sections(settings):
        items = [
            build_item(ranked, polish_map.get(ranked.article.article_id)) for ranked in articles
        ]
        sections.append(
            NewsletterSection(category=category, title=settings.title_for(category), items=items)
        )

    bullets = [b for b in (brief or []) if b] or fallback_brief(selection.selected)

    return NewsletterEdition(
        edition_id=window.issue_label(),
        masthead=settings.masthead,
        issue_label=window.issue_label(),
        period_start=window.start,
        period_end=window.end,
        executive_summary=bullets[:MAX_BRIEF_BULLETS],
        lead_story=lead,
        sections=sections,
        generated_at=now or datetime.now(UTC),
    )


# --------------------------------------------------------------------------- #
# the model-backed editor
# --------------------------------------------------------------------------- #


def build_editor_content(
    selection: SelectionResult, settings: NewsletterSettings, window: DateWindow
) -> str:
    """Structured, validated records only -- never raw HTML or article bodies."""
    lines = [
        "## Issue metadata (trusted, supplied by the pipeline)",
        f"masthead: {settings.masthead}",
        f"issue: {window.issue_label()}",
        f"period: {window.start.date().isoformat()} to {(window.end.date()).isoformat()}",
        "",
        "## Selected stories (already chosen and ordered; the first is the lead)",
    ]
    for position, ranked in enumerate(selection.selected, start=1):
        lines.extend(
            [
                "",
                f"### {position}. article_id: {ranked.article.article_id}",
                f"section: {ranked.assessment.category.value}",
                f"current_headline: {ranked.article.title}",
                f"summary: {ranked.assessment.summary}",
                f"why_it_matters: {ranked.assessment.why_it_matters}",
            ]
        )
        if ranked.assessment.key_facts:
            facts = "; ".join(ranked.assessment.key_facts)
            lines.append(f"key_facts: {facts}")
    return "\n".join(lines) + "\n"


class NewsletterEditor:
    """Ask the model for wording, then assemble the edition in Python."""

    def __init__(
        self,
        client: StructuredClient,
        *,
        prompt_version: str = EDITOR_PROMPT_VERSION,
        schema_version: str = EDITOR_SCHEMA_VERSION,
    ) -> None:
        self.client = client
        self.prompt_version = prompt_version
        self.schema_version = schema_version
        self.instructions = load_editor_prompt(prompt_version)

    @property
    def model(self) -> str:
        return self.client.model

    def compose(
        self,
        selection: SelectionResult,
        settings: NewsletterSettings,
        window: DateWindow,
        *,
        now: datetime | None = None,
    ) -> NewsletterEdition:
        """Edit and assemble. Raises :class:`ModelError` if the model fails."""
        payload, _ = self.client.parse(
            instructions=self.instructions,
            content=build_editor_content(selection, settings, window),
            schema=EditorialPayload,
        )
        selected_ids = [ranked.article.article_id for ranked in selection.selected]
        return build_edition(
            selection,
            settings,
            window,
            polish=usable_polish(payload, selected_ids),
            brief=usable_brief(payload),
            now=now,
        )

    def compose_or_fallback(
        self,
        selection: SelectionResult,
        settings: NewsletterSettings,
        window: DateWindow,
        *,
        now: datetime | None = None,
    ) -> tuple[NewsletterEdition, ModelError | None]:
        """Compose, degrading to the deterministic edition if the model fails.

        A failed editorial call costs polish, never the edition: the stories, the
        links and the ordering were all decided before the model was consulted.
        """
        try:
            return self.compose(selection, settings, window, now=now), None
        except ModelError as exc:
            logger.warning("editorial synthesis failed, using deterministic edition: %s", exc)
            return build_edition(selection, settings, window, now=now), exc
