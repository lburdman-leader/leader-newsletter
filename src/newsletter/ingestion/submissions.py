"""Reader submissions: anyone can propose a link.

A submission is an ordinary candidate wearing no special clothes. It is fetched,
normalized, date-filtered, deduplicated, assessed and scored by exactly the same
code as an article from a configured source, and it competes for a place in the
edition on the same terms. Submitting buys **consideration, not publication**.

Three rules keep the door open without letting the newsletter be captured:

* **The submitter never talks to the model.** ``note`` is stored for humans and
  never enters a prompt. Otherwise submitting a link would be a way to write the
  analyst's instructions.
* **The URL is treated as hostile.** Scheme, host and resolved address are all
  checked before anything is fetched, so a submission cannot point the fetcher at
  a loopback service or a cloud metadata endpoint.
* **The gate is the ordinary threshold.** Approval means "scored well enough to
  compete", which is decided by the same deterministic formula as everything else.

A post is often a pointer rather than the story: 300 characters announcing
something, with a link to the announcement. Judging the pointer as though it were
the story is unfair to the submitter, so a thin submission is **enriched** -- the
page's own outbound link is followed and its text is attached for the analyst to
read. Python picks the link from the page's markup by rule; the model never
chooses what to fetch, the linked page goes through the same transport guards,
and the submitted page stays the article, keeping its title, date and URL.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from datetime import UTC, datetime
from urllib.parse import urlsplit

from scrapling import Selector

from newsletter.ingestion.base import FetchError, max_articles_for
from newsletter.ingestion.http import HttpClient, HttpError, UrllibHttpClient, private_host_reason
from newsletter.logging_setup import get_logger
from newsletter.models import (
    DateWindow,
    DiscoveredArticle,
    RawArticle,
    SourceConfig,
    Submission,
    SubmissionStatus,
    validate_public_url,
)
from newsletter.normalization.urls import canonicalize_url, dedupe_key

logger = get_logger("ingestion.submissions")

#: Below this much readable text, a page is treated as a pointer rather than a story.
DEFAULT_MIN_TEXT_CHARS = 600
#: How many of the page's outbound links to try before giving up.
DEFAULT_MAX_LINK_HOPS = 3
#: How much linked material to attach. Enough for an announcement, not a book.
DEFAULT_MAX_LINKED_CHARS = 8_000

#: Paths that are never the story a post points at.
_UNINTERESTING_SUFFIXES = (
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".webp",
    ".svg",
    ".mp4",
    ".mov",
    ".pdf",
    ".zip",
)


class SubmissionRejected(ValueError):
    """The submitted URL cannot be accepted at all."""


def submission_id_for(url: str) -> str:
    """Stable id derived from the URL, so resubmitting updates instead of piling up.

    It is the same derivation as ``article_id``, which means a submission and the
    article it becomes usually share an identifier.
    """
    return hashlib.sha256(dedupe_key(url).encode("utf-8")).hexdigest()[:16]


def check_submitted_url(
    url: str,
    *,
    require_https: bool = True,
    blocked_hosts: Sequence[str] = (),
    check_address: bool = True,
) -> str:
    """Validate and canonicalize a submitted URL, or raise :class:`SubmissionRejected`."""
    try:
        canonical = canonicalize_url(url)
    except ValueError as exc:
        raise SubmissionRejected(str(exc)) from exc

    if require_https and not canonical.startswith("https://"):
        raise SubmissionRejected("only https links are accepted")

    host = (urlsplit(canonical).hostname or "").lower()
    for blocked in blocked_hosts:
        blocked = blocked.strip().lower().lstrip(".")
        if blocked and (host == blocked or host.endswith("." + blocked)):
            raise SubmissionRejected(f"{host} is not accepted")

    if check_address:
        reason = private_host_reason(canonical)
        if reason:
            raise SubmissionRejected(reason)

    return canonical


def create_submission(
    url: str,
    *,
    submitted_by: str | None = None,
    note: str | None = None,
    now: datetime | None = None,
    require_https: bool = True,
    blocked_hosts: Sequence[str] = (),
    check_address: bool = True,
) -> Submission:
    """Build a pending submission from a raw, untrusted URL."""
    canonical = check_submitted_url(
        url,
        require_https=require_https,
        blocked_hosts=blocked_hosts,
        check_address=check_address,
    )
    return Submission(
        submission_id=submission_id_for(canonical),
        url=canonical,
        submitted_at=now or datetime.now(UTC),
        submitted_by=(submitted_by or "").strip()[:80] or None,
        note=(note or "").strip()[:500] or None,
        status=SubmissionStatus.PENDING,
    )


#: Hosts that are the same publication under two names.
HOST_ALIASES: dict[str, str] = {"twitter.com": "x.com"}


def registrable_host(url: str) -> str:
    """The last two labels of the host, as a cheap stand-in for the site.

    Good enough to tell ``support.x.com`` from ``openai.com`` without a public
    suffix list. On a multi-part TLD it over-matches (``bbc.co.uk`` collapses to
    ``co.uk``), which errs towards skipping a link rather than wandering onto an
    unrelated site -- the safe direction.
    """
    host = (urlsplit(url).hostname or "").lower().removeprefix("www.")
    labels = host.split(".")
    site = ".".join(labels[-2:]) if len(labels) > 2 else host
    return HOST_ALIASES.get(site, site)


def outbound_links(
    page: Selector, source_url: str, *, blocked_hosts: Sequence[str] = ()
) -> list[str]:
    """Links on the page that point at a different site, in document order.

    Redirectors are kept deliberately: a post's outbound link is usually wrapped
    (``t.co/...``), and the transport follows redirects, so the wrapper resolves
    to the real destination and ``final_url`` records where it landed.
    """
    own_site = registrable_host(source_url)
    blocked = {host.strip().lower().removeprefix(".") for host in blocked_hosts if host.strip()}

    seen: set[str] = set()
    found: list[str] = []
    for anchor in page.css("a"):
        href = anchor.attrib.get("href")
        if not href:
            continue
        try:
            candidate = canonicalize_url(page.urljoin(str(href).strip()))
        except ValueError:
            continue

        site = registrable_host(candidate)
        if not site or site == own_site or site in blocked:
            continue
        if urlsplit(candidate).path.lower().endswith(_UNINTERESTING_SUFFIXES):
            continue
        if candidate in seen:
            continue
        seen.add(candidate)
        found.append(candidate)
    return found


def readable_length(html: str, source: SourceConfig | None = None) -> int:
    """How much text the page will actually contribute as an article.

    It must ask the same question normalization asks. Measuring the whole
    ``<body>`` instead counts navigation, footers and sidebars, so a post with
    300 characters of content inside 1,700 characters of page furniture looks
    substantial and is never enriched -- which is exactly the case this feature
    exists for.
    """
    from newsletter.normalization.article import extract_text

    reference = source or SourceConfig(
        id="probe", name="probe", entrypoint="https://probe.invalid/", strategy="rss", priority=0
    )
    return len(extract_text(Selector(html), reference))


class SubmissionAdapter:
    """Presents pending submissions through the ordinary source interface."""

    def __init__(
        self,
        source: SourceConfig,
        submissions: Sequence[Submission],
        *,
        http: HttpClient | None = None,
        follow_links: bool = True,
        min_text_chars: int = DEFAULT_MIN_TEXT_CHARS,
        max_link_hops: int = DEFAULT_MAX_LINK_HOPS,
        max_linked_chars: int = DEFAULT_MAX_LINKED_CHARS,
        blocked_hosts: Sequence[str] = (),
    ) -> None:
        self.source = source
        self.submissions = list(submissions)
        self.http = http or UrllibHttpClient()
        self.follow_links = follow_links
        self.min_text_chars = min_text_chars
        self.max_link_hops = max_link_hops
        self.max_linked_chars = max_linked_chars
        self.blocked_hosts = list(blocked_hosts)
        #: Maps the URL handed to the pipeline back to its submission.
        self.by_url: dict[str, Submission] = {}

    def discover(self, window: DateWindow) -> list[DiscoveredArticle]:
        """Every pending submission, in submission order.

        No publication-date hint is offered: a submitter does not get to assert
        when something was published. The date must come from the page itself,
        and an article without one is rejected during normalization like any
        other.
        """
        limit = max_articles_for(self.source)
        discovered: list[DiscoveredArticle] = []

        for submission in self.submissions:
            if submission.status is not SubmissionStatus.PENDING:
                continue
            if len(discovered) >= limit:
                logger.warning(
                    "submission cap of %d reached; %d left for the next run",
                    limit,
                    len(self.submissions) - len(discovered),
                )
                break
            try:
                candidate = DiscoveredArticle(source_id=self.source.id, url=submission.url)
            except ValueError as exc:  # pragma: no cover - the URL was validated on submit
                logger.warning("skipping unusable submission %s: %s", submission.submission_id, exc)
                continue
            self.by_url[candidate.url] = submission
            discovered.append(candidate)

        logger.info("%d pending submissions offered to this run", len(discovered))
        return discovered

    def fetch(self, article: DiscoveredArticle) -> RawArticle:
        # The address guard lives in the transport, which enforces it on every
        # request; duplicating it here would mean two places to keep correct.
        try:
            validate_public_url(article.url)
        except ValueError as exc:
            raise FetchError(self.source.id, f"unsafe submitted URL: {exc}") from exc

        try:
            response = self.http.get(article.url)
        except HttpError as exc:
            raise FetchError(self.source.id, f"could not fetch {article.url}: {exc}") from exc

        metadata: dict[str, object] = {**response.metadata, "origin": "submission"}
        linked_url, linked_text = None, None

        if self.follow_links and readable_length(response.text, self.source) < self.min_text_chars:
            linked_url, linked_text = self._follow(response.text, response.final_url)
            if linked_url:
                metadata["linked_from"] = linked_url

        return RawArticle(
            source_id=self.source.id,
            url=article.url,
            final_url=response.final_url,
            raw_content=response.text,
            retrieved_at=datetime.now(UTC),
            content_type=response.content_type,
            http_metadata=metadata,
            linked_url=linked_url,
            linked_text=linked_text,
        )

    def _follow(self, html: str, source_url: str) -> tuple[str | None, str | None]:
        """Fetch the first outbound link that carries real text.

        A failure here is never fatal: the submission is simply judged on the
        thin page it already has.
        """
        page = Selector(html, url=source_url)
        candidates = outbound_links(page, source_url, blocked_hosts=self.blocked_hosts)

        for candidate in candidates[: self.max_link_hops]:
            try:
                linked = self.http.get(candidate)
            except HttpError as exc:
                logger.info("linked page %s could not be read: %s", candidate, exc)
                continue

            text = extract_linked_text(linked.text)
            if len(text) < self.min_text_chars:
                logger.info("linked page %s is thin too (%d chars)", linked.final_url, len(text))
                continue

            logger.info(
                "enriched a thin submission with %d chars from %s", len(text), linked.final_url
            )
            return linked.final_url, text[: self.max_linked_chars]

        return None, None


def extract_linked_text(html: str) -> str:
    """Readable text from a linked page, using the same extraction as an article."""
    from newsletter.normalization.article import extract_text

    reference = SourceConfig(
        id="probe", name="probe", entrypoint="https://probe.invalid/", strategy="rss", priority=0
    )
    return extract_text(Selector(html), reference)
