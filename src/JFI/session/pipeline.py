"""Which pipeline a session was created on (laya_plan.md G16a).

Every new session records `SessionRecord.pipeline_version = "v2"`. Sessions
created before v1 was removed carry "v1" (or no record at all, which reads as
"v1"); the runner refuses to resume them, since their tiered planner, Program
Manager and testing phase no longer exist. `export-db` still reads them.
"""

from JFI.models import SessionRecord, get_session

CURRENT_PIPELINE = "v2"


def session_pipeline(engine, session_id: str) -> str:
    with get_session(engine) as db:
        record = db.get(SessionRecord, session_id)
        return record.pipeline_version if record is not None else "v1"
