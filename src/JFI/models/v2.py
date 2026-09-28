"""Tables the v2 pipeline adds (laya_plan.md §13.2). Unused by v1 sessions;
every row is keyed by session_id like the rest of the schema.

- Episode:        one row per LLM episode (a planner role, one Dev leaf, the
                  reviewer, cleanup) -- drives the token budget and turn cap.
- PlannerVerdict: one row per node Laya judged -- its raw answers, which
                  fallback fired, the status actually written.
- PlanEvent:      append-only audit trail of plan changes that aren't
                  verdicts (redo, escalate, reopen, overflow, ...).
- RunbookEntry:   how to set up / run / stop / view / test the app (§3.1).
- DesignEntry:    the design: stack, components, contracts, conventions (§3.2).
- Directive:      a user directive forced mid-run, delivered to the next
                  episode for its node (G7).
"""

from datetime import datetime
from typing import Any, Optional

from sqlalchemy import JSON, Column, UniqueConstraint
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


class PlannerVerdict(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    session_id: str = Field(index=True, foreign_key="sessionrecord.session_id")
    node_id: int = Field(index=True)
    level: str
    judged_at: datetime = Field(default_factory=utcnow)
    laya_model: Optional[str] = None  # None when Laya was unavailable
    laya_verdict: Optional[str] = None
    laya_redo_reason: Optional[str] = None
    probabilities: Optional[dict[str, Any]] = Field(default=None, sa_column=Column(JSON))
    answer_confidence: Optional[float] = None
    fallback: str = "none"  # none / conservative / llm / unavailable
    duplicate_of: Optional[int] = None  # sibling it was flagged against (G10)
    duplicate_score: Optional[float] = None
    budget_override: bool = False  # status forced by the token check (§5.3)
    final_status: str


class PlanEvent(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    session_id: str = Field(index=True, foreign_key="sessionrecord.session_id")
    node_id: Optional[int] = Field(default=None, index=True)
    # redo / escalate / breakdown / reopen / overflow / restart / skip / cap_reached / directive
    type: str = Field(index=True)
    detail: str = ""
    episode_id: Optional[int] = None
    created_at: datetime = Field(default_factory=utcnow)


class RunbookEntry(SQLModel, table=True):
    __table_args__ = (UniqueConstraint("session_id", "name", name="uq_runbookentry_session_name"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    session_id: str = Field(index=True, foreign_key="sessionrecord.session_id")
    name: str  # setup / run / stop / view / test / test_one / build / logs / e2e / ...
    command: str
    notes: str = ""
    verified: bool = False
    verified_at: Optional[datetime] = None
    updated_by: str = ""  # the role that last wrote it
    updated_at: datetime = Field(default_factory=utcnow)


class DesignEntry(SQLModel, table=True):
    __table_args__ = (UniqueConstraint("session_id", "kind", "key", name="uq_designentry_session_kind_key"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    session_id: str = Field(index=True, foreign_key="sessionrecord.session_id")
    # stack / component / contract / convention / assumption / out_of_scope / outline
    kind: str
    key: str
    text: str
    created_by: str = ""
    updated_at: datetime = Field(default_factory=utcnow)


class Directive(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    session_id: str = Field(index=True, foreign_key="sessionrecord.session_id")
    node_id: Optional[int] = Field(default=None, index=True)  # None = the next episode of any kind
    text: str
    consumed_episode_id: Optional[int] = None  # None = still pending
    created_at: datetime = Field(default_factory=utcnow)
