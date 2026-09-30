"""Plan progress and the built-files summary, read from the DB through a
REAL SimpleSessionManager's engine."""

from JFI.models import Leaf, get_session
from JFI.tool.plan_db_tools import phase_progress_db, plan_progress_db
from test.test_plan_db_tools import add_leaf, mark_leaf_done


def _add(manager, description, files=None, parent_id=None):
    leaf_id = add_leaf(manager.db_engine, manager.session_id, "imp", description, parent_id=parent_id)
    if files:
        with get_session(manager.db_engine) as db:
            leaf = db.get(Leaf, leaf_id)
            leaf.files = files
            db.add(leaf)
            db.commit()
    return leaf_id


class TestPlanAndPhaseProgress:
    def test_progress_counts_only_leaves_and_updates_as_they_finish(self, make_manager):
        manager = make_manager("demo")
        parent = _add(manager, "Calculator")
        first = _add(manager, "Core arithmetic", parent_id=parent)
        _add(manager, "Parse input", parent_id=parent)
        engine, sid = manager.db_engine, manager.session_id

        assert plan_progress_db(engine, sid) == (0, 2)
        mark_leaf_done(engine, sid, first)
        assert plan_progress_db(engine, sid) == (1, 2)
        assert phase_progress_db(engine, sid, "imp") == (1, 2)
        assert phase_progress_db(engine, sid, "reviewer") == (0, 0)


class TestProjectStateSummary:
    def test_a_re_plan_is_told_which_files_were_built(self, make_manager):
        """Observed after the v2 cutover: the summary listed files the old
        file tools recorded with track_file, which v2's code tools never
        call -- so every failed review's re-plan was told "No files have
        been tracked yet." It now comes from the done leaves' files."""
        manager = make_manager("demo")
        assert manager.get_project_state_summary() == "No leaf has been built yet."

        done = _add(manager, "Core arithmetic", files=["src/calc.js", "test/calc.test.js"])
        _add(manager, "Parse input", files=["src/parse.js"])
        also_done = _add(manager, "Wire up", files=["src/calc.js", "src/main.js"])
        mark_leaf_done(manager.db_engine, manager.session_id, done)
        mark_leaf_done(manager.db_engine, manager.session_id, also_done)

        assert manager.get_project_state_summary() == (
            "Files built so far:\n- src/calc.js\n- test/calc.test.js\n- src/main.js")
