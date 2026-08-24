"""Resolve a database URL to a storage implementation.

One function, one decision, no cleverness. The scheme in the DSN chooses the
backend; an unrecognised scheme raises :class:`~newsletter.config.ConfigError`
and the run stops.

There is deliberately no fallback. A deployment that believes it is writing to a
Postgres server while quietly writing to a local file loses every edition it
thinks it saved, and discovers this only when someone looks. Failing loudly at
startup costs one restart; failing quietly costs the archive.
"""

from __future__ import annotations

from newsletter.config import ConfigError
from newsletter.persistence.base import Storage
from newsletter.persistence.dsn import (
    is_postgres,
    is_sqlite,
    redact_dsn,
    sqlite_path_from_dsn,
    validate_dsn,
)


def create_storage(database_url: str, *, read_only: bool = False) -> Storage:
    """Build the storage backend for ``database_url``, unconnected.

    The caller opens it, so failure to *reach* the database is a persistence
    error while failure to *understand* the URL is a configuration error.

    ``read_only`` asks the engine itself to refuse writes and skips schema
    creation, so a report can be run against a live database without touching
    it. Every backend honours it; none simulates it in Python.
    """
    try:
        dsn = validate_dsn(database_url)
    except ValueError as exc:
        raise ConfigError(f"invalid database_url: {exc}") from exc

    if is_sqlite(dsn):
        from newsletter.persistence.sqlite import Database

        return Database(sqlite_path_from_dsn(dsn), read_only=read_only)

    if is_postgres(dsn):
        # Imported here so that the driver stays an optional extra: a default
        # install, and `import newsletter`, never need psycopg.
        from newsletter.persistence.postgres import PostgresStorage

        return PostgresStorage(dsn, read_only=read_only)

    raise ConfigError(  # pragma: no cover - validate_dsn already closed this set
        f"no storage backend for {redact_dsn(database_url)}"
    )
