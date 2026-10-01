"""Engine/session factory for the DB_BACKEND env var: "sqlite" (default, ONE
file per PROJECT -- `.jfi/JFI.db` at the project root, shared by every
session ever run there, each row distinguished by its `session_id` column)
or "mysql"/"postgres" (one shared DATABASE_URL, e.g. a fleet-wide server
multiple projects/machines can all reach).

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
from JFI.models.episode import Directive, Episode  # noqa: F401
from JFI.models.planning import PlanEvent, PlannerVerdict  # noqa: F401
from JFI.models.runbook_design import DesignEntry, RunbookEntry  # noqa: F401


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
    for table, columns in ADDED_COLUMNS.items():
        _ensure_columns(engine, table, columns)
    return engine


#: Columns added to tables that older project DBs already have. create_all()
#: never alters an existing table, so each one is added here as well (see
#: AGENTS.md). Every clause is nullable or has a default, so a plain ADD
#: COLUMN works on SQLite, MySQL and Postgres alike. Old rows read back
#: unchanged.
ADDED_COLUMNS = {
    "leaf": {
        # Program Manager's per-leaf review (v1)
        "review_status": "TEXT",
        "review_note": "TEXT",
        "rejection_count": "INTEGER DEFAULT 0",
        # v2 planner (laya_plan.md §13.1)
        "level": "TEXT",
        "kind": "TEXT",
        "plan_status": "TEXT",
        "redo_count": "INTEGER DEFAULT 0",
        "redo_reason": "TEXT",
        "escalation_count": "INTEGER DEFAULT 0",
        "paused": "BOOLEAN DEFAULT FALSE",
        "done_when": "TEXT",
        "notes": "TEXT",
        "references": "JSON",
        "files": "JSON",
        "depends_on": "JSON",
        "tools": "JSON",
        "attempt_count": "INTEGER DEFAULT 0",
        "fix_note": "TEXT",
        "reopened_count": "INTEGER DEFAULT 0",
        "checkpoint": "TEXT",
    },
    "sessionrecord": {
        "pipeline_version": "VARCHAR(8) DEFAULT 'v1'",
    },
    "historymessage": {
        "episode_id": "INTEGER",
    },
    # The two-score judge (the rule + Laya, LLM tie-break)
    "plannerverdict": {
        "rule_verdict": "TEXT",
        "tiebreak_verdict": "TEXT",
        "decided_by": "TEXT",
    },
}


def _ensure_columns(engine, table: str, columns: dict) -> None:
    """Adds any of `columns` (name -> type/default clause) missing from an
    already-existing table -- create_all() above only ever creates NEW
    tables, it never alters one that's already there, so a schema change to
    an existing table needs this idempotent ALTER TABLE bootstrap to reach a
    project DB that predates it (the same "cheap and idempotent to reassert
    every time" tradeoff as the WAL pragma above).

    Works on every DB_BACKEND: existing columns come from SQLAlchemy's
    inspector rather than SQLite's PRAGMA, and `ALTER TABLE t ADD COLUMN c
    <clause>` is the same statement on SQLite, MySQL and Postgres for the
    nullable / defaulted columns used here. It used to return early on
    anything but SQLite, so a MySQL/Postgres project with an existing DB
    silently never got new columns."""
    from sqlalchemy import inspect

    inspector = inspect(engine)
    if not inspector.has_table(table):
        return
    existing = {col["name"] for col in inspector.get_columns(table)}
    missing = [(name, clause) for name, clause in columns.items() if name not in existing]
    if not missing:
        return
    quote = engine.dialect.identifier_preparer.quote
    with engine.connect() as conn:
        for name, clause in missing:
            conn.exec_driver_sql(f"ALTER TABLE {quote(table)} ADD COLUMN {quote(name)} {clause}")
        conn.commit()


def get_session(engine) -> Session:
    return Session(engine)
