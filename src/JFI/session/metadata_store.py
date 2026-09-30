"""DB-backed session metadata -- the replacement for metadata.json. See
history_store.py's module docstring for why; same reasoning applies here.

SimpleSessionManager keeps `self.metadata` as a plain in-memory dict; only
load_metadata()/save_metadata() bridge it to the DB. Only the queued requests
are live. Older sessions also have implemented files, unlocked tools and a
history digest (from v1's file tools and one-conversation turn loop); those
rows stay for export-db but aren't loaded any more.
"""

from sqlmodel import select

from JFI.models import QueuedItem, SessionRecord, get_session
from JFI.session.pipeline import CURRENT_PIPELINE


def _record(db, session_id: str, repo_path: str) -> SessionRecord:
    record = db.get(SessionRecord, session_id)
    if record is None:
        record = SessionRecord(session_id=session_id, repo_path=repo_path, pipeline_version=CURRENT_PIPELINE)
        db.add(record)
        db.commit()
    return record


def load_metadata_from_db(engine, session_id: str, repo_path: str) -> dict:
    with get_session(engine) as db:
        _record(db, session_id, repo_path)
        queued = [row.text for row in db.exec(
            select(QueuedItem).where(QueuedItem.session_id == session_id).order_by(QueuedItem.position))]
        return {"queued_requests": queued}


def save_metadata_to_db(engine, session_id: str, repo_path: str, metadata: dict) -> None:
    """Replaces the stored queue with `metadata`'s: it can shrink or reorder
    (drained, or the console promotes part of it)."""
    with get_session(engine) as db:
        _record(db, session_id, repo_path)
        for row in db.exec(select(QueuedItem).where(QueuedItem.session_id == session_id)):
            db.delete(row)
        for position, text in enumerate(metadata.get("queued_requests", [])):
            db.add(QueuedItem(session_id=session_id, position=position, text=text))

        db.commit()
