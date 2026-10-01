"""Project memory (JFI.session.project_memory). Observed on every calc run: a
new session in the same project re-discovered the runbook (setup, test_one,
"python3 is the Store alias on Windows") from scratch."""
from sqlmodel import select

from JFI.models import DesignEntry, RunbookEntry, get_engine, get_session
from JFI.session.metadata_store import load_metadata_from_db
from JFI.tool.runbook_tools import runbook_set


def test_a_new_session_starts_from_the_last_sessions_runbook_and_lasting_design(tmp_path):
    engine = get_engine(tmp_path)
    load_metadata_from_db(engine, "first", str(tmp_path))
    runbook_set(engine, "first", "test_one", "uv run pytest {test_id}", "id: tests/test_ops.py::test_add",
                True, "architect")
    with get_session(engine) as db:
        db.add(DesignEntry(session_id="first", kind="convention", key="errors", text="raise CalcError"))
        db.add(DesignEntry(session_id="first", kind="assumption", key="ints", text="integers only"))
        db.commit()

    load_metadata_from_db(engine, "second", str(tmp_path))
    with get_session(engine) as db:
        runbook = db.exec(select(RunbookEntry).where(RunbookEntry.session_id == "second")).all()
        design = db.exec(select(DesignEntry).where(DesignEntry.session_id == "second")).all()
    assert [(r.name, r.command, r.verified) for r in runbook] == [("test_one", "uv run pytest {test_id}", False)]
    assert runbook[0].updated_by == "carried from first"
    assert [(d.kind, d.key) for d in design] == [("convention", "errors")], "a goal's assumptions aren't carried"


def test_resuming_a_session_carries_nothing_again(tmp_path):
    engine = get_engine(tmp_path)
    load_metadata_from_db(engine, "first", str(tmp_path))
    runbook_set(engine, "first", "setup", "uv sync", "", False, "architect")
    load_metadata_from_db(engine, "second", str(tmp_path))
    runbook_set(engine, "second", "setup", "uv sync --frozen", "", False, "architect")
    load_metadata_from_db(engine, "second", str(tmp_path))
    with get_session(engine) as db:
        rows = db.exec(select(RunbookEntry).where(RunbookEntry.session_id == "second")).all()
    assert [r.command for r in rows] == ["uv sync --frozen"]
