"""SimpleSessionManager.plan_progress/phase_progress/_pending_items/
skip_current_task/skip_remaining_tasks/current_task_title all prefer the
DB (JFI.models.Leaf) over plan.md's regex-scanned text once this session's
plan lives there -- see each method's own docstring for why both paths
coexist (an additive migration, not a flag-day cutover, since ~81
pre-existing tests across test_get_system_message.py/test_plan_location.py/
test_plan_parsing.py/test_stuck_task_decomposition.py/test_terminal_title.py
construct plan.md text directly and must keep passing unchanged).

This file is the DB-path counterpart to those: same methods, but with
add_leaf/mark_leaf_done driving state instead of plan.md text, called
through a REAL SimpleSessionManager instance (not the standalone
plan_db_tools functions test_plan_db_tools.py already covers) -- proving
the has_leaves()-gated branching inside each method is wired correctly,
not just that the underlying DB functions work in isolation.
"""

from JFI.tool.plan_db_tools import add_leaf, mark_leaf_done


def _add(manager, phase, description, parent_id=0):
    result = add_leaf(manager.db_engine, manager.session_id, phase, description, parent_id)
    return int(result.split("id=")[1].split(" ")[0])


class TestPlanAndPhaseProgress:
    def test_falls_back_to_plan_md_when_the_db_has_no_leaves(self, make_manager):
        manager = make_manager("demo")
        manager.plan_file.parent.mkdir(parents=True, exist_ok=True)
        manager.plan_file.write_text(
            "## Implementation\n- [x] 1.1 done thing\n- [ ] 1.2 pending thing\n"
        )
        assert manager.plan_progress() == (1, 2)
        assert manager.phase_progress("imp") == (1, 2)

    def test_prefers_the_db_once_a_leaf_exists_even_with_a_stale_plan_md_present(self, make_manager):
        """A plan.md could still be sitting on disk (leftover, or never
        cleaned up) -- the moment the DB has a leaf, it must win, not the
        file, or the two sources of truth could silently disagree."""
        manager = make_manager("demo")
        manager.plan_file.parent.mkdir(parents=True, exist_ok=True)
        manager.plan_file.write_text("## Implementation\n- [ ] 1.1 stale plan.md item\n")

        _add(manager, "imp", "Core arithmetic")

        assert manager.plan_progress() == (0, 1)
        assert manager.phase_progress("imp") == (0, 1)

    def test_progress_updates_as_leaves_are_marked_done(self, make_manager):
        manager = make_manager("demo")
        leaf_id = _add(manager, "imp", "Core arithmetic")
        _add(manager, "testing", "Build gate")

        assert manager.plan_progress() == (0, 2)
        mark_leaf_done(manager.db_engine, manager.session_id, leaf_id)
        assert manager.plan_progress() == (1, 2)
        assert manager.phase_progress("imp") == (1, 1)
        assert manager.phase_progress("testing") == (0, 1)


class TestPendingItemsAndCurrentTaskTitle:
    def test_pending_items_returns_leaf_id_tagged_lines_from_the_db(self, make_manager):
        manager = make_manager("demo")
        _add(manager, "imp", "Core arithmetic")
        _add(manager, "testing", "Build gate")

        pending = manager._pending_items("Implementation")
        assert len(pending) == 1
        assert "Core arithmetic" in pending[0]
        assert "[id=" in pending[0]

    def test_current_task_title_returns_a_clean_description_not_the_tagged_line(self, make_manager):
        manager = make_manager("demo")
        _add(manager, "imp", "Verify .env loading happens before theme resolution")

        title = manager.current_task_title("imp")
        assert title == "Verify .env loading happens before theme resolution"
        assert "[id=" not in title

    def test_current_task_title_truncates_long_descriptions(self, make_manager):
        manager = make_manager("demo")
        _add(manager, "imp", "x" * 200)

        title = manager.current_task_title("imp", max_len=20)
        assert len(title) == 20
        assert title.endswith("…")

    def test_current_task_title_is_none_when_nothing_pending(self, make_manager):
        manager = make_manager("demo")
        assert manager.current_task_title("imp") is None


class TestSkipCurrentAndSkipRemaining:
    def test_skip_current_task_skips_the_first_pending_leaf_only(self, make_manager):
        manager = make_manager("demo")
        _add(manager, "imp", "First")
        _add(manager, "imp", "Second")

        skipped = manager.skip_current_task("imp")
        assert skipped == "First"
        assert manager.phase_progress("imp") == (1, 2)
        assert manager.current_task_title("imp") == "Second"

    def test_skip_current_task_returns_none_when_nothing_pending(self, make_manager):
        manager = make_manager("demo")
        assert manager.skip_current_task("imp") is None

    def test_skip_remaining_tasks_skips_everything_pending_in_that_phase_only(self, make_manager):
        manager = make_manager("demo")
        _add(manager, "imp", "First")
        _add(manager, "imp", "Second")
        _add(manager, "testing", "Untouched")

        count = manager.skip_remaining_tasks("imp")
        assert count == 2
        assert manager.phase_progress("imp") == (2, 2)
        assert manager.phase_progress("testing") == (0, 1)
