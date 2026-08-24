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
import re
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from newsletter.config import DEFAULT_SECTION_TITLES
from newsletter.logging_setup import get_logger
from newsletter.models import (
    ISSUE_LABEL_PATTERN,
    IssueRef,
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


#: Month abbreviations, written out rather than taken from the system locale:
#: the edition must read the same on any machine that renders it.
SPANISH_MONTHS: tuple[str, ...] = (
    "ene",
    "feb",
    "mar",
    "abr",
    "may",
    "jun",
    "jul",
    "ago",
    "sep",
    "oct",
    "nov",
    "dic",
)


def issue_date(value: datetime) -> str:
    """``17 ago 2026`` -- day first, as Spanish readers expect."""
    return f"{value.day} {SPANISH_MONTHS[value.month - 1]} {value.year}"


def issue_datetime(value: datetime) -> str:
    zone = f" {value:%Z}".rstrip()
    return f"{issue_date(value)}, {value:%H:%M}{zone}"


def issue_date_end(value: datetime) -> str:
    """The last day a window actually covers.

    Windows are half-open, so ``period_end`` is midnight on the day *after* the
    edition. Printing it verbatim claims a day the edition does not include.
    This is the same instant ``DateWindow.issue_label`` uses to pick the week.
    """
    return issue_date(value - timedelta(microseconds=1))


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
    env.filters["issue_date_end"] = issue_date_end
    env.filters["issue_datetime"] = issue_datetime
    env.filters["md"] = markdown_escape
    return env


# --------------------------------------------------------------------------- #
# paging between issues
# --------------------------------------------------------------------------- #

#: An issue label that names an ISO week, so it can be said out loud.
_WEEK_LABEL_PATTERN = re.compile(r"\d{4}-W(?P<week>\d{2})")


@dataclass(frozen=True)
class IssueLink:
    """One paging control: the issue it leads to, where it lives, what it says.

    ``href`` is deliberately relative and always of the shape
    ``../<label>/newsletter.html``. That one string works in both places the
    edition is read: on disk it resolves to the sibling issue directory, and when
    the server answers ``/`` with this file it resolves to
    ``/<label>/newsletter.html``, which is a route. An absolute link would have
    to guess a host the edition does not know.
    """

    issue_label: str
    href: str
    text: str


def issue_week_text(issue_label: str) -> str:
    """``2026-W33`` -> ``Semana 33``; any other label is printed as stored.

    A bare arrow tells a reader nothing, so every enabled control names its
    destination. The masthead spells the *current* label out with its year as
    well; an arrow has less room and the year is already on the page.
    """
    match = _WEEK_LABEL_PATTERN.fullmatch(issue_label)
    return f"Semana {int(match['week'])}" if match else issue_label


def issue_link(issue: IssueRef | None) -> IssueLink | None:
    """A paging control for ``issue``, or None when there is nothing to link to.

    A label the web reader would refuse to serve is dropped rather than printed:
    linking it would put a dead address in an archived artifact, and building a
    relative path out of an unvetted label is how ``../`` gets into an href.
    """
    if issue is None:
        return None
    if not ISSUE_LABEL_PATTERN.fullmatch(issue.issue_label):
        logger.warning("issue %r cannot be linked from an edition", issue.issue_label)
        return None
    return IssueLink(
        issue_label=issue.issue_label,
        href=f"../{issue.issue_label}/{HTML_FILENAME}",
        text=issue_week_text(issue.issue_label),
    )


def issue_neighbours(
    edition: NewsletterEdition, issues: Sequence[IssueRef]
) -> tuple[IssueLink | None, IssueLink | None]:
    """The nearest older and nearest newer generated issues, as paging controls.

    ``issues`` is what the database says was published. The edition being
    rendered is folded in whether or not it is there yet -- on a first run the
    artifacts are written before the edition row is saved -- and its own period
    start is what places it, so re-printing a week that was never published still
    lands it between the right neighbours.

    The ordering is done here, in Python, rather than trusted from the backend:
    the same stored editions must produce byte-identical artifacts whichever
    database served them (AC9).
    """
    here = IssueRef(issue_label=edition.issue_label, period_start=edition.period_start)
    known: dict[str, IssueRef] = {here.issue_label: here}
    for issue in issues:
        known.setdefault(issue.issue_label, issue)

    ordered = sorted(known.values(), key=lambda issue: (issue.period_start, issue.issue_label))
    index = next(i for i, issue in enumerate(ordered) if issue.issue_label == here.issue_label)

    previous = ordered[index - 1] if index > 0 else None
    following = ordered[index + 1] if index + 1 < len(ordered) else None
    return issue_link(previous), issue_link(following)


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


def checked_submit_url(submit_url: str | None) -> str | None:
    """The submission-form URL, re-validated immediately before it is printed.

    It is the one link in the edition that does not come from ingestion, so
    ``validate_edition_links`` cannot vouch for it. Configuration validates it at
    load time; this is the second check, on the path that actually renders it, so
    no caller can print an unvalidated link by passing one directly.
    """
    if submit_url is None:
        return None
    candidate = submit_url.strip()
    if not candidate:
        return None
    try:
        return validate_public_url(candidate)
    except ValueError as exc:
        raise RenderError(f"submission form URL is not publishable: {exc}") from exc


def render_html(
    edition: NewsletterEdition,
    *,
    tagline: str = "",
    submit_url: str | None = None,
    previous_issue: IssueLink | None = None,
    next_issue: IssueLink | None = None,
) -> str:
    """Render the newspaper.

    ``previous_issue`` and ``next_issue`` come from :func:`issue_neighbours`;
    ``None`` for either renders that arrow as an unclickable, disabled control
    rather than as a link with nowhere to go.
    """
    return (
        build_environment()
        .get_template(HTML_TEMPLATE)
        .render(
            edition=edition,
            tagline=tagline,
            submit_url=checked_submit_url(submit_url),
            category_titles=category_titles_for(edition),
            previous_issue=previous_issue,
            next_issue=next_issue,
        )
    )


def render_markdown(edition: NewsletterEdition, *, submit_url: str | None = None) -> str:
    return (
        build_environment()
        .get_template(MARKDOWN_TEMPLATE)
        .render(edition=edition, submit_url=checked_submit_url(submit_url))
    )


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
    submit_url: str | None = None,
    previous_issue: IssueLink | None = None,
    next_issue: IssueLink | None = None,
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

    write(
        "html",
        HTML_FILENAME,
        render_html(
            edition,
            tagline=tagline,
            submit_url=submit_url,
            previous_issue=previous_issue,
            next_issue=next_issue,
        ),
    )
    write("markdown", MARKDOWN_FILENAME, render_markdown(edition, submit_url=submit_url))
    write("json", JSON_FILENAME, render_json(edition))
    if ranked is not None:
        write("selected_articles", SELECTED_FILENAME, selected_articles_payload(ranked))
    if manifest is not None:
        write("run_manifest", MANIFEST_FILENAME, manifest.model_dump_json(indent=2))

    logger.info("wrote %d artifacts to %s", len(written), directory)
    return written
