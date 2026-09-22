"""JFI.tool.plan_db_tools -- the tool surface that replaces free-form
write_file/append_to_file/replace_in_file text surgery on plan.md (see
that module's own docstring, and /todo.md's "SQLite/Pydantic persistence
rewrite" section). No LLM anywhere here: each tool function is called
directly, the same way runner.py's execute_tool_call would, and the
resulting DB state is asserted through a fresh query -- the same
no-LLM-needed style as test_db_integration.py's dummy session.
"""

import pytest
from sqlmodel import select

from JFI.models import Leaf, LeafStatus, SessionRecord, get_engine, get_session
from JFI.tool.plan_db_tools import (
    add_leaf,
    get_plan,
    make_plan_db_tools,
    mark_leaf_done,
    render_plan_markdown,
    reorder_leaf,
    split_leaf,
    start_leaf,
)


@pytest.fixture()
def engine_and_session(tmp_path):
    session_dir = tmp_path / "JFI" / "demo"
    engine = get_engine(session_dir)
    with get_session(engine) as db:
        db.add(SessionRecord(session_id="demo", repo_path="/tmp/repo"))
        db.commit()
    return engine, "demo"


def _leaves(engine, session_id):
    with get_session(engine) as db:
        return list(db.exec(select(Leaf).where(Leaf.session_id == session_id)))


class TestGetPlan:
    def test_empty_plan_says_so(self, engine_and_session):
        engine, session_id = engine_and_session
        assert "empty" in get_plan(engine, session_id).lower()

    def test_populated_plan_shows_ids_numbers_and_status(self, engine_and_session):
        engine, session_id = engine_and_session
        add_leaf(engine, session_id, "imp", "Core arithmetic")
        [root] = _leaves(engine, session_id)
        add_leaf(engine, session_id, "imp", "Add evaluate()", parent_id=root.id)

        text = get_plan(engine, session_id)
        assert f"[id={root.id}] 1. Core arithmetic" in text
        assert "1.1 Add evaluate()" in text
        assert "## imp" in text


class TestAddLeaf:
    def test_adds_a_root_leaf(self, engine_and_session):
        engine, session_id = engine_and_session
        result = add_leaf(engine, session_id, "imp", "Core arithmetic")
        assert "Added leaf id=" in result
        [leaf] = _leaves(engine, session_id)
        assert leaf.description == "Core arithmetic"
        assert leaf.parent_id is None
        assert leaf.status == LeafStatus.TODO

    def test_adds_a_child_under_a_given_parent(self, engine_and_session):
        engine, session_id = engine_and_session
        add_leaf(engine, session_id, "imp", "Core arithmetic")
        [root] = _leaves(engine, session_id)
        add_leaf(engine, session_id, "imp", "Add evaluate()", parent_id=root.id)

        leaves = _leaves(engine, session_id)
        child = next(leaf for leaf in leaves if leaf.parent_id == root.id)
        assert child.description == "Add evaluate()"

    def test_siblings_get_gap_numbered_sort_keys(self, engine_and_session):
        """10, 20, 30, ... so a future split_leaf can insert between two
        without renumbering anything -- see Leaf's own docstring."""
        engine, session_id = engine_and_session
        add_leaf(engine, session_id, "imp", "First")
        add_leaf(engine, session_id, "imp", "Second")
        add_leaf(engine, session_id, "imp", "Third")

        leaves = sorted(_leaves(engine, session_id), key=lambda leaf: leaf.sort_key)
        assert [leaf.sort_key for leaf in leaves] == [10, 20, 30]

    def test_unknown_phase_is_rejected(self, engine_and_session):
        engine, session_id = engine_and_session
        result = add_leaf(engine, session_id, "bogus-phase", "x")
        assert "Error" in result
        assert _leaves(engine, session_id) == []

    def test_nonexistent_parent_is_rejected(self, engine_and_session):
        engine, session_id = engine_and_session
        result = add_leaf(engine, session_id, "imp", "x", parent_id=999)
        assert "Error" in result
        assert _leaves(engine, session_id) == []

    def test_cannot_parent_under_a_real_leaf_that_already_has_status(self, engine_and_session):
        """A leaf that's already been started/finished is a REAL leaf, not
        a parent -- see Leaf's docstring on the structural distinction.
        Must be split_leaf'd first, not silently given a child."""
        engine, session_id = engine_and_session
        add_leaf(engine, session_id, "imp", "Already done")
        [leaf] = _leaves(engine, session_id)
        mark_leaf_done(engine, session_id, leaf.id)

        result = add_leaf(engine, session_id, "imp", "child", parent_id=leaf.id)
        assert "Error" in result


