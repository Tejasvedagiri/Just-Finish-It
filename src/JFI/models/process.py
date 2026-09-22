"""Background processes started via execute_command's background mode
(dev servers, long-running watchers) -- replaces the status snapshot's
background_processes list. `elapsed` (shown in the dashboard's process
panel) is intentionally NOT a stored column, same principle as Leaf's own
timing: it's `now - started_at` (or `exited_at - started_at` once it has
exited), computed at read time so it can never go stale between writes.
"""

from datetime import datetime
from typing import Optional

from sqlmodel import Field, SQLModel

from JFI.models._util import utcnow


class BackgroundProcess(SQLModel, table=True):
    __tablename__ = "backgroundprocess"

    id: Optional[int] = Field(default=None, primary_key=True)
    session_id: str = Field(index=True, foreign_key="sessionrecord.session_id")
    handle: str  # short id the model refers to it by, e.g. "bg1"
    command: str
    pid: Optional[int] = None
    host: Optional[str] = None
    port: Optional[int] = None
    status: str = "running"  # "running" / "exited"

    started_at: datetime = Field(default_factory=utcnow)
    exited_at: Optional[datetime] = None
