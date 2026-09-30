"""What the Architect writes down for every later role to pull.

- RunbookEntry: how to set up / run / stop / view / test / build the app
                (laya_plan.md §3.1); `verified` once a command has really run.
- DesignEntry:  the design -- stack, components, contracts, conventions,
                assumptions, the outline of a document (§3.2).
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import UniqueConstraint
from sqlmodel import Field, SQLModel

from JFI.models._util import utcnow


class RunbookEntry(SQLModel, table=True):
    __table_args__ = (UniqueConstraint("session_id", "name", name="uq_runbookentry_session_name"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    session_id: str = Field(index=True, foreign_key="sessionrecord.session_id")
    name: str  # setup / run / stop / view / test / test_one / build / logs / e2e / ...
    command: str
    notes: str = ""
    verified: bool = False
    verified_at: Optional[datetime] = None
    updated_by: str = ""  # the role that last wrote it
    updated_at: datetime = Field(default_factory=utcnow)


class DesignEntry(SQLModel, table=True):
    __table_args__ = (UniqueConstraint("session_id", "kind", "key", name="uq_designentry_session_kind_key"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    session_id: str = Field(index=True, foreign_key="sessionrecord.session_id")
    # stack / component / contract / convention / assumption / out_of_scope / outline
    kind: str
    key: str
    text: str
    created_by: str = ""
    updated_at: datetime = Field(default_factory=utcnow)
