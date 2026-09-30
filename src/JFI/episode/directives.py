"""User directives forced mid-run (laya_plan.md G7).

`!text` typed while an episode runs goes straight into that episode (see
engine.run_episode); it's stored as a Directive row, already consumed, for
the record.
"""

from typing import Optional

from JFI.models import Directive, get_session


def record_delivered_directive(engine, session_id: str, text: str, node_id: Optional[int], episode_id: int) -> None:
    """A directive the running episode already received (the user's `!text`,
    see engine.run_episode): stored for the record, already consumed."""
    with get_session(engine) as db:
        db.add(Directive(session_id=session_id, node_id=node_id, text=text.strip(), consumed_episode_id=episode_id))
        db.commit()
