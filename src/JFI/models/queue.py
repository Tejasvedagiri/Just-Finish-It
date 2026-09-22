"""A session's pending queued follow-up messages -- what the dashboard's
"Queue a follow-up" control (server/master.js's `queue` action) and a `!`-
prefixed forced directive both add to. `position` is a plain ordinal (0 =
next to run), not a sort_key like Leaf's -- this list is only ever
appended to at the end or drained from the front, never reordered/inserted
into the middle, so gap numbering buys nothing here.
"""

from datetime import datetime
from typing import Optional

from sqlmodel import Field, SQLModel

from JFI.models._util import utcnow


class QueuedItem(SQLModel, table=True):
    __tablename__ = "queueditem"

    id: Optional[int] = Field(default=None, primary_key=True)
    session_id: str = Field(index=True, foreign_key="sessionrecord.session_id")
    position: int
    text: str

    created_at: datetime = Field(default_factory=utcnow)
