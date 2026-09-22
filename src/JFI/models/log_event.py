"""Full cutover replacement for the old run.log file -- every tag
PromptToolkitConsoleManager._log() ever wrote (SYSTEM, RULE, REASONING,
ASSISTANT, USER, TOOL_CALL, TOOL_RESULT, ERROR) gets a row here, not just
SYSTEM/RULE: REASONING and ERROR have no other persistence path at all
(never part of a HistoryMessage), and TOOL_CALL/TOOL_RESULT/USER/
ASSISTANT's text here can differ from HistoryMessage's own structured
version. export_db._render_log_export reads this table alone, in `seq`
order, to reconstruct the same view run.log used to show live.
"""

from datetime import datetime
from typing import Optional

from sqlmodel import Field, SQLModel

from JFI.models._util import utcnow


class LogEvent(SQLModel, table=True):
    __tablename__ = "logevent"

    id: Optional[int] = Field(default=None, primary_key=True)
    session_id: str = Field(index=True, foreign_key="sessionrecord.session_id")
    seq: int = Field(index=True)

    tag: str  # "system" / "rule" / "reasoning" / "assistant" / "user" / "tool_call" / "tool_result" / "error"
    text: str

    created_at: datetime = Field(default_factory=utcnow, index=True)
