"""Which deferred tools a v1 session called load_tool on -- replaced
metadata.json's unlocked_tools list. v2 episodes load optional tools per
episode (JFI.episode.tools) and don't write this; it stays for export-db. Unlike a JSON blob, this is independently queryable
("every session that ever unlocked execute_command") and gets a real
uniqueness guarantee (a tool can't be double-unlocked) from the schema
instead of the caller having to de-dupe a Python list by hand.
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import UniqueConstraint
from sqlmodel import Field, SQLModel

from JFI.models._util import utcnow


class UnlockedTool(SQLModel, table=True):
    __tablename__ = "unlockedtool"
    __table_args__ = (UniqueConstraint("session_id", "tool_name", name="uq_unlockedtool_session_tool"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    session_id: str = Field(index=True, foreign_key="sessionrecord.session_id")
    tool_name: str

    unlocked_at: datetime = Field(default_factory=utcnow)
