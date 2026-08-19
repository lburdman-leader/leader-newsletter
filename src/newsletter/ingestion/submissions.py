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
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from datetime import UTC, datetime
from urllib.parse import urlsplit

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


class SubmissionAdapter:
    """Presents pending submissions through the ordinary source interface."""

    def __init__(
        self,
        source: SourceConfig,
        submissions: Sequence[Submission],
        *,
        http: HttpClient | None = None,
    ) -> None:
        self.source = source
        self.submissions = list(submissions)
        self.http = http or UrllibHttpClient()
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

        return RawArticle(
            source_id=self.source.id,
            url=article.url,
            final_url=response.final_url,
            raw_content=response.text,
            retrieved_at=datetime.now(UTC),
            content_type=response.content_type,
            http_metadata={**response.metadata, "origin": "submission"},
        )
