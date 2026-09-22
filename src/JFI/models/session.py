"""One row per JFI session -- the scalar/singular fields from metadata.json
(digest_summary, digest_block_count) and the live status snapshot
(phase/stage/state/iteration/queue_size/is_paused/current task/tokens).

List-shaped things that used to live on this row as JSON blobs now have
their own tables instead (see JFI.models package docstring for the full
file-to-table map): queued_items -> QueuedItem, unlocked_tools ->
UnlockedTool, implemented_files -> ImplementedFile, background_processes ->
BackgroundProcess, done_phases -> DonePhase. Each of those is independently
queryable now ("every session that touched file X") the way a JSON blob
never could be.

`awaiting_prompt`/`awaiting_options` stay fields here rather than their own
table: there is only ever ONE current awaiting prompt per session (never a
history worth separate rows -- once answered it's just cleared, and the
transition itself is already captured as a LogEvent/ActivityEvent), so a
table would model something with no multi-row shape.
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import JSON, Column
from sqlmodel import Field, SQLModel

from JFI.models._util import utcnow
from JFI.models.enums import Phase


class SessionRecord(SQLModel, table=True):
    __tablename__ = "sessionrecord"

    session_id: str = Field(primary_key=True)
    repo_path: str
    goal_text: str = ""
    task_type: Optional[str] = None  # "python"/"javascript"/... -- see task_rules.detect_task_type

    phase: Optional[Phase] = None
    stage: Optional[str] = None  # planner sub-stage: architect/team_lead/journeyman/function_breakdown
    state: Optional[str] = None  # "streaming"/"thinking"/"running tools"/"idle · queue empty"/...

    iteration: int = 1
    queue_size: int = 0
    is_paused: bool = False

    # Currently-executing leaf, denormalized here for a cheap "what's it
    # doing right now" read without a join -- the authoritative row is
    # still Leaf (status=todo with a non-null started_at and no ended_at).
    current_task: Optional[str] = None
    current_task_started_at: Optional[datetime] = None

    # Set while blocked on a get_user_choice-style prompt; cleared once
    # answered (see socket_reporter.py's _dispatch_control on the receiving
    # end for what answers it). `awaiting_options` is a small transient
    # list of {key,label} pairs with no independent query value, so it
    # stays JSON here rather than its own table.
    awaiting_prompt: Optional[str] = None
    awaiting_options: Optional[list[dict]] = Field(default=None, sa_column=Column(JSON))

    tokens_used: Optional[int] = None
    tokens_budget: Optional[int] = None
    tokens_read: Optional[int] = None
    tokens_written: Optional[int] = None

    digest_summary: Optional[str] = None
    digest_block_count: int = 0

    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
