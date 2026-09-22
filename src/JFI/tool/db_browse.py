"""Shared table registry + generic row-fetching for the DB browser feature
in both dashboards:

- The Streamlit dashboard's own "Database" tab (JFI.web.dashboard) queries
  this directly -- it already has a local `.jfi/JFI.db` engine.
- The Node fleet dashboard's "Session DB" tab has no such access (master.js
  is meant to run on a different machine than the sessions reporting to
  it, see JFI.manager.socket_reporter's own module docstring) -- it sends
  a {"action": "db_query"} control message instead, which the reporting
  session's own SocketReporter answers by calling query_table() here (on
  the machine that actually has the file) and relaying the JSON result
  back over the same WebSocket. Same underlying query either way, so the
  two dashboards can never drift on which tables exist or how they're
  filtered.
"""

from datetime import datetime
from typing import Optional

# Generous but bounded -- a remote viewer over a WebSocket (the fleet
# dashboard path) shouldn't be able to trigger shipping an entire
# multi-thousand-row HistoryMessage table in one response.
ROW_LIMIT = 500


def table_registry() -> dict:
    """name -> SQLModel class, built lazily (not at import time) so
    importing this module doesn't pull in JFI.models before it's needed."""
    from JFI.models import (
        ActivityEvent,
        BackgroundProcess,
        ContextEntry,
        DonePhase,
        HistoryMessage,
        ImplementedFile,
        Leaf,
        LogEvent,
        QueuedItem,
        SessionNote,
        SessionRecord,
        UnlockedTool,
    )

    return {
        "SessionRecord": SessionRecord,
        "Leaf": Leaf,
        "HistoryMessage": HistoryMessage,
        "LogEvent": LogEvent,
        "ContextEntry": ContextEntry,
        "SessionNote": SessionNote,
        "QueuedItem": QueuedItem,
        "UnlockedTool": UnlockedTool,
        "ImplementedFile": ImplementedFile,
        "BackgroundProcess": BackgroundProcess,
        "ActivityEvent": ActivityEvent,
        "DonePhase": DonePhase,
    }


def _json_safe(value):
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def query_table(engine, table_name: str, session_id: Optional[str] = None) -> list[dict]:
    """Up to ROW_LIMIT rows of `table_name` as JSON-safe dicts, optionally
    filtered to one session_id. Raises ValueError for an unknown table
    name -- callers (a Streamlit selectbox, a remote fleet-dashboard
    request relayed through a WebSocket) must never let an arbitrary
    string reach a query, so this whitelist lookup is the only thing that
    ever turns into SQL."""
    from sqlmodel import select

    from JFI.models import get_session

    model = table_registry().get(table_name)
    if model is None:
        raise ValueError(f"Unknown table {table_name!r}")

    with get_session(engine) as db:
        query = select(model)
        if session_id and hasattr(model, "session_id"):
            query = query.where(model.session_id == session_id)
        query = query.limit(ROW_LIMIT)
        rows = list(db.exec(query))

    return [{k: _json_safe(v) for k, v in row.model_dump().items()} for row in rows]