class TestStartAndMarkLeafDone:
    def test_start_leaf_sets_started_at_and_updates_session_record(self, engine_and_session):
        engine, session_id = engine_and_session
        add_leaf(engine, session_id, "imp", "Core arithmetic")
        [leaf] = _leaves(engine, session_id)

        start_leaf(engine, session_id, leaf.id)

        [leaf] = _leaves(engine, session_id)
        assert leaf.started_at is not None
        with get_session(engine) as db:
            record = db.get(SessionRecord, session_id)
        assert record.current_task == "Core arithmetic"
        assert record.current_task_started_at is not None

    def test_mark_leaf_done_sets_status_and_ended_at_and_clears_current_task(self, engine_and_session):
        engine, session_id = engine_and_session
        add_leaf(engine, session_id, "imp", "Core arithmetic")
        [leaf] = _leaves(engine, session_id)
        start_leaf(engine, session_id, leaf.id)

        mark_leaf_done(engine, session_id, leaf.id, tokens=500)

        [leaf] = _leaves(engine, session_id)
        assert leaf.status == LeafStatus.DONE
        assert leaf.ended_at is not None
        assert leaf.tokens == 500
        with get_session(engine) as db:
            record = db.get(SessionRecord, session_id)
        assert record.current_task is None

    def test_mark_leaf_done_without_start_leaf_still_sets_started_at(self, engine_and_session):
        """A leaf finished in one shot (no explicit start_leaf call) still
        needs a non-null started_at -- timing math elsewhere assumes both
        ends exist together."""
        engine, session_id = engine_and_session
        add_leaf(engine, session_id, "imp", "Core arithmetic")
        [leaf] = _leaves(engine, session_id)

        mark_leaf_done(engine, session_id, leaf.id)

        [leaf] = _leaves(engine, session_id)
        assert leaf.started_at is not None
        assert leaf.started_at == leaf.ended_at

    def test_marking_a_parent_done_is_rejected(self, engine_and_session):
        engine, session_id = engine_and_session
        add_leaf(engine, session_id, "imp", "Core arithmetic")
        [root] = _leaves(engine, session_id)
        add_leaf(engine, session_id, "imp", "child", parent_id=root.id)

        result = mark_leaf_done(engine, session_id, root.id)
        assert "Error" in result
        [root_after] = [leaf for leaf in _leaves(engine, session_id) if leaf.id == root.id]
        assert root_after.status == LeafStatus.TODO

    def test_marking_a_nonexistent_leaf_done_is_rejected(self, engine_and_session):
        engine, session_id = engine_and_session
        assert "Error" in mark_leaf_done(engine, session_id, 999)


