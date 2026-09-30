"""The planner's record: how each node was judged, and every other plan change.

- PlannerVerdict: one row per judged node -- the rule's answer, Laya's
                  (with its confidence), the LLM tie-break's pick, the status
                  written and who decided (laya_plan.md §5).
- PlanEvent:      append-only audit trail of plan changes that aren't
                  verdicts (redo, escalate, reopen, overflow, ...).
"""

from datetime import datetime
from typing import Any, Optional

from sqlalchemy import JSON, Column
from sqlmodel import Field, SQLModel

from JFI.models._util import utcnow


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
    # The two-score judge: the rule's answer, the LLM's pick when the rule and
    # a confident Laya disagreed, and which of agree / rule / llm decided.
    rule_verdict: Optional[str] = None
    tiebreak_verdict: Optional[str] = None
    decided_by: Optional[str] = None


class PlanEvent(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    session_id: str = Field(index=True, foreign_key="sessionrecord.session_id")
    node_id: Optional[int] = Field(default=None, index=True)
    # redo / escalate / breakdown / reopen / overflow / restart / skip / cap_reached / directive
    type: str = Field(index=True)
    detail: str = ""
    episode_id: Optional[int] = None
    created_at: datetime = Field(default_factory=utcnow)
