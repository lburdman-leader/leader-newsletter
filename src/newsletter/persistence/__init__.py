"""Persistence: articles, assessments, editions, submissions, run history.

``base.Storage`` is the contract; ``sqlite.Database`` and ``postgres.PostgresStorage``
implement it, and ``factory.create_storage`` resolves a DSN to one of them. Nothing
is imported eagerly here, so a default install never pays for a database driver
it does not use.
"""