class TestSplitLeaf:
    def test_turns_a_leaf_into_a_parent_with_new_children(self, engine_and_session):
        engine, session_id = engine_and_session
        add_leaf(engine, session_id, "imp", "Core arithmetic")
        [leaf] = _leaves(engine, session_id)

        result = split_leaf(engine, session_id, leaf.id, ["Build OPERATORS table", "Write tests"])

        assert "Split leaf" in result
        leaves = _leaves(engine, session_id)
        assert len(leaves) == 3
        parent = next(leaf for leaf in leaves if leaf.id == leaf.id and leaf.parent_id is None)
        assert parent.status == LeafStatus.TODO
        children = [leaf for leaf in leaves if leaf.parent_id == parent.id]
        assert {c.description for c in children} == {"Build OPERATORS table", "Write tests"}

    def test_splitting_clears_any_existing_status_and_timing(self, engine_and_session):
        """The original row stops being a real leaf once it has children --
        see Leaf's own docstring: a parent never carries status/timing."""
        engine, session_id = engine_and_session
        add_leaf(engine, session_id, "imp", "Core arithmetic")
        [leaf] = _leaves(engine, session_id)
        mark_leaf_done(engine, session_id, leaf.id, tokens=100)

        split_leaf(engine, session_id, leaf.id, ["A", "B"])

        [parent] = [leaf for leaf in _leaves(engine, session_id) if leaf.parent_id is None]
        assert parent.status == LeafStatus.TODO
        assert parent.started_at is None
        assert parent.ended_at is None
        assert parent.tokens is None

    def test_splitting_into_fewer_than_two_children_is_rejected(self, engine_and_session):
        engine, session_id = engine_and_session
        add_leaf(engine, session_id, "imp", "Core arithmetic")
        [leaf] = _leaves(engine, session_id)

        result = split_leaf(engine, session_id, leaf.id, ["Only one"])
        assert "Error" in result
        assert len(_leaves(engine, session_id)) == 1

    def test_splitting_a_nonexistent_leaf_is_rejected(self, engine_and_session):
        engine, session_id = engine_and_session
        assert "Error" in split_leaf(engine, session_id, 999, ["A", "B"])


class TestDepthGuardrails:
    """MAX_LEAF_DEPTH / MAX_INVESTIGATION_DEPTH -- the structural backstop
    for the runaway-recursive-splitting failure observed in a real run
    (see plan_db_tools.py's own module-level docstring for the exact
    numbers and the real plan it happened on). Prose guidance alone (the
    Journeyman prompt's own version of this rule) didn't stop it, so these
    are enforced mechanically here, independent of whether the model
    reads/follows the prompt."""

    def _chain(self, engine, session_id, n, description="step"):
        """Builds a straight-line chain of `n` add_leaf calls, each
        parented under the previous one, returning every call's result so
        a test can see exactly which call in the chain first got
        rejected."""
        results = []
        parent_id = 0
        for i in range(n):
            result = add_leaf(engine, session_id, "imp", f"{description} {i}", parent_id=parent_id)
            results.append(result)
            if "Added leaf id=" in result:
                parent_id = int(result.split("id=")[1].split(" ")[0])
            else:
                break
        return results

    def test_ordinary_leaves_allowed_up_to_the_general_depth_cap(self, engine_and_session):
        from JFI.tool.plan_db_tools import MAX_LEAF_DEPTH

        engine, session_id = engine_and_session
        results = self._chain(engine, session_id, MAX_LEAF_DEPTH, description="build the thing")
        assert len(results) == MAX_LEAF_DEPTH
        assert all("Added leaf id=" in r for r in results)

    def test_ordinary_leaves_rejected_past_the_general_depth_cap(self, engine_and_session):
        from JFI.tool.plan_db_tools import MAX_LEAF_DEPTH

        engine, session_id = engine_and_session
        results = self._chain(engine, session_id, MAX_LEAF_DEPTH + 1, description="build the thing")
        assert len(results) == MAX_LEAF_DEPTH + 1
        assert all("Added leaf id=" in r for r in results[:-1])
        assert "Error" in results[-1]
        assert "Stop splitting" in results[-1]

    def test_investigation_leaf_capped_much_shallower(self, engine_and_session):
        from JFI.tool.plan_db_tools import MAX_INVESTIGATION_DEPTH

        engine, session_id = engine_and_session
        results = self._chain(
            engine, session_id, MAX_INVESTIGATION_DEPTH + 1, description="investigate the SectorPie bug"
        )
        assert len(results) == MAX_INVESTIGATION_DEPTH + 1
        assert all("Added leaf id=" in r for r in results[:-1])
        assert "Error" in results[-1]
        assert "investigation" in results[-1].lower()

    def test_investigation_cap_applies_to_descendants_even_without_the_keyword(self, engine_and_session):
        """A child of an investigation leaf is investigation work too, even
        if its own description doesn't repeat the keyword -- the keyword
        check walks every ancestor, not just the immediate parent."""
        engine, session_id = engine_and_session
        add_leaf(engine, session_id, "imp", "Investigate why the KPI cards stay empty")
        [root] = _leaves(engine, session_id)
        add_leaf(engine, session_id, "imp", "Check the fetch layer", parent_id=root.id)
        check_leaf = next(leaf for leaf in _leaves(engine, session_id) if leaf.description == "Check the fetch layer")

        result = split_leaf(engine, session_id, check_leaf.id, ["Probe A", "Probe B"])
        assert "Error" in result

    def test_non_investigation_keyword_leaf_gets_the_general_cap_not_the_strict_one(self, engine_and_session):
        """Sanity check the OTHER direction: an ordinary implementation
        leaf with no investigation wording is never held to the stricter
        cap just because it's nested under something else."""
        engine, session_id = engine_and_session
        results = self._chain(engine, session_id, 3, description="wire up the API client")
        assert all("Added leaf id=" in r for r in results)

    def test_split_leaf_enforces_the_same_general_cap(self, engine_and_session):
        from JFI.tool.plan_db_tools import MAX_LEAF_DEPTH

        engine, session_id = engine_and_session
        results = self._chain(engine, session_id, MAX_LEAF_DEPTH, description="build the thing")
        deepest_id = int(results[-1].split("id=")[1].split(" ")[0])

        result = split_leaf(engine, session_id, deepest_id, ["A", "B"])
        assert "Error" in result
        # Rejected split must not have mutated the leaf into a parent.
        [deepest] = [leaf for leaf in _leaves(engine, session_id) if leaf.id == deepest_id]
        assert deepest.status == LeafStatus.TODO
        assert _leaves(engine, session_id).__len__() == MAX_LEAF_DEPTH  # no new children created


