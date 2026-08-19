"""Rendering. Templates produce markup; models never do.

HTML, Markdown and JSON all render from one `NewsletterEdition`, so the three
artifacts cannot disagree with each other.

Two safety properties are enforced here, immediately before anything is written:

* **Link integrity (AC13).** Every URL in the edition is re-validated as
  ``http``/``https`` and, when the caller supplies the ingested set, checked to be
  one of those URLs. A link that did not come from ingestion stops the render.
* **Escaping.** The HTML template runs with autoescape on and uses no ``| safe``,
  because headlines and summaries originate in scraped pages. Markdown text is
  escaped so a bracket in a headline cannot break a link.
"""

from __future__ import annotations

import json
from collections.abc import Collection, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from newsletter.config import DEFAULT_SECTION_TITLES
from newsletter.logging_setup import get_logger
from newsletter.models import (
    NewsletterEdition,
    RankedArticle,
    RunManifest,
    TopicCategory,
    validate_public_url,
)
from newsletter.ranking.scoring import score_components

logger = get_logger("rendering")

TEMPLATES_DIR = Path(__file__).parent / "templates"

HTML_TEMPLATE = "newsletter.html.j2"
MARKDOWN_TEMPLATE = "newsletter.md.j2"

HTML_FILENAME = "newsletter.html"
MARKDOWN_FILENAME = "newsletter.md"
JSON_FILENAME = "newsletter.json"
SELECTED_FILENAME = "selected_articles.json"
MANIFEST_FILENAME = "run_manifest.json"

#: Characters that would break Markdown structure if they appeared in text.
_MARKDOWN_ESCAPES = str.maketrans(
    {
        "\\": "\\\\",
        "[": "\\[",
        "]": "\\]",
        "|": "\\|",
        "`": "\\`",
        "*": "\\*",
        "_": "\\_",
    }
)


class RenderError(Exception):
    """The edition cannot be rendered safely."""


# --------------------------------------------------------------------------- #
# template filters
# --------------------------------------------------------------------------- #


def issue_date(value: datetime) -> str:
    """``17 Aug 2026`` -- unambiguous for both British and American readers."""
    return f"{value.day} {value:%b %Y}"


def issue_datetime(value: datetime) -> str:
    return f"{value.day} {value:%b %Y at %H:%M %Z}".strip()


def markdown_escape(value: str) -> str:
    return str(value).translate(_MARKDOWN_ESCAPES)


def build_environment() -> Environment:
    env = Environment(
        loader=FileSystemLoader(TEMPLATES_DIR),
        autoescape=lambda name: bool(name) and name.endswith(".html.j2"),
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
    )
    env.filters["issue_date"] = issue_date
    env.filters["issue_datetime"] = issue_datetime
    env.filters["md"] = markdown_escape
    return env


# --------------------------------------------------------------------------- #
# link validation (AC4, AC13)
# --------------------------------------------------------------------------- #


def validate_edition_links(
    edition: NewsletterEdition, *, allowed_urls: Collection[str] | None = None
) -> None:
    """Fail the render if any published link is unsafe or not from ingestion."""
    permitted = set(allowed_urls) if allowed_urls is not None else None
    problems: list[str] = []

    for item in edition.all_items():
        try:
            validate_public_url(item.source_url)
        except ValueError as exc:
            problems.append(f"{item.article_id}: {exc}")
            continue
        if permitted is not None and item.source_url not in permitted:
            problems.append(
                f"{item.article_id}: {item.source_url} did not originate from ingestion"
            )
        for field in ("headline", "summary", "why_it_matters"):
            text = getattr(item, field)
            if "http://" in text or "https://" in text:
                problems.append(f"{item.article_id}: {field} contains a URL")

    for bullet in edition.executive_summary:
        if "http://" in bullet or "https://" in bullet:
            problems.append("executive_summary contains a URL")

    if problems:
        raise RenderError("unsafe links in edition: " + "; ".join(problems))


# --------------------------------------------------------------------------- #
# rendering
# --------------------------------------------------------------------------- #


def category_titles_for(edition: NewsletterEdition) -> dict[TopicCategory, str]:
    """Display title per category: the edition's own section titles win.

    The lead story shows its category on the band, but the lead is not inside a
    section, so the label has to come from somewhere. The edition already names
    every category it publishes; defaults cover the rest.
    """
    return {**DEFAULT_SECTION_TITLES, **{s.category: s.title for s in edition.sections}}


def render_html(edition: NewsletterEdition, *, tagline: str = "") -> str:
    return (
        build_environment()
        .get_template(HTML_TEMPLATE)
        .render(
            edition=edition,
            tagline=tagline,
            category_titles=category_titles_for(edition),
        )
    )


def render_markdown(edition: NewsletterEdition) -> str:
    return build_environment().get_template(MARKDOWN_TEMPLATE).render(edition=edition)


def render_json(edition: NewsletterEdition) -> str:
    return edition.model_dump_json(indent=2)


def selected_articles_payload(ranked: Sequence[RankedArticle]) -> str:
    """Full provenance for every published story: score, assessment, source."""
    rows: list[dict[str, Any]] = []
    for article in ranked:
        rows.append(
            {
                "article_id": article.article.article_id,
                "source_id": article.article.source_id,
                "source_name": article.source_name,
                "source_priority": article.source_priority,
                "canonical_url": article.article.canonical_url,
                "title": article.article.title,
                "published_at": article.article.published_at.isoformat(),
                "content_hash": article.article.content_hash,
                "final_score": article.final_score,
                "score_breakdown": _breakdown_for(article),
                "assessment": article.assessment.model_dump(mode="json"),
            }
        )
    return json.dumps(rows, indent=2, ensure_ascii=False)


def _breakdown_for(article: RankedArticle) -> dict[str, int]:
    return score_components(article.assessment, article.source_priority).as_dict()


def write_edition(
    edition: NewsletterEdition,
    output_dir: Path | str,
    *,
    ranked: Sequence[RankedArticle] | None = None,
    manifest: RunManifest | None = None,
    tagline: str = "",
    allowed_urls: Collection[str] | None = None,
) -> dict[str, Path]:
    """Validate, render and write every artifact. Returns name -> path."""
    validate_edition_links(edition, allowed_urls=allowed_urls)

    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)

    written: dict[str, Path] = {}

    def write(name: str, filename: str, content: str) -> None:
        path = directory / filename
        path.write_text(content, encoding="utf-8", newline="\n")
        written[name] = path

    write("html", HTML_FILENAME, render_html(edition, tagline=tagline))
    write("markdown", MARKDOWN_FILENAME, render_markdown(edition))
    write("json", JSON_FILENAME, render_json(edition))
    if ranked is not None:
        write("selected_articles", SELECTED_FILENAME, selected_articles_payload(ranked))
    if manifest is not None:
        write("run_manifest", MANIFEST_FILENAME, manifest.model_dump_json(indent=2))

    logger.info("wrote %d artifacts to %s", len(written), directory)
    return written
