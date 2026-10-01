"""
Tests for score.py against a real `.jfi/JFI.db` built with JFI's own models
(score.py itself reads it with plain sqlite3, so a schema drift shows up
here):

    uv run pytest benchmark/test_score.py

Not wired into the main `uv run pytest` (pyproject.toml's testpaths is
"test" only) -- benchmark/ is a separate offline tool.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import score  # noqa: E402
from JFI.models import Episode, HistoryMessage, Leaf, PlanEvent, PlannerVerdict, get_engine, get_session  # noqa: E402
from JFI.models.enums import LeafStatus, Phase  # noqa: E402
from JFI.session.metadata_store import load_metadata_from_db  # noqa: E402


def _project(tmp_path: Path) -> Path:
    engine = get_engine(tmp_path)
    load_metadata_from_db(engine, "calc", str(tmp_path))
    with get_session(engine) as db:
        root = Leaf(session_id="calc", phase=Phase.IMP, description="calc package", level="architect")
        db.add(root)
        db.commit()
        db.refresh(root)
        for i, status in enumerate((LeafStatus.DONE, LeafStatus.DONE, LeafStatus.TODO)):
            db.add(Leaf(session_id="calc", phase=Phase.IMP, parent_id=root.id, sort_key=i, level="task",
                        description=f"leaf {i}", status=status, attempt_count=1, reopened_count=i == 1))
        db.add(PlannerVerdict(session_id="calc", node_id=root.id, level="architect", final_status="BREAKDOWN",
                              decided_by="agree"))
        db.add(Episode(session_id="calc", role="dev", mode="leaf", turns=12, tokens=9000, end_reason="done"))
        db.add(Episode(session_id="calc", role="dev", mode="leaf", turns=25, tokens=20000, end_reason="turn_cap"))
        db.add(PlanEvent(session_id="calc", node_id=root.id, type="overflow"))
        db.add(HistoryMessage(session_id="calc", seq=1, role="assistant", content="PLANNER_COMPLETE"))
        db.add(HistoryMessage(session_id="calc", seq=2, role="assistant", content="",
                              tool_calls=[{"id": "1", "function": {"name": "add_node", "arguments": "{}"}},
                                          {"id": "2", "function": {"name": "add_node", "arguments": "{}"}}]))
        db.add(HistoryMessage(session_id="calc", seq=3, role="tool", name="add_node", tool_call_id="1",
                              content="Error: the description is over 200 characters"))
        db.add(HistoryMessage(session_id="calc", seq=4, role="tool", name="add_node", tool_call_id="2",
                              content="Added node 5"))
        db.commit()
    return tmp_path


def test_scores_a_session_from_its_database(tmp_path):
    report = score.score_session(_project(tmp_path), "calc", {"ran": True, "passed": False})

    assert report["session_id"] == "calc" and report["phases_completed"] == ["planner"]
    assert {k: report["plan"][k] for k in ("nodes", "leaves", "done", "left", "max_depth")} == \
        {"nodes": 4, "leaves": 3, "done": 2, "left": 1, "max_depth": 2}
    assert report["judge"]["decided_by"] == {"agree": 1}
    assert report["episodes"]["overflows"] == 1 and report["episodes"]["turns"] == 37
    assert report["dev"] == {"attempts": 3, "reopened": 1, "overflow_events": 1, "deferrals": 0,
                             "events": {"overflow": 1}}
    assert (report["tools"]["tool_calls_total"], report["tools"]["tool_call_error_rate"]) == (2, 0.5)
    assert report["tools"]["tool_errors_by_name"] == {"add_node": 1}
    flags = " | ".join(report["flags"])
    assert "phases not completed: imp, reviewer, cleanup" in flags and "1 of 3 leaves never finished" in flags
    assert "50% of tool calls failed" in flags and "objective hidden-test verification" in flags


def test_a_project_where_jfi_never_ran(tmp_path):
    report = score.score_session(tmp_path, "calc", None)
    assert report["session_id"] is None and report["flags"][0].startswith("no JFI session")
    assert not (tmp_path / ".jfi").exists(), "scoring never creates a database"
