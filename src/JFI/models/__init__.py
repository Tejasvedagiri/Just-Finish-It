"""DB-backed session persistence -- replaces everything under
.jfi/ except .lock (a process mutex, not data -- see /todo.md).

File-to-table map (see /todo.md's "SQLite/Pydantic persistence rewrite"
section for the full design and open decisions):

    plan.md                              -> Leaf
    metadata.json: unlocked_tools        -> UnlockedTool
    metadata.json: implemented_files     -> ImplementedFile
    metadata.json: digest_summary/count  -> SessionRecord fields
    context.json                         -> ContextEntry
    history.jsonl.gz                     -> HistoryMessage
    run.log: SYSTEM/RULE lines           -> LogEvent
    run.log: REASONING/ASSISTANT/TOOL_*  -> HistoryMessage
    status snapshot: queued_items        -> QueuedItem
    status snapshot: background_processes -> BackgroundProcess
    status snapshot: done_phases         -> DonePhase
    status snapshot: awaiting            -> SessionRecord fields
    web_status.json                      -> derived (a live query), not stored
    review.md / NotesForReviewer.md /
      feedback_to_plan.md                -> SessionNote (one row per kind)

Public surface: every table, their enums (Phase, LeafStatus), and the
engine/session factory (get_engine, get_session, database_url) that reads
DB_BACKEND/DATABASE_URL from env.
"""

from JFI.models.activity import ActivityEvent
from JFI.models.context_entry import ContextEntry
from JFI.models.db import database_url, get_engine, get_session
from JFI.models.enums import LeafStatus, Phase
from JFI.models.files import ImplementedFile
from JFI.models.history import HistoryMessage
from JFI.models.leaf import Leaf, build_indexes, display_number
from JFI.models.log_event import LogEvent
from JFI.models.phases import DonePhase
from JFI.models.process import BackgroundProcess
from JFI.models.queue import QueuedItem
from JFI.models.session import SessionRecord
from JFI.models.session_note import SessionNote
from JFI.models.tools import UnlockedTool

__all__ = [
    "ActivityEvent",
    "BackgroundProcess",
    "ContextEntry",
    "DonePhase",
    "HistoryMessage",
    "ImplementedFile",
    "Leaf",
    "LeafStatus",
    "LogEvent",
    "Phase",
    "QueuedItem",
    "SessionRecord",
    "SessionNote",
    "UnlockedTool",
    "build_indexes",
    "database_url",
    "display_number",
    "get_engine",
    "get_session",
]
