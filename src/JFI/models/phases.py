"""Which of the six phases (planner/product_owner/imp/testing/reviewer/
cleanup) this session has completed -- replaces the status snapshot's
done_phases list. A real table rather than a JSON list mainly so
`completed_at` is tracked per phase (useful for later timing questions --
"how long did imp take" -- that a bare list of names can't answer).
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import UniqueConstraint
from sqlmodel import Field, SQLModel

from JFI.models._util import utcnow
from JFI.models.enums import Phase


class DonePhase(SQLModel, table=True):
    __tablename__ = "donephase"
    __table_args__ = (UniqueConstraint("session_id", "phase", name="uq_donephase_session_phase"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    session_id: str = Field(index=True, foreign_key="sessionrecord.session_id")
    phase: Phase

    completed_at: datetime = Field(default_factory=utcnow)
