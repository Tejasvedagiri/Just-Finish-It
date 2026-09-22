"""DB-backed session metadata -- full cutover replacement for
metadata.json (implemented_files, unlocked_tools, queued_requests,
digest_summary/digest_block_count). See history_store.py's module
docstring for why; same reasoning applies here.

SimpleSessionManager keeps `self.metadata` as a plain in-memory dict --
every existing call site (track_file, unlock_tool, save_queued_requests,
the Tier-4 compression digest) reads/writes it exactly as before. Only
load_metadata()/save_metadata() themselves change, bridging that dict to
the DB instead of to a JSON file.
"""

from sqlmodel import select

from JFI.models import ImplementedFile, QueuedItem, SessionRecord, UnlockedTool, get_session


def load_metadata_from_db(engine, session_id: str, repo_path: str) -> dict:
    with get_session(engine) as db:
        record = db.get(SessionRecord, session_id)
        if record is None:
            db.add(SessionRecord(session_id=session_id, repo_path=repo_path))
            db.commit()
            record = db.get(SessionRecord, session_id)

        unlocked = [
            row.tool_name
            for row in db.exec(select(UnlockedTool).where(UnlockedTool.session_id == session_id))
        ]
        files = [
            row.file_path
            for row in db.exec(select(ImplementedFile).where(ImplementedFile.session_id == session_id))
        ]
        queued = [
            row.text
            for row in db.exec(
                select(QueuedItem).where(QueuedItem.session_id == session_id).order_by(QueuedItem.position)
            )
        ]
        return {
            "implemented_files": files,
            "unlocked_tools": unlocked,
            "queued_requests": queued,
            "digest_summary": record.digest_summary or "",
            "digest_block_count": record.digest_block_count or 0,
        }


def save_metadata_to_db(engine, session_id: str, repo_path: str, metadata: dict) -> None:
    """Reconciles the DB with `metadata`'s current contents. unlocked_tools/
    implemented_files only ever grow (a tool once unlocked, a file once
    touched, both stay true for the rest of the session -- matches the old
    JSON list's own append-only semantics), so those are insert-if-missing.
    queued_requests can shrink/reorder (drained, or the console promotes
    part of the queue), so that one is fully replaced each save."""
    with get_session(engine) as db:
        record = db.get(SessionRecord, session_id)
        if record is None:
            record = SessionRecord(session_id=session_id, repo_path=repo_path)
        record.digest_summary = metadata.get("digest_summary") or None
        record.digest_block_count = metadata.get("digest_block_count", 0)
        db.add(record)

        existing_tools = {
            row.tool_name
            for row in db.exec(select(UnlockedTool).where(UnlockedTool.session_id == session_id))
        }
        for name in metadata.get("unlocked_tools", []):
            if name not in existing_tools:
                db.add(UnlockedTool(session_id=session_id, tool_name=name))

        existing_files = {
            row.file_path
            for row in db.exec(select(ImplementedFile).where(ImplementedFile.session_id == session_id))
        }
        for path in metadata.get("implemented_files", []):
            if path not in existing_files:
                db.add(ImplementedFile(session_id=session_id, file_path=path))

        for row in db.exec(select(QueuedItem).where(QueuedItem.session_id == session_id)):
            db.delete(row)
        for position, text in enumerate(metadata.get("queued_requests", [])):
            db.add(QueuedItem(session_id=session_id, position=position, text=text))

        db.commit()
