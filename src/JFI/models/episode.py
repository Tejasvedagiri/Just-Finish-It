"""One row per LLM episode, and the user directives delivered into them.

- Episode:   one short, scoped LLM conversation (a planner role on one node,
             one Dev leaf, the reviewer, cleanup) -- its turns, tokens and
             why it ended (laya_plan.md §0, §5.3).
- Directive: a user directive forced mid-run (`!text`), delivered to the next
             episode for its node (G7).
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import JSON, Column
from sqlmodel import Field, SQLModel

from JFI.models._util import utcnow


class Episode(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    session_id: str = Field(index=True, foreign_key="sessionrecord.session_id")
    node_id: Optional[int] = Field(default=None, index=True)  # None for cleanup
    role: str  # architect / lead / task / dev / reviewer / cleanup
    mode: str  # create / breakdown / redo / extend / implement / fix / e2e / cleanup
    started_at: datetime = Field(default_factory=utcnow)
    ended_at: Optional[datetime] = None
    turns: int = 0
    tokens: int = 0  # the server's usage when reported, else the chars/4 estimate (G14)
    end_reason: Optional[str] = None  # finish / done / budget / turn_cap / error / stopped
    tools_loaded: Optional[list[str]] = Field(default=None, sa_column=Column(JSON))
    tools_used: Optional[list[str]] = Field(default=None, sa_column=Column(JSON))


class Directive(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    session_id: str = Field(index=True, foreign_key="sessionrecord.session_id")
    node_id: Optional[int] = Field(default=None, index=True)  # None = the next episode of any kind
    text: str
    consumed_episode_id: Optional[int] = None  # None = still pending
    created_at: datetime = Field(default_factory=utcnow)