class TestRenderPlanMarkdown:
    """render_plan_markdown must match plan.md's OLD bullet syntax
    byte-for-byte (no [id=N] tags) -- it's consumed by two things that
    already parse that exact format unchanged: frontend/src/main.js's
    parsePlanLines/buildPlanTree (the fleet dashboard checklist) and the
    Streamlit dashboard's st.markdown render. A regression here breaks
    both UIs silently, not just this module's own callers."""

    def test_empty_plan_renders_as_empty_string(self, engine_and_session):
        engine, session_id = engine_and_session
        assert render_plan_markdown(engine, session_id) == ""

    def test_matches_plan_md_bullet_syntax_exactly(self, engine_and_session):
        engine, session_id = engine_and_session
        add_leaf(engine, session_id, "imp", "Core arithmetic")
        add_leaf(engine, session_id, "imp", "Add evaluate()", parent_id=1)
        mark_leaf_done(engine, session_id, 2)

        md = render_plan_markdown(engine, session_id)

        assert md == "## Implementation\n- 1. Core arithmetic\n  - [x] 1.1 Add evaluate()\n"

    def test_no_id_tags_anywhere_in_output(self, engine_and_session):
        """The whole point of this renderer -- [id=N] tags are for the
        model's own tool calls (see get_plan), never for a human-facing
        or machine-parsed-as-plan.md view."""
        engine, session_id = engine_and_session
        add_leaf(engine, session_id, "imp", "Core arithmetic")

        assert "[id=" not in render_plan_markdown(engine, session_id)

    def test_both_phases_render_as_separate_sections(self, engine_and_session):
        engine, session_id = engine_and_session
        add_leaf(engine, session_id, "imp", "Core arithmetic")
        add_leaf(engine, session_id, "testing", "Build gate")

        md = render_plan_markdown(engine, session_id)

        assert "## Implementation" in md
        assert "## Testing" in md
        assert md.index("## Implementation") < md.index("## Testing")

    def test_only_imp_and_testing_leaves_render_others_are_ignored(self, engine_and_session):
        """planner/product_owner/reviewer/cleanup leaves (if any ever
        exist) never had a plan.md checklist section under the old
        format either -- see PHASE_SECTION's own comment on why only
        these two phases ever had one."""
        engine, session_id = engine_and_session
        add_leaf(engine, session_id, "reviewer", "Not a checklist phase")
        add_leaf(engine, session_id, "imp", "Real checklist item")

        md = render_plan_markdown(engine, session_id)

        assert "Not a checklist phase" not in md
        assert "Real checklist item" in md

    def test_skipped_leaf_uses_the_circle_marker(self, engine_and_session):
        from JFI.models import Leaf, LeafStatus, get_session

        engine, session_id = engine_and_session
        add_leaf(engine, session_id, "imp", "Core arithmetic")
        with get_session(engine) as db:
            leaf = db.get(Leaf, 1)
            leaf.status = LeafStatus.SKIPPED
            db.add(leaf)
            db.commit()

        assert "- [○] 1 Core arithmetic" in render_plan_markdown(engine, session_id)

    def test_a_parent_with_no_children_in_that_phase_is_omitted_from_output(self, engine_and_session):
        """A phase with zero root leaves renders no section at all -- an
        empty '## Testing' header with nothing under it would be
        confusing noise, not useful signal."""
        engine, session_id = engine_and_session
        add_leaf(engine, session_id, "imp", "Core arithmetic")

        md = render_plan_markdown(engine, session_id)

        assert "## Testing" not in md


