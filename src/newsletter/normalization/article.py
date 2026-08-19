"""Turn an untrusted fetched page into a :class:`NormalizedArticle`.

This is where scraped HTML stops being HTML and becomes a typed record. Three
rules govern the module:

* **Never invent a publication date.** An article whose date cannot be
  established is rejected with a recorded error, not dated "now".
* **Attribution cannot be hijacked.** A page-declared canonical URL is only
  honoured when it points at the same site the page was fetched from; otherwise
  the fetched URL wins. Untrusted markup must not be able to redirect a story's
  credit or link to another domain.
* **Text is data, never instructions.** Everything extracted here is content to
  be analysed later; nothing in it is ever treated as a directive.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any

from scrapling import Selector

from newsletter.ingestion.dates import parse_datetime
from newsletter.logging_setup import get_logger
from newsletter.models import (
    DiscoveredArticle,
    NormalizedArticle,
    PipelineStage,
    RawArticle,
    RunManifest,
    SourceConfig,
)
from newsletter.normalization.urls import canonicalize_url, dedupe_key, same_site

logger = get_logger("normalization")

#: Below this, there is nothing an analyzer could honestly assess.
MIN_TEXT_LENGTH = 120

#: Containers tried in order when a source declares no content selector.
CONTENT_SELECTORS: tuple[str, ...] = (
    "article",
    "main",
    "[itemprop='articleBody']",
    ".article-body",
    ".post-content",
    "body",
)


class NormalizationError(Exception):
    """A fetched page could not be turned into a usable article."""

    def __init__(self, source_id: str, url: str, message: str) -> None:
        super().__init__(f"[{source_id}] {url}: {message}")
        self.source_id = source_id
        self.url = url
        self.reason = message


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _first_text(page: Selector, selector: str) -> str | None:
    matches = page.css(selector)
    if not len(matches):
        return None
    text = matches[0].get_all_text(strip=True)
    return collapse_inline_whitespace(text) or None


def _first_attr(page: Selector, selector: str, attribute: str) -> str | None:
    matches = page.css(selector)
    if not len(matches):
        return None
    value = matches[0].attrib.get(attribute)
    return str(value).strip() or None if value else None


def collapse_inline_whitespace(text: str) -> str:
    """Collapse runs of spaces inside a single line."""
    return " ".join(text.split())


def normalize_text(text: str) -> str:
    """Collapse whitespace while preserving paragraph breaks.

    Paragraph structure is kept because it carries meaning for summarisation;
    incidental whitespace is removed because it would otherwise change the
    content hash for identical content.
    """
    lines = (collapse_inline_whitespace(line) for line in text.splitlines())
    return "\n".join(line for line in lines if line)


def compute_content_hash(text: str) -> str:
    """Stable SHA-256 over normalized text. Identical content, identical hash."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def compute_article_id(canonical_url: str) -> str:
    """Deterministic identifier derived from the comparison key of the URL."""
    return hashlib.sha256(dedupe_key(canonical_url).encode("utf-8")).hexdigest()[:16]


def _json_ld_documents(page: Selector) -> list[dict[str, Any]]:
    """Every JSON-LD object on the page, flattened out of @graph and lists."""
    documents: list[dict[str, Any]] = []
    for node in page.css("script[type='application/ld+json']"):
        raw = str(node.text or "").strip()
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
        pending = payload if isinstance(payload, list) else [payload]
        while pending:
            item = pending.pop(0)
            if not isinstance(item, dict):
                continue
            graph = item.get("@graph")
            if isinstance(graph, list):
                pending.extend(graph)
            documents.append(item)
    return documents


# --------------------------------------------------------------------------- #
# field extraction
# --------------------------------------------------------------------------- #


def extract_canonical_url(page: Selector, raw: RawArticle) -> str:
    """Canonical URL, refusing cross-site claims from untrusted markup."""
    for selector, attribute in (
        ("link[rel='canonical']", "href"),
        ("meta[property='og:url']", "content"),
    ):
        declared = _first_attr(page, selector, attribute)
        if not declared:
            continue
        if not same_site(declared, raw.final_url):
            logger.warning(
                "%s: ignoring cross-site canonical %r on %s",
                raw.source_id,
                declared,
                raw.final_url,
            )
            continue
        try:
            return canonicalize_url(declared)
        except ValueError:
            continue
    return canonicalize_url(raw.final_url)


def extract_title(page: Selector, hint: DiscoveredArticle | None) -> str | None:
    for selector, attribute in (
        ("meta[property='og:title']", "content"),
        ("meta[name='twitter:title']", "content"),
    ):
        value = _first_attr(page, selector, attribute)
        if value:
            return collapse_inline_whitespace(value)

    for document in _json_ld_documents(page):
        headline = document.get("headline")
        if isinstance(headline, str) and headline.strip():
            return collapse_inline_whitespace(headline)

    for selector in ("h1", "title"):
        value = _first_text(page, selector)
        if value:
            return value

    return collapse_inline_whitespace(hint.title_hint) if hint and hint.title_hint else None


