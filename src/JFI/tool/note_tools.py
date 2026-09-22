"""add_reviewer_note / get_reviewer_notes / write_review_report /
write_plan_feedback -- DB-backed replacement (JFI.models.SessionNote) for
the three harness-control-flow markdown files a session used to write:
NotesForReviewer.md, review.md, feedback_to_plan.md. See SessionNote's own
module docstring for why these are a separate model from ContextEntry
(the model's own free-form scratchpad) despite the identical underlying
shape: these three specifically drive runner.py's own
review_outcome/product_owner_feedback_outcome control flow, not just
facts the model might want to recall later.

Three `kind` values, one per file replaced -- each written by exactly one
phase, read by at most one other:
    REVIEWER_NOTES = "reviewer_notes"   -- imp writes (appends), reviewer reads
    REVIEW_REPORT   = "review_report"   -- reviewer writes, runner.py reads+clears
    PLAN_FEEDBACK   = "plan_feedback"   -- product_owner writes, runner.py reads+clears

get_note/set_note/append_note/clear_note are the raw accessors underneath
the four model-facing tool functions -- also what runner.py uses directly
for the read+clear side of REVIEW_REPORT/PLAN_FEEDBACK (never exposed to
the model as tools; only the harness itself consumes those two kinds).
"""

from typing import Callable, Dict, Optional

from sqlmodel import select

from JFI.models import SessionNote, get_session
from JFI.models._util import utcnow

REVIEWER_NOTES = "reviewer_notes"
REVIEW_REPORT = "review_report"
PLAN_FEEDBACK = "plan_feedback"


def get_note(engine, session_id: str, kind: str) -> Optional[str]:
    with get_session(engine) as db:
        row = db.exec(
            select(SessionNote).where(SessionNote.session_id == session_id, SessionNote.kind == kind)
        ).first()
        return row.text if row else None


def set_note(engine, session_id: str, kind: str, text: str) -> None:
    with get_session(engine) as db:
        row = db.exec(
            select(SessionNote).where(SessionNote.session_id == session_id, SessionNote.kind == kind)
        ).first()
        if row is None:
            db.add(SessionNote(session_id=session_id, kind=kind, text=text))
        else:
            row.text = text
            row.updated_at = utcnow()
            db.add(row)
        db.commit()


def append_note(engine, session_id: str, kind: str, text: str) -> None:
    """Like set_note, but adds `text` as one more entry instead of
    replacing what's already there -- add_reviewer_note's own semantics
    (multiple imp leaves each leaving their own note across one phase
    pass), matching the old NotesForReviewer.md's append_to_file usage."""
    existing = get_note(engine, session_id, kind)
    combined = f"{existing}\n\n{text}" if existing else text
    set_note(engine, session_id, kind, combined)


def clear_note(engine, session_id: str, kind: str) -> None:
    with get_session(engine) as db:
        row = db.exec(
            select(SessionNote).where(SessionNote.session_id == session_id, SessionNote.kind == kind)
        ).first()
        if row is not None:
            db.delete(row)
            db.commit()


def add_reviewer_note(engine, session_id: str, text: str) -> str:
    text = (text or "").strip()
    if not text:
        return "Error: add_reviewer_note needs a non-empty text."
    append_note(engine, session_id, REVIEWER_NOTES, text)
    return "Success: note added for the reviewer."


def get_reviewer_notes(engine, session_id: str) -> str:
    notes = get_note(engine, session_id, REVIEWER_NOTES)
    return notes if notes else "No notes left by implementation this pass."


def write_review_report(engine, session_id: str, text: str) -> str:
    text = (text or "").strip()
    if not text:
        return "Error: write_review_report needs a non-empty text."
    set_note(engine, session_id, REVIEW_REPORT, text)
    return "Success: review report recorded — another full iteration will be scheduled."


def write_plan_feedback(engine, session_id: str, text: str) -> str:
    text = (text or "").strip()
    if not text:
        return "Error: write_plan_feedback needs a non-empty text."
    set_note(engine, session_id, PLAN_FEEDBACK, text)
    return "Success: plan feedback recorded — sent back to the planner."


def make_note_tools(engine, session_id: str) -> Dict[str, Callable]:
    """{"add_reviewer_note": ..., "get_reviewer_notes": ..., "write_review_report": ...,
    "write_plan_feedback": ...} bound to one session's own DB engine -- the
    same per-session rebinding pattern context_tools.make_context_tools/
    plan_db_tools.make_plan_db_tools use."""
    return {
        "add_reviewer_note": lambda text: add_reviewer_note(engine, session_id, text),
        "get_reviewer_notes": lambda: get_reviewer_notes(engine, session_id),
        "write_review_report": lambda text: write_review_report(engine, session_id, text),
        "write_plan_feedback": lambda text: write_plan_feedback(engine, session_id, text),
    }