class TestReorderLeaf:
    def _add_three(self, engine, session_id):
        add_leaf(engine, session_id, "imp", "First")
        add_leaf(engine, session_id, "imp", "Second")
        add_leaf(engine, session_id, "imp", "Third")
        leaves = sorted(_leaves(engine, session_id), key=lambda leaf: leaf.sort_key)
        return {leaf.description: leaf.id for leaf in leaves}

    def test_moving_to_the_front_with_no_after_leaf_id(self, engine_and_session):
        engine, session_id = engine_and_session
        ids = self._add_three(engine, session_id)

        reorder_leaf(engine, session_id, ids["Third"])

        ordered = sorted(_leaves(engine, session_id), key=lambda leaf: leaf.sort_key)
        assert [leaf.description for leaf in ordered] == ["Third", "First", "Second"]

    def test_moving_right_after_a_given_sibling(self, engine_and_session):
        engine, session_id = engine_and_session
        ids = self._add_three(engine, session_id)

        reorder_leaf(engine, session_id, ids["First"], after_leaf_id=ids["Second"])

        ordered = sorted(_leaves(engine, session_id), key=lambda leaf: leaf.sort_key)
        assert [leaf.description for leaf in ordered] == ["Second", "First", "Third"]

    def test_rebalances_sort_keys_to_a_fresh_gap_sequence(self, engine_and_session):
        """Every sibling gets a new 10/20/30/... sort_key in the new order
        -- not just the moved leaf -- so a later insert always has room."""
        engine, session_id = engine_and_session
        ids = self._add_three(engine, session_id)

        reorder_leaf(engine, session_id, ids["Third"])

        ordered = sorted(_leaves(engine, session_id), key=lambda leaf: leaf.sort_key)
        assert [leaf.sort_key for leaf in ordered] == [10, 20, 30]

    def test_reordering_a_nonexistent_leaf_is_rejected(self, engine_and_session):
        engine, session_id = engine_and_session
        assert "Error" in reorder_leaf(engine, session_id, 999)

    def test_after_leaf_id_must_be_a_real_sibling(self, engine_and_session):
        engine, session_id = engine_and_session
        ids = self._add_three(engine, session_id)

        result = reorder_leaf(engine, session_id, ids["First"], after_leaf_id=999)
        assert "Error" in result
        assert "not a sibling" in result

    def test_after_leaf_id_must_share_the_same_parent(self, engine_and_session):
        engine, session_id = engine_and_session
        ids = self._add_three(engine, session_id)
        add_leaf(engine, session_id, "testing", "Unrelated, different phase")
        other_phase_id = next(leaf.id for leaf in _leaves(engine, session_id) if leaf.description == "Unrelated, different phase")

        result = reorder_leaf(engine, session_id, ids["First"], after_leaf_id=other_phase_id)
        assert "Error" in result


class TestMakePlanDbTools:
    def test_returns_bound_callables_for_every_tool(self, engine_and_session):
        engine, session_id = engine_and_session
        tools = make_plan_db_tools(engine, session_id)

        assert set(tools) == {"get_plan", "add_leaf", "start_leaf", "mark_leaf_done", "split_leaf", "reorder_leaf"}
        assert "Added leaf" in tools["add_leaf"](phase="imp", description="Core arithmetic")
        assert "Core arithmetic" in tools["get_plan"]()
