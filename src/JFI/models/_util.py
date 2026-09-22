"""Shared `now()` for every model's timestamp columns and for export_db's
elapsed-time math -- naive UTC, not timezone-aware, because SQLite (the
default DB_BACKEND) round-trips a stored datetime as naive: a value
written as `datetime.now(timezone.utc)` reads back with its tzinfo
silently stripped, and subtracting that from a freshly-constructed aware
`datetime.now(timezone.utc)` raises TypeError. Staying naive everywhere
(and treating every naive value in this package as implicitly UTC) is
simpler and more portable across DB_BACKEND choices than adding a custom
timezone-aware SQLAlchemy type just for SQLite's sake.
"""

from datetime import datetime, timezone


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)
