"""DB-backed conversation history -- full cutover replacement for
history.jsonl.gz (see /todo.md's Live validation section for why: once the
plan itself moved to the DB, the remaining files -- history.jsonl.gz,
context.json, metadata.json, run.log -- were the obvious next step rather
than leaving the migration half-done).

`HistoryMessage.seq` is an explicit per-session incrementing counter (not
row-insertion order) so export_db can merge-sort it against LogEvent's own
`seq` and reconstruct a run.log-shaped view with the two interleaved
correctly, the same way they're interleaved live today.

Message dicts round-trip through here in the EXACT shape the rest of this
codebase already uses (see runner.py's own message construction): a key is
only present when it was actually set (an assistant message with no
tool_calls never gets a "tool_calls": null in the dict, matching how
runner.py builds it conditionally), so nothing downstream needs to change
because history now comes from the DB instead of a file.
"""

from sqlmodel import select

from JFI.models import HistoryMessage, get_session


def has_history(engine, session_id: str) -> bool:
    with get_session(engine) as db:
        return db.exec(select(HistoryMessage).where(HistoryMessage.session_id == session_id)).first() is not None


def load_history_from_db(engine, session_id: str) -> list[dict]:
    """The full conversation as plain message dicts, in seq order --
    exactly the shape self.history already carries everywhere else in
    this codebase."""
    with get_session(engine) as db:
        rows = list(
            db.exec(
                select(HistoryMessage)
                .where(HistoryMessage.session_id == session_id)
                .order_by(HistoryMessage.seq)
            )
        )
    messages = []
    for row in rows:
        message: dict = {"role": row.role}
        if row.content is not None:
            message["content"] = row.content
        if row.tool_calls:
            message["tool_calls"] = row.tool_calls
        if row.tool_call_id is not None:
            message["tool_call_id"] = row.tool_call_id
        if row.name is not None:
            message["name"] = row.name
        messages.append(message)
    return messages


def append_history_to_db(engine, session_id: str, messages: list[dict]) -> None:
    """Appends `messages` (already-new ones only -- the caller tracks its
    own _flushed_count, same responsibility SimpleSessionManager.
    save_history already had for the old gzip file) as new HistoryMessage
    rows, continuing this session's own seq counter."""
    if not messages:
        return
    with get_session(engine) as db:
        existing_max = db.exec(
            select(HistoryMessage.seq)
            .where(HistoryMessage.session_id == session_id)
            .order_by(HistoryMessage.seq.desc())
        ).first()
        next_seq = (existing_max or 0) + 1
        for offset, message in enumerate(messages):
            db.add(HistoryMessage(
                session_id=session_id,
                seq=next_seq + offset,
                role=message.get("role", "user"),
                content=message.get("content"),
                tool_calls=message.get("tool_calls"),
                tool_call_id=message.get("tool_call_id"),
                name=message.get("name"),
            ))
        db.commit()
