"""Which deferred tools (see JFI.tool.schemas's CORE_TOOLS/DEFERRED_TOOLS
split) this session has called load_tool on -- replaces metadata.json's
unlocked_tools list. Unlike a JSON blob, this is independently queryable
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
