"""Engine/session factory for the DB_BACKEND env var described in todo.md's
scope section 2: "sqlite" (default, ONE file per PROJECT -- `.jfi/JFI.db`
at the project root, shared by every session ever run there, each row
distinguished by its `session_id` column) or "mysql"/"postgres" (one
shared DATABASE_URL, e.g. a fleet-wide server multiple projects/machines
can all reach -- see todo.md's open decision #1 on how the master
dashboard reads this).

One DB per project rather than one per session: every table already keyed
its rows by `session_id`, so nothing about the schema had to change to
support this -- only where the file lives. Everything JFI-related --
`JFI.db` and every session's own bookkeeping files alike -- lives inside
the single hidden `.jfi/` folder at the project root (flat, no
per-session subfolder -- see SimpleSessionManager.__init__), so a
project's own .gitignore only ever needs the one entry (`.jfi/`) to cover
all of it, not several separate file/folder names.

Importing this module (rather than just the individual model modules) is
what registers every table with SQLModel.metadata, since `create_all` below
only ever sees tables that have actually been imported somewhere first.
"""

import os
from pathlib import Path

from sqlmodel import Session, SQLModel, create_engine

# Importing each model module registers its table with SQLModel.metadata --
# see this module's own docstring. Order doesn't matter for registration,
# but every table with a foreign_key="sessionrecord.session_id" needs
# SessionRecord's own table already defined by the time create_all runs,
# which import order alone doesn't guarantee -- SQLAlchemy resolves that
# from the string reference at create_all time, not at import time, so this
# is just for registration, not dependency ordering.
from JFI.models.activity import ActivityEvent  # noqa: F401
from JFI.models.context_entry import ContextEntry  # noqa: F401
from JFI.models.files import ImplementedFile  # noqa: F401
from JFI.models.history import HistoryMessage  # noqa: F401
from JFI.models.leaf import Leaf  # noqa: F401
from JFI.models.log_event import LogEvent  # noqa: F401
from JFI.models.phases import DonePhase  # noqa: F401
from JFI.models.process import BackgroundProcess  # noqa: F401
from JFI.models.queue import QueuedItem  # noqa: F401
from JFI.models.session import SessionRecord  # noqa: F401
from JFI.models.session_note import SessionNote  # noqa: F401
from JFI.models.tools import UnlockedTool  # noqa: F401


def database_url(project_root: Path) -> str:
    backend = os.environ.get("DB_BACKEND", "sqlite").lower()
    if backend == "sqlite":
        db_path = Path(project_root) / ".jfi" / "JFI.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        return f"sqlite:///{db_path}"
    if backend in ("mysql", "postgres", "postgresql"):
        url = os.environ.get("DATABASE_URL")
        if not url:
            raise ValueError(
                f"DB_BACKEND={backend!r} requires DATABASE_URL to be set "
                "(e.g. postgresql+psycopg://user:pass@host/db, or "
                "mysql+pymysql://user:pass@host/db)"
            )
        return url
    raise ValueError(f"Unknown DB_BACKEND={backend!r} -- expected sqlite, mysql, or postgres")


def get_engine(project_root: Path):
    """One engine per process, created once and reused -- see
    SimpleSessionManager's own __init__ for where this gets called. Safe
    to call once per session even though the underlying DB is shared
    (SQLAlchemy pools connections; create_all is a no-op once the tables
    already exist) -- and safe to call from MULTIPLE processes at once
    against the same file: the fleet dashboard (jfi-web/master.js) and
    export-db all open their own engine against the very same
    `.jfi/JFI.db` a live jfi session is writing to, at the same time."""
    url = database_url(project_root)
    # SQLite connections are not thread-safe by default; JFI's own console/
    # tool-execution machinery can touch the session manager from more than
    # one thread (background processes, the web bridge), same reason
    # SimpleSessionManager already takes its own file lock elsewhere.
    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    engine = create_engine(url, connect_args=connect_args)
    if url.startswith("sqlite"):
        # WAL instead of SQLite's default rollback-journal mode: a writer
        # (the live jfi session) no longer takes an exclusive lock that
        # blocks every reader (the dashboard, export-db) for the write's
        # duration -- readers see the last-committed state and proceed
        # immediately instead of hitting "database is locked". Persists in
        # the file itself once set, but cheap and idempotent to reassert
        # every time a new process opens this same file.
        with engine.connect() as conn:
            conn.exec_driver_sql("PRAGMA journal_mode=WAL")
            conn.exec_driver_sql("PRAGMA busy_timeout=5000")
    SQLModel.metadata.create_all(engine)
    return engine


def get_session(engine) -> Session:
    return Session(engine)