def extract_published_at(page: Selector, hint: DiscoveredArticle | None) -> datetime | None:
    """The page states its own date; the feed hint is the fallback. Never invented."""
    for selector, attribute in (
        ("meta[property='article:published_time']", "content"),
        ("meta[name='article:published_time']", "content"),
        ("meta[itemprop='datePublished']", "content"),
        ("meta[name='date']", "content"),
        ("meta[name='pubdate']", "content"),
    ):
        parsed = parse_datetime(_first_attr(page, selector, attribute))
        if parsed is not None:
            return parsed

    for document in _json_ld_documents(page):
        for key in ("datePublished", "dateCreated"):
            parsed = parse_datetime(
                document.get(key) if isinstance(document.get(key), str) else None
            )
            if parsed is not None:
                return parsed

    for node in page.css("time"):
        parsed = parse_datetime(node.attrib.get("datetime"))
        if parsed is not None:
            return parsed

    return hint.published_at_hint if hint else None


def extract_author(page: Selector) -> str | None:
    for selector, attribute in (
        ("meta[name='author']", "content"),
        ("meta[property='article:author']", "content"),
    ):
        value = _first_attr(page, selector, attribute)
        if value and not value.startswith(("http://", "https://")):
            return collapse_inline_whitespace(value)

    for document in _json_ld_documents(page):
        author = document.get("author")
        if isinstance(author, dict) and isinstance(author.get("name"), str):
            return collapse_inline_whitespace(author["name"])
        if isinstance(author, list) and author and isinstance(author[0], dict):
            name = author[0].get("name")
            if isinstance(name, str):
                return collapse_inline_whitespace(name)
        if isinstance(author, str) and author.strip():
            return collapse_inline_whitespace(author)

    for selector in ("[rel='author']", ".byline", ".author"):
        value = _first_text(page, selector)
        if value:
            return value
    return None


def extract_text(page: Selector, source: SourceConfig) -> str:
    """Article body text. Scrapling excludes script and style content already."""
    configured = source.selectors.get("content")
    candidates = (configured, *CONTENT_SELECTORS) if configured else CONTENT_SELECTORS

    best = ""
    for selector in candidates:
        if not selector:
            continue
        matches = page.css(selector)
        if not len(matches):
            continue
        text = normalize_text(matches[0].get_all_text(strip=True))
        if len(text) >= MIN_TEXT_LENGTH:
            return text
        best = max(best, text, key=len)
    return best


# --------------------------------------------------------------------------- #
# normalization
# --------------------------------------------------------------------------- #


def normalize_article(
    raw: RawArticle,
    source: SourceConfig,
    *,
    hint: DiscoveredArticle | None = None,
) -> NormalizedArticle:
    """Build a :class:`NormalizedArticle`, or raise :class:`NormalizationError`."""
    page = Selector(raw.raw_content, url=raw.final_url)

    try:
        canonical_url = extract_canonical_url(page, raw)
    except ValueError as exc:
        raise NormalizationError(raw.source_id, raw.url, f"unusable canonical URL: {exc}") from exc

    title = extract_title(page, hint)
    if not title:
        raise NormalizationError(raw.source_id, raw.url, "no title could be extracted")

    published_at = extract_published_at(page, hint)
    if published_at is None:
        raise NormalizationError(
            raw.source_id, raw.url, "no publication date found; refusing to invent one"
        )

    clean_text = extract_text(page, source)
    if len(clean_text) < MIN_TEXT_LENGTH:
        raise NormalizationError(
            raw.source_id,
            raw.url,
            f"extracted text is too short to assess ({len(clean_text)} chars)",
        )

    return NormalizedArticle(
        article_id=compute_article_id(canonical_url),
        source_id=raw.source_id,
        canonical_url=canonical_url,
        origin_url=raw.url if raw.url != canonical_url else None,
        title=title,
        published_at=published_at,
        author=extract_author(page),
        clean_text=clean_text,
        content_hash=compute_content_hash(clean_text),
        retrieved_at=raw.retrieved_at,
    )


def normalize_all(
    raw_articles: Iterable[RawArticle],
    sources_by_id: Mapping[str, SourceConfig],
    *,
    manifest: RunManifest,
    hints: Mapping[str, DiscoveredArticle] | None = None,
) -> list[NormalizedArticle]:
    """Normalize every fetched page, recording and skipping the ones that fail."""
    hint_map = hints or {}
    normalized: list[NormalizedArticle] = []

    for raw in raw_articles:
        source = sources_by_id.get(raw.source_id)
        if source is None:  # pragma: no cover - configuration guarantees this
            manifest.record_error(
                PipelineStage.NORMALIZE,
                KeyError(f"unknown source {raw.source_id!r}"),
                source_id=raw.source_id,
            )
            continue
        try:
            normalized.append(normalize_article(raw, source, hint=hint_map.get(raw.url)))
        except (NormalizationError, ValueError) as exc:
            manifest.record_error(PipelineStage.NORMALIZE, exc, source_id=raw.source_id)
            logger.warning("normalization failed: %s", exc)

    return normalized
