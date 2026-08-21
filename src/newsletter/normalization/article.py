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
* **The body is the article; the page is not.** A site's navigation, its footer
  and its "latest stories" rail are the same on every page it serves, so
  admitting them makes two unrelated stories from one outlet look like one story
  and hands the analyst other headlines to judge. :func:`extract_text` keeps the
  body and drops the furniture, by measurement rather than by site-specific rule.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from datetime import datetime
from functools import lru_cache
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

#: A page that titles itself by its account -- "Grok (@grok) on X" -- is naming
#: the author, not the story. Social platforms do this on every post.
_ACCOUNT_TITLE = re.compile(r"^.{0,60}\(@[\w.]{1,30}\)\s+on\s+\S+$", re.IGNORECASE)
_HANDLE_TITLE = re.compile(r"^@[\w.]{1,30}\s+on\s+\S+$", re.IGNORECASE)

#: The other social shape: 'Grok on X: "the actual post ..."'. The wrapper is
#: furniture; the quoted part is the story.
_QUOTED_TITLE = re.compile(
    r"^.{1,60}?\s+on\s+[^:]{1,30}:\s*[\"\u201c\u2018']\s*(?P<body>.+)$", re.DOTALL
)

#: A fallback headline stops here, on a word boundary.
MAX_FALLBACK_HEADLINE = 120
#: Below this a single sentence is too terse to stand as a headline.
MIN_FALLBACK_HEADLINE = 40

#: Upper bound on how much embedded ``<script>`` text is scanned for a date.
#: Payload scripts on a data-driven page run to a few hundred KB; anything past
#: this is refused rather than scanned, so a hostile or runaway page cannot turn
#: date extraction into unbounded work.
MAX_EMBEDDED_SCAN_CHARS = 1_000_000

#: Longest plausible serialized timestamp. Bounds the value group so the pattern
#: stays linear on adversarial input.
MAX_EMBEDDED_DATE_CHARS = 64

#: Containers tried in order when a source declares no content selector.
CONTENT_SELECTORS: tuple[str, ...] = (
    "article",
    "main",
    "[role='main']",
    "[itemprop='articleBody']",
    ".article-body",
    ".post-content",
    "body",
)

#: At most this many matches of one selector are measured. A page that repeats
#: ``<article>`` for every teaser in a rail would otherwise make container choice
#: proportional to the size of the rail; the body is never the twentieth one.
MAX_CONTAINER_MATCHES = 20

#: How small a container may be, relative to the largest match of the same
#: selector, and still win on document order. Two shapes have to work at once: a
#: blog whose first ``<article>`` is a 130-character teaser card and whose body
#: is the tenth one, and a social thread whose first ``<article>`` is the post
#: that was submitted and whose neighbours are replies of a similar size. Reading
#: order settles the second case and size settles the first, so size only
#: overrules order when the difference is not a matter of degree.
MIN_CONTAINER_SHARE = 0.5

#: Elements that never carry article body, whatever they contain: the page's
#: furniture (``nav``, ``aside``, ``footer``, ``header``), its behaviour
#: (``script``, ``form``, ``button``, ``template``), and things that are not text
#: at all (``svg``, ``iframe``). Their text is dropped wherever they appear,
#: including inside the chosen container -- a "latest stories" rail nested in an
#: ``<article>`` is still a rail.
NON_CONTENT_TAGS: tuple[str, ...] = (
    "script",
    "style",
    "nav",
    "aside",
    "footer",
    "header",
    "form",
    "iframe",
    "noscript",
    "svg",
    "template",
    "button",
    "select",
    "dialog",
)
_NON_CONTENT = frozenset(NON_CONTENT_TAGS)

#: Elements that hold one unit of prose. Their text is taken whole and they are
#: never judged by link density: a paragraph that cites four sources is still a
#: paragraph, and dropping it would lose real body.
PROSE_TAGS = frozenset(
    {
        "p",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "li",
        "blockquote",
        "pre",
        "figcaption",
        "dd",
        "dt",
        "td",
        "th",
        "caption",
        "summary",
    }
)

#: Elements that live *inside* a line of prose. A node whose only element
#: children are inline is a single line of text, so its text is taken in one
#: piece rather than split at every ``<a>`` and ``<em>``.
INLINE_TAGS = frozenset(
    {
        "a",
        "abbr",
        "b",
        "bdi",
        "bdo",
        "br",
        "cite",
        "code",
        "data",
        "del",
        "dfn",
        "em",
        "i",
        "img",
        "ins",
        "kbd",
        "label",
        "mark",
        "picture",
        "q",
        "s",
        "samp",
        "small",
        "source",
        "span",
        "strong",
        "sub",
        "sup",
        "time",
        "u",
        "var",
        "wbr",
    }
)

