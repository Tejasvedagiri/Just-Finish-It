"""Which pipeline a session runs on (laya_plan.md G16a).

Every session records `SessionRecord.pipeline_version` when it's created:
"v2" when JFI_PIPELINE=v2 is set, otherwise "v1". It never changes after
that -- sessions created before v2 existed read as "v1" and keep the old code
path until they finish, so building v2 phase by phase never disturbs them.
"""

import os

from JFI.models import SessionRecord, get_session

PIPELINES = ("v1", "v2")


def new_session_pipeline() -> str:
    value = os.environ.get("JFI_PIPELINE", "").strip().lower()
    return value if value in PIPELINES else "v1"


def session_pipeline(engine, session_id: str) -> str:
    with get_session(engine) as db:
        record = db.get(SessionRecord, session_id)
        return record.pipeline_version if record is not None else "v1"
