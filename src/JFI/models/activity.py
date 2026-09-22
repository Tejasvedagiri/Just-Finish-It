"""Mirrors the activity feed server/session-registry.js's deriveEvents()
already computes today (phase change, awaiting-input, ticked N/M, pipeline
complete) -- a Python-side model for the same shape, in case a future
caller wants to persist/query it from the DB side. deriveEvents() itself
stays exactly where it is for v1 (see todo.md's open decision #2): this
model does not yet replace it, it just gives the same shape a DB row.
"""

from datetime import datetime
from typing import Literal, Optional

from sqlmodel import Field, SQLModel

from JFI.models._util import utcnow

Severity = Literal["info", "good", "warn", "bad"]


class ActivityEvent(SQLModel, table=True):
    __tablename__ = "activityevent"

    id: Optional[int] = Field(default=None, primary_key=True)
    session_id: str = Field(index=True, foreign_key="sessionrecord.session_id")
    severity: str = "info"  # Severity's values, as a plain str column (see enums.py's docstring on why)
    text: str
    created_at: datetime = Field(default_factory=utcnow, index=True)