#: Above this share of linked characters a block is navigation, not prose. Body
#: paragraphs link out but are overwhelmingly their own words; a "latest stories"
#: rail is almost nothing but other headlines. Measured on captured pages, real
#: bodies sit near zero and rails sit above 0.9, so the boundary is deliberately
#: placed well clear of the prose end of that gap.
MAX_LINK_DENSITY = 0.6

#: Link density is only evidence once there is enough text to measure. Below
#: this a block is a caption, a tag or a "read more", and whichever way it is
#: judged the edition reads the same.
MIN_CHROME_CHARS = 60

#: How deep into a page the body walk descends before it stops looking for
#: chrome and simply keeps what it finds. Real markup nests a few dozen levels;
#: a page nested past this is either broken or hostile, and either way the walk
#: must end in text rather than in a ``RecursionError`` that would take the whole
#: run down with it (CLAUDE.md rule 7).
MAX_TREE_DEPTH = 200

#: How ``Selector.xpath("node()")`` reports a bare text node.
_TEXT_NODE = "#text"


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


def is_account_title(title: str) -> bool:
    """True when a title names the account rather than the story."""
    collapsed = collapse_inline_whitespace(title)
    return bool(_ACCOUNT_TITLE.match(collapsed) or _HANDLE_TITLE.match(collapsed))


def unwrap_social_title(title: str) -> str:
    """Strip the 'Someone on Platform: "..."' wrapper social sites put on posts."""
    match = _QUOTED_TITLE.match(title.strip())
    if not match:
        return title
    body = match.group("body").strip()
    return body.rstrip("\u201d\u2019\"'").rstrip(" .\u2026").strip() or title


def headline_from_prose(text: str) -> str:
    """A headline-sized opening from body prose, cut on a sentence or a word.

    Used only as a fallback, when the page offers no headline of its own. It has
    to be defensible rather than clever: the editorial pass normally rewrites it,
    and when that pass fails this is what gets printed.
    """
    prose = collapse_inline_whitespace(unwrap_social_title(text))
    if len(prose) <= MIN_FALLBACK_HEADLINE:
        return prose

    taken = ""
    for sentence in re.split(r"(?<=[.!?])\s+", prose):
        taken = f"{taken} {sentence}".strip() if taken else sentence
        if len(taken) >= MIN_FALLBACK_HEADLINE:
            break

    if len(taken) <= MAX_FALLBACK_HEADLINE:
        return taken
    clipped = taken[:MAX_FALLBACK_HEADLINE].rsplit(" ", 1)[0].rstrip(" ,;:")
    return f"{clipped}\u2026"


def extract_title(page: Selector, hint: DiscoveredArticle | None) -> str | None:
    candidates: list[str] = []
    for selector, attribute in (
        ("meta[property='og:title']", "content"),
        ("meta[name='twitter:title']", "content"),
    ):
        value = _first_attr(page, selector, attribute)
        if value:
            candidates.append(value)

    for document in _json_ld_documents(page):
        headline = document.get("headline")
        if isinstance(headline, str) and headline.strip():
            candidates.append(headline)

    for selector in ("h1", "title"):
        value = _first_text(page, selector)
        if value:
            candidates.append(value)

    if hint and hint.title_hint:
        candidates.append(hint.title_hint)

    for value in candidates:
        if not is_account_title(value):
            return headline_from_prose(unwrap_social_title(value))

    # Every title on the page names the account. The description carries what was
    # actually posted, which is the nearest thing to a headline the page has.
    for selector, attribute in (
        ("meta[property='og:description']", "content"),
        ("meta[name='description']", "content"),
    ):
        description = _first_attr(page, selector, attribute)
        if description:
            return headline_from_prose(description)

    return collapse_inline_whitespace(candidates[0]) if candidates else None


@lru_cache(maxsize=32)
def _embedded_date_pattern(key: str) -> re.Pattern[str]:
    """Match ``"key": "value"`` whether or not the quotes are backslash-escaped.

    A framework that streams its data as a JavaScript string literal ships the
    payload double-encoded, so the same key appears on the wire as ``\\"key\\"``
    rather than ``"key"``. One optional backslash before each quote covers both
    without a second pass over the text.

    The alternation also matches an *unquoted* value (``null``, a bare number).
    That branch never yields a date -- it exists so a key whose value stopped
    being a string is seen and refused here, instead of the scan sliding on to
    the next occurrence of the key and returning some other record's date.
    """
    escaped = re.escape(key)
    return re.compile(
        rf'\\?"{escaped}\\?"\s*:\s*'
        rf'(?:\\?"(?P<quoted>[^"\\]{{0,{MAX_EMBEDDED_DATE_CHARS}}})\\?"'
        rf"|(?P<bare>[A-Za-z0-9_.+-]{{1,{MAX_EMBEDDED_DATE_CHARS}}}))"
    )


