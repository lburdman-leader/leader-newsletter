"""URL canonicalization.

Two functions with deliberately different appetites for risk:

:func:`canonicalize_url`
    Produces the URL that gets **published**. It only removes things that cannot
    change which page you land on: fragments, analytics parameters, default
    ports, case in the scheme and host. A published link that 404s is worse than
    a duplicate story, so this function stays conservative.

:func:`dedupe_key`
    Produces a key used only for **comparison**. It is free to be aggressive --
    dropping ``www.``, trailing slashes and ``index.html`` -- because the key is
    never shown to anyone and never followed.

Both are pure and total: same input, same output, no network.
"""

from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from newsletter.models import validate_public_url

#: Parameter *prefixes* that are unambiguously analytics.
TRACKING_PREFIXES: tuple[str, ...] = ("utm_", "at_", "pk_", "piwik_", "matomo_")

#: Exact parameter names that are unambiguously analytics or share-tracking.
TRACKING_PARAMS: frozenset[str] = frozenset(
    {
        "fbclid",
        "gclid",
        "dclid",
        "gbraid",
        "wbraid",
        "msclkid",
        "yclid",
        "twclid",
        "ttclid",
        "igshid",
        "igsh",
        "mc_cid",
        "mc_eid",
        "_hsenc",
        "_hsmi",
        "vero_id",
        "oly_enc_id",
        "oly_anon_id",
        "ref_src",
        "ref_url",
        "cmpid",
        "ncid",
        "sr_share",
        "share_id",
        "si",  # YouTube share identifier
        "s_kwcid",
        "trk",
        "trkCampaign",
        "__twitter_impression",
    }
)

DEFAULT_PORTS: dict[str, str] = {"http": "80", "https": "443"}

#: Filenames that mean "the directory itself".
INDEX_FILENAMES: frozenset[str] = frozenset(
    {"index.html", "index.htm", "index.php", "default.html", "default.htm"}
)


def is_tracking_param(name: str) -> bool:
    """True when a query parameter carries analytics rather than content."""
    lowered = name.lower()
    return lowered in TRACKING_PARAMS or lowered.startswith(TRACKING_PREFIXES)


def strip_tracking_params(query: str) -> str:
    """Drop analytics parameters and sort the rest for a stable ordering."""
    if not query:
        return ""
    kept = [
        (name, value)
        for name, value in parse_qsl(query, keep_blank_values=True)
        if not is_tracking_param(name)
    ]
    if not kept:
        return ""
    kept.sort(key=lambda pair: (pair[0], pair[1]))
    return urlencode(kept, doseq=False)


def canonicalize_url(url: str) -> str:
    """Return the publishable canonical form of ``url``.

    Raises :class:`ValueError` for anything that is not an absolute http(s) URL,
    so a bad link can never reach an edition.
    """
    validated = validate_public_url(url)
    parts = urlsplit(validated)

    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    if not host:
        raise ValueError(f"URL has no host: {url!r}")

    # An IPv6 literal keeps its brackets, or the rebuilt URL is malformed.
    netloc = f"[{host}]" if ":" in host else host
    if parts.port is not None and str(parts.port) != DEFAULT_PORTS.get(scheme):
        netloc = f"{netloc}:{parts.port}"
    if parts.username:
        credentials = parts.username
        if parts.password:
            credentials = f"{credentials}:{parts.password}"
        netloc = f"{credentials}@{netloc}"

    return urlunsplit((scheme, netloc, parts.path, strip_tracking_params(parts.query), ""))


def dedupe_key(url: str) -> str:
    """Comparison key: the canonical URL, normalized further for matching only.

    Additionally folds ``www.``, a trailing slash and an index filename, because
    ``https://a.example/news/`` and ``https://www.a.example/news/index.html`` are
    the same story.
    """
    parts = urlsplit(canonicalize_url(url))

    host = parts.netloc.removeprefix("www.")

    path = parts.path
    tail = path.rsplit("/", 1)[-1]
    if tail.lower() in INDEX_FILENAMES:
        path = path[: -len(tail)]
    path = path.rstrip("/")

    return urlunsplit((parts.scheme, host, path, parts.query, ""))


def same_site(first: str, second: str) -> bool:
    """True when two URLs share a registrable-ish host (ignoring ``www.``).

    Used to decide whether a page-declared canonical URL can be trusted: an
    untrusted page must not be able to redirect attribution to another domain.
    """
    try:
        left = urlsplit(canonicalize_url(first)).netloc.removeprefix("www.")
        right = urlsplit(canonicalize_url(second)).netloc.removeprefix("www.")
    except ValueError:
        return False
    return bool(left) and left == right
