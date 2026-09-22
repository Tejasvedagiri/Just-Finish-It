"""The full conversation transcript -- replaces history.jsonl.gz's gzip'd
JSONL messages. `seq` is an explicit per-session incrementing counter (not
just row-insertion order) so export_db can merge-sort this against
LogEvent's own `seq` and reconstruct a run.log-shaped view with the two
interleaved correctly, the same way they're interleaved live today.
"""

from datetime import datetime
from typing import Any, Optional

from sqlalchemy import JSON, Column
from sqlmodel import Field, SQLModel

from JFI.models._util import utcnow


class HistoryMessage(SQLModel, table=True):
    __tablename__ = "historymessage"

    id: Optional[int] = Field(default=None, primary_key=True)
    session_id: str = Field(index=True, foreign_key="sessionrecord.session_id")
    seq: int = Field(index=True)

    role: str  # "system" / "user" / "assistant" / "tool"
    # Usually a plain string, but a multimodal user message (an attached
    # image) carries content as a list of parts instead (e.g.
    # [{"type": "text", ...}, {"type": "image_url", ...}] -- see
    # runner.py's own construction sites) -- JSON so either shape binds to
    # SQLite cleanly, not just str.
    content: Optional[Any] = Field(default=None, sa_column=Column(JSON))
    # [{"id": ..., "function": {"name": ..., "arguments": ...}}, ...] when
    # an assistant message calls tools -- same shape the OpenAI-compatible
    # API already uses, so no translation is needed at either read or write.
    tool_calls: Optional[list[dict]] = Field(default=None, sa_column=Column(JSON))
    # Set on a role="tool" message, linking it back to the tool_calls
    # entry it answers.
    tool_call_id: Optional[str] = None
    # Also set on a role="tool" message (the function name) -- every real
    # construction site (runner.py's tool-call loop,
    # SimpleSessionManager._repair_dangling_tool_calls' own synthesized
    # entries) includes this; stored directly rather than reconstructed by
    # looking up the matching tool_calls entry in an earlier row, which
    # would be fragile if history ever gets edited or partially loaded.
    name: Optional[str] = None

    created_at: datetime = Field(default_factory=utcnow, index=True)