def extract_embedded_date(page: Selector, key: str) -> datetime | None:
    """Publication date from a JSON key inside an embedded ``<script>`` payload.

    For sites that render from a client-side data blob and expose no date in the
    markup: no ``article:published_time``, no JSON-LD, no ``<time datetime>``.
    The date is in the page, just not anywhere a CSS selector can reach.

    The payload is untrusted data and is treated as such. It is never evaluated
    and never deserialized -- only scanned, under a character budget, with a
    bounded pattern -- and whatever comes back is validated through
    :func:`~newsletter.ingestion.dates.parse_datetime` like any other date. A
    string that is not a real timestamp yields ``None``, same as a missing key.

    **The first match wins**, which is what makes this usable on an article page
    and not on an index page. An article page leads with its own record, so the
    first occurrence of the key is that article's date; the later ones belong to
    related-post teasers further down the payload. An index page carries one
    record per listed item with no such privileged position, so first-match would
    hand every item the first item's date. Hence this runs during normalization,
    against a single article, and discovery does not use it.
    """
    pattern = _embedded_date_pattern(key)
    remaining = MAX_EMBEDDED_SCAN_CHARS

    for node in page.css("script"):
        text = str(node.text or "")
        if not text:
            continue
        if remaining <= 0:
            logger.warning("embedded date scan hit its %d-char budget", MAX_EMBEDDED_SCAN_CHARS)
            break
        match = pattern.search(text[:remaining])
        remaining -= len(text)
        if match is None:
            continue
        # Found the key. Whatever it holds is the answer, right or wrong: reading
        # past it would silently pick up a neighbouring record's date.
        return parse_datetime(match.group("quoted"))

    return None


def extract_published_at(
    page: Selector,
    hint: DiscoveredArticle | None,
    source: SourceConfig | None = None,
) -> datetime | None:
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

    # After the standard routes, never before them: a site that publishes a real
    # metadata contract is more trustworthy than its own rendering internals, so
    # this can only add a date, never override a correctly declared one.
    if source is not None and source.embedded_date_key:
        parsed = extract_embedded_date(page, source.embedded_date_key)
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


def _subtree_text(node: Selector) -> str:
    """One line of text for a subtree, with non-content elements left out.

    Joined with no separator and re-collapsed rather than joined on newlines, so
    the markup's own spacing is what survives: ``<p>Hello <b>world</b>.</p>``
    reads back as ``Hello world.`` and not as three fragments.
    """
    raw = node.get_all_text(
        separator="",
        strip=False,
        ignore_tags=NON_CONTENT_TAGS,
        valid_values=False,
    )
    return collapse_inline_whitespace(str(raw))


def _linked_chars(node: Selector) -> int:
    """Characters of ``node`` that sit inside a link.

    Counted by the same rules :func:`_subtree_text` measures by -- non-content
    elements skipped, a nested link counted once -- because a ratio whose two
    halves disagree about what text exists is not a ratio. Counting an anchor
    inside a ``<header>`` whose text was already excluded is what would push a
    plain article body past any threshold.

    Walked with an explicit stack rather than by recursion: this runs on every
    block of every page, and a page nested deeply enough to exhaust the
    interpreter's stack must cost its own article at most, never the run.
    """
    total = 0
    pending = [node]
    while pending:
        current = pending.pop()
        tag = current.tag
        if not tag or tag.startswith("#") or tag in _NON_CONTENT:
            continue
        if tag == "a":
            total += len(_subtree_text(current))
            continue
        pending.extend(current.children)
    return total


def link_density(node: Selector, text: str | None = None) -> float:
    """Share of a block's characters that sit inside a link, in ``[0, 1]``.

    The one measurement that separates a navigation rail from an article: a rail
    is other stories' headlines, and a headline is a link. ``text`` may be passed
    in when the caller already has it, so a block is never read twice.
    """
    body = _subtree_text(node) if text is None else text
    if not body:
        return 0.0
    return min(_linked_chars(node) / len(body), 1.0)


