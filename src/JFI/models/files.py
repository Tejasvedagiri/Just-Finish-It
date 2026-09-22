"""Which real project files this session has written/changed -- replaces
metadata.json's implemented_files list. A real table (not a JSON blob)
because this is exactly the kind of thing worth querying independently,
e.g. "has any session already touched src/dataService.js" before starting
a new leaf that assumes it hasn't.
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import UniqueConstraint
from sqlmodel import Field, SQLModel

from JFI.models._util import utcnow


class ImplementedFile(SQLModel, table=True):
    __tablename__ = "implementedfile"
    __table_args__ = (UniqueConstraint("session_id", "file_path", name="uq_implementedfile_session_path"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    session_id: str = Field(index=True, foreign_key="sessionrecord.session_id")
    file_path: str

    first_touched_at: datetime = Field(default_factory=utcnow)
