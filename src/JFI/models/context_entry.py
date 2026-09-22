"""Replaces context.json's key-value store, read/written by the
context_lookup/context_save tools (see the Function Breakdown planner
stage in simple_session_manager.py) so a later leaf touching the same
file/module doesn't re-read it from scratch. One row per (session, key);
`value` holds whatever fact text was saved under that key.
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import UniqueConstraint
from sqlmodel import Field, SQLModel

from JFI.models._util import utcnow


class ContextEntry(SQLModel, table=True):
    __tablename__ = "contextentry"
    __table_args__ = (UniqueConstraint("session_id", "key", name="uq_contextentry_session_key"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    session_id: str = Field(index=True, foreign_key="sessionrecord.session_id")
    key: str = Field(index=True)
    value: str

    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
