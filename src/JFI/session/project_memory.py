"""Project memory across sessions: a new session in a project starts from the
most recent earlier session's runbook and its lasting design entries.

Before this, the runbook and design were per session, so every new goal in
the same project re-discovered them -- how to set up, run and test, where
tests live, "on Windows `python3` is the Store alias" -- the setup cost every
calc run paid again. The copies are unverified (the project may have changed
since), and the Architect is told to keep what still fits the new goal.
Goal-specific design (assumptions, out of scope, a document's outline) isn't
carried."""

from sqlmodel import select

from JFI.models import DesignEntry, RunbookEntry, SessionRecord
from JFI.session.pipeline import CURRENT_PIPELINE

CARRIED_DESIGN_KINDS = ("stack", "component", "contract", "convention")


def carry_over(db, session_id: str) -> str:
    """Copies the latest earlier session's runbook and lasting design into
    `session_id`; the source session's id, or "" when there was none."""
    for source in db.exec(select(SessionRecord).where(SessionRecord.session_id != session_id,
                                                       SessionRecord.pipeline_version == CURRENT_PIPELINE)
                          .order_by(SessionRecord.updated_at.desc())):
        runbook = db.exec(select(RunbookEntry).where(RunbookEntry.session_id == source.session_id)).all()
        if not runbook:
            continue
        for row in runbook:
            db.add(RunbookEntry(session_id=session_id, name=row.name, command=row.command, notes=row.notes,
                                updated_by=f"carried from {source.session_id}"))
        for row in db.exec(select(DesignEntry).where(DesignEntry.session_id == source.session_id,
                                                     DesignEntry.kind.in_(CARRIED_DESIGN_KINDS))):
            db.add(DesignEntry(session_id=session_id, kind=row.kind, key=row.key, text=row.text,
                               created_by=f"carried from {source.session_id}"))
        return source.session_id
    return ""