def _is_chrome(node: Selector, text: str) -> bool:
    """True when a block reads as page furniture rather than as body text."""
    return len(text) >= MIN_CHROME_CHARS and link_density(node, text) >= MAX_LINK_DENSITY


def _collect_body(node: Selector, out: list[str], depth: int = 0) -> None:
    """Append the article-body fragments of ``node``'s subtree to ``out``.

    Depth-first in document order, dropping two things and nothing else: any
    :data:`NON_CONTENT_TAGS` element, and any non-prose block whose link density
    marks it as navigation. Everything else is kept -- when in doubt the text
    stays, because a lost paragraph silently degrades every later judgement while
    a surviving scrap of chrome merely adds noise.
    """
    tag = node.tag
    if tag == _TEXT_NODE:
        text = collapse_inline_whitespace(str(node))
        if text:
            out.append(text)
        return
    if not tag or tag.startswith("#") or tag in _NON_CONTENT:
        return  # a comment, a processing instruction, or page furniture

    text = _subtree_text(node)
    if not text:
        return

    is_prose = tag in PROSE_TAGS
    if not is_prose and _is_chrome(node, text):
        return

    blocks = [child for child in node.children if child.tag not in INLINE_TAGS]
    if not blocks or depth >= MAX_TREE_DEPTH:
        out.append(text)  # a single line: its own words plus inline markup
        return

    for child in node.xpath("node()"):
        _collect_body(child, out, depth + 1)


def body_text(container: Selector) -> str:
    """The article body inside one container, with page chrome removed.

    The container itself is never judged -- it was chosen as the body, and the
    filtering happens strictly below it.
    """
    fragments: list[str] = []
    for child in container.xpath("node()"):
        _collect_body(child, fragments)
    return normalize_text("\n".join(fragments))


def extract_text(page: Selector, source: SourceConfig) -> str:
    """Article body text: the narrowest container that holds it, minus the chrome.

    Two deterministic steps. First a container is chosen -- the source's own
    selector when it declares one, then the semantic containers a page states for
    itself (``<article>``, ``<main>``, ``[role='main']``), and only then the whole
    ``<body>``, which is the fallback for a page that names nothing. Where a
    selector matches several times, the first match wins unless it is less than
    :data:`MIN_CONTAINER_SHARE` of the largest one, in which case it was a teaser
    card and the largest is the story.

    Then the chrome inside that container is dropped: non-content elements
    outright, link-dense blocks by measurement. Both passes read the tree without
    modifying it, so every later extractor still sees the page as fetched, and
    identical HTML always yields identical text (AC9).
    """
    configured = source.selectors.get("content")
    candidates = (configured, *CONTENT_SELECTORS) if configured else CONTENT_SELECTORS

    best = ""
    for selector in candidates:
        if not selector:
            continue
        matches = page.css(selector)
        if not len(matches):
            continue
        texts = [body_text(match) for match in matches[:MAX_CONTAINER_MATCHES]]
        floor = max(len(text) for text in texts) * MIN_CONTAINER_SHARE
        text = next(text for text in texts if len(text) >= floor)
        if len(text) >= MIN_TEXT_LENGTH:
            return text
        best = max(best, text, key=len)
    return best


# --------------------------------------------------------------------------- #
# normalization
# --------------------------------------------------------------------------- #


def with_linked_material(text: str, url: str | None, linked: str) -> str:
    """Append material the page linked to, labelled so its origin stays visible.

    The page itself remains the article -- its title, date and URL are what get
    published. The linked material only widens what the analyst has to judge,
    which is the difference between assessing a 300-character post and assessing
    the announcement it points at.
    """
    label = f"Material linked from this page ({url}):" if url else "Material linked from this page:"
    return "\n\n".join((text.strip(), label, normalize_text(linked))).strip()


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

    published_at = extract_published_at(page, hint, source)
    if published_at is None:
        detail = "no publication date found; refusing to invent one"
        if source.embedded_date_key:
            # Name the configured key. A source that depends on an embedded payload
            # breaks the day the site renames or restructures that key, and this
            # message in the run manifest is how an operator learns which one.
            detail = (
                f"no publication date found: embedded key "
                f"{source.embedded_date_key!r} is missing or does not hold a "
                f"valid timestamp, and the page states no date otherwise; "
                f"refusing to invent one"
            )
        raise NormalizationError(raw.source_id, raw.url, detail)

    clean_text = extract_text(page, source)
    if raw.linked_text:
        clean_text = with_linked_material(clean_text, raw.linked_url, raw.linked_text)
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
