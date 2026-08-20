"""Database URLs: parsing, validation and the path/DSN correspondence.

Deployment must be a variable, not a code change, so the database location is a
DSN. This module is pure string work with no dependency on configuration or on
any driver, which is what lets both ``config.py`` and the storage factory use it
without importing each other.

Two families are understood::

    sqlite:///newsletter.sqlite      relative file
    sqlite:////var/lib/news.sqlite   absolute POSIX file
    sqlite:///C:/data/news.sqlite    absolute Windows file
    sqlite:///:memory:               ephemeral, for tests
    postgresql://user:pw@host:5432/db
    postgres://...                   accepted, normalised to postgresql://
    postgresql+psycopg://...         accepted, the driver suffix is dropped

Anything else is rejected. A DSN whose scheme we do not recognise must never be
"helpfully" treated as a local file: a deployment that believes it is on Postgres
while quietly writing to a filesystem path is the failure this module exists to
prevent.
"""

from __future__ import annotations

import re
from pathlib import Path

#: The default location: a file next to the working directory, as before.
DEFAULT_DATABASE_URL = "sqlite:///newsletter.sqlite"

SQLITE_SCHEMES = frozenset({"sqlite", "sqlite3"})
POSTGRES_SCHEMES = frozenset({"postgresql", "postgres"})

_SCHEME = re.compile(r"^([a-zA-Z][a-zA-Z0-9+.\-]*)://(.*)$", re.DOTALL)


def split_dsn(dsn: str) -> tuple[str, str]:
    """Return ``(scheme, remainder)`` for a DSN, lowercasing the scheme.

    A ``+driver`` suffix (``postgresql+psycopg``) is dropped: it names a Python
    driver, not a database, and this project chooses the driver itself.
    """
    match = _SCHEME.match(dsn.strip())
    if match is None:
        raise ValueError(
            f"{dsn!r} is not a database URL; expected something like "
            f"{DEFAULT_DATABASE_URL!r} or 'postgresql://user:password@host:5432/newsletter'"
        )
    scheme, remainder = match.group(1).lower(), match.group(2)
    return scheme.split("+", 1)[0], remainder


def validate_dsn(dsn: str) -> str:
    """Normalise a DSN, or raise :class:`ValueError` naming the supported schemes."""
    scheme, remainder = split_dsn(dsn)
    if scheme in SQLITE_SCHEMES:
        if not sqlite_path_from_dsn(dsn):
            raise ValueError(f"{dsn!r} names no SQLite file; expected {DEFAULT_DATABASE_URL!r}")
        return f"sqlite://{remainder}" if scheme != "sqlite" else dsn.strip()
    if scheme in POSTGRES_SCHEMES:
        if not remainder:
            raise ValueError(f"{dsn!r} names no Postgres server, database or socket")
        return f"postgresql://{remainder}"
    supported = ", ".join(sorted(SQLITE_SCHEMES | POSTGRES_SCHEMES))
    raise ValueError(f"unsupported database scheme {scheme!r} in {dsn!r}; supported: {supported}")


def is_sqlite(dsn: str) -> bool:
    return split_dsn(dsn)[0] in SQLITE_SCHEMES


def is_postgres(dsn: str) -> bool:
    return split_dsn(dsn)[0] in POSTGRES_SCHEMES


def sqlite_path_from_dsn(dsn: str) -> str:
    """The file a SQLite DSN points at, in the form ``sqlite3.connect`` expects.

    ``sqlite:///relative`` keeps one slash for the scheme separator, so exactly
    one leading slash is removed; ``sqlite:////absolute`` therefore stays
    absolute, and ``sqlite:///C:/x`` stays a Windows path.
    """
    scheme, remainder = split_dsn(dsn)
    if scheme not in SQLITE_SCHEMES:
        raise ValueError(f"{dsn!r} is not a SQLite URL")
    remainder = remainder.split("?", 1)[0]
    if remainder.startswith("/"):
        remainder = remainder[1:]
    return remainder


def dsn_for_sqlite_path(path: Path | str) -> str:
    """The DSN for a filesystem path, the inverse of :func:`sqlite_path_from_dsn`."""
    raw = str(path)
    if raw == ":memory:":
        return "sqlite:///:memory:"
    return f"sqlite:///{Path(raw).as_posix()}"


def redact_dsn(dsn: str) -> str:
    """The DSN with any password removed, so it can be printed or logged.

    A connection string is a credential. Nothing that reaches a terminal, a log
    file or a run manifest may carry the password inside it.
    """
    try:
        scheme, remainder = split_dsn(dsn)
    except ValueError:
        return "(invalid database url)"
    if scheme in SQLITE_SCHEMES:
        return dsn.strip()
    userinfo, separator, host = remainder.rpartition("@")
    if not separator:
        return f"{scheme}://{remainder}"
    user = userinfo.split(":", 1)[0]
    return f"{scheme}://{user}:***@{host}" if ":" in userinfo else f"{scheme}://{userinfo}@{host}"
