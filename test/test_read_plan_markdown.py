"""runner._read_plan_markdown -- what feeds AbstractManager.set_status's
plan_markdown param, which the fleet dashboard's socket_reporter relays
over the wire. Prefers the DB the same has_leaves()-gated way every other
plan.md reader in this migration does (see SimpleSessionManager.
plan_progress's own docstring) -- falls back to reading plan.md directly
for a session that predates the DB migration.
"""

from JFI.runner import _read_plan_markdown
from JFI.tool.plan_db_tools import add_leaf, mark_leaf_done


class TestReadPlanMarkdown:
    def test_falls_back_to_plan_md_when_the_db_has_no_leaves(self, manager):
        manager.plan_file.parent.mkdir(parents=True, exist_ok=True)
        manager.plan_file.write_text("## Implementation\n- [ ] 1.1 pending thing\n")

        assert _read_plan_markdown(manager) == "## Implementation\n- [ ] 1.1 pending thing\n"

    def test_returns_empty_string_when_neither_db_nor_file_has_anything(self, manager):
        assert _read_plan_markdown(manager) == ""

    def test_prefers_the_db_once_a_leaf_exists_rendered_in_plan_md_syntax(self, manager):
        """Byte-for-byte plan.md bullet syntax (no [id=N] tags) -- so
        frontend/src/main.js's existing parser keeps working unchanged
        against a DB-backed session's checklist."""
        manager.plan_file.parent.mkdir(parents=True, exist_ok=True)
        manager.plan_file.write_text("## Implementation\n- [ ] 1.1 stale plan.md item\n")

        add_leaf(manager.db_engine, manager.session_id, "imp", "Core arithmetic")
        mark_leaf_done(manager.db_engine, manager.session_id, 1)

        result = _read_plan_markdown(manager)
        assert result == "## Implementation\n- [x] 1 Core arithmetic\n"
        assert "stale plan.md item" not in result
        assert "[id=" not in result
