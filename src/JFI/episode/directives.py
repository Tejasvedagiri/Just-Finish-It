"""User directives forced mid-run (laya_plan.md G7).

v1 folds a forced directive into the one shared history, which v2's scoped
episodes never see. v2 stores it as a Directive row for the node being worked
on (or for "the next episode", node None) and delivers it in that node's next
episode's kickoff, marking it consumed so it's delivered exactly once.
"""

from typing import List, Optional

from sqlmodel import or_, select

from JFI.models import Directive, get_session


def add_directive(engine, session_id: str, text: str, node_id: Optional[int] = None) -> int:
    with get_session(engine) as db:
        row = Directive(session_id=session_id, node_id=node_id, text=text.strip())
        db.add(row)
        db.commit()
        db.refresh(row)
        return row.id


def take_directives(engine, session_id: str, node_id: Optional[int], episode_id: int) -> List[str]:
    """Pending directives for `node_id` plus untargeted ones, oldest first;
    marks them consumed by `episode_id`."""
    with get_session(engine) as db:
        query = select(Directive).where(Directive.session_id == session_id,
                                        Directive.consumed_episode_id.is_(None))
        if node_id is None:
            query = query.where(Directive.node_id.is_(None))
        else:
            query = query.where(or_(Directive.node_id == node_id, Directive.node_id.is_(None)))
        rows = list(db.exec(query.order_by(Directive.id)))
        for row in rows:
            row.consumed_episode_id = episode_id
            db.add(row)
        db.commit()
        return [row.text for row in rows]
