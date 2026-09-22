"""Replaces the three harness-control-flow markdown files that used to
live in a session's own bookkeeping folder: review.md (reviewer's failure
report), NotesForReviewer.md (implementation's notes for the reviewer),
feedback_to_plan.md (product owner's plan-rejection feedback). Unlike
ContextEntry (the model's own free-form scratchpad, pulled via
context_lookup, never read by the harness itself), these three are a
harness<->model CONTROL-FLOW signal: their presence/absence and content
directly drive runner.py's review_outcome/product_owner_feedback_outcome
decisions (another full iteration vs. done) -- kept as their own model
rather than folded into ContextEntry to keep that distinction explicit,
even though the underlying one-row-per-(session, kind) shape is the same.

One row per (session_id, kind); `kind` is one of "reviewer_notes"
(implementation -> reviewer, see JFI.tool.note_tools.REVIEWER_NOTES),
"review_report" (reviewer -> next planner iteration, see
JFI.tool.note_tools.REVIEW_REPORT), or "plan_feedback" (product owner ->
planner, see JFI.tool.note_tools.PLAN_FEEDBACK). A row's mere EXISTENCE is
itself part of the signal (matching the old "does review.md exist" file
check) -- runner.py deletes the row once it has consumed it, the same
"cleared immediately and unconditionally, whichever branch it took" rule
the old file-based version already followed.
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import UniqueConstraint
from sqlmodel import Field, SQLModel

from JFI.models._util import utcnow


class SessionNote(SQLModel, table=True):
    __tablename__ = "sessionnote"
    __table_args__ = (UniqueConstraint("session_id", "kind", name="uq_sessionnote_session_kind"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    session_id: str = Field(index=True, foreign_key="sessionrecord.session_id")
    kind: str = Field(index=True)
    text: str

    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
