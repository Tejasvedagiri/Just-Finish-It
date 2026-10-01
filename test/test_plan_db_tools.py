"""JFI.tool.plan_db_tools -- the read-only plan tools the reviewer uses
(get_plan, get_leaf) and the markdown renderer the dashboards parse. The
plan is written by the planner's node tools and Dev's gated mark_leaf_done
(tested in test_planner.py / test_imp.py); here leaves are inserted
directly, the same rows those tools write. No LLM anywhere.
"""

from datetime import datetime, timezone

import pytest
from sqlmodel import select

from JFI.models import Leaf, LeafStatus, Phase, SessionRecord, get_engine, get_session
from JFI.tool.plan_db_tools import get_leaf, get_plan, make_plan_db_tools, render_plan_markdown


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


def add_leaf(engine, session_id, phase, description, parent_id=None):
    with get_session(engine) as db:
        siblings = db.exec(select(Leaf).where(Leaf.session_id == session_id, Leaf.parent_id == parent_id)).all()
        leaf = Leaf(session_id=session_id, parent_id=parent_id, phase=Phase(phase),
                    sort_key=(len(siblings) + 1) * 10, description=description)
        db.add(leaf)
        db.commit()
        return leaf.id


def mark_leaf_done(engine, session_id, leaf_id, tokens=None):
    with get_session(engine) as db:
        leaf = db.get(Leaf, leaf_id)
        leaf.status = LeafStatus.DONE
        leaf.started_at = leaf.ended_at = datetime.now(timezone.utc)
        leaf.tokens = tokens
        db.add(leaf)
        db.commit()


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


class TestGetLeaf:
    def test_shows_full_detail_for_a_genuine_leaf(self, engine_and_session):
        engine, session_id = engine_and_session
        add_leaf(engine, session_id, "imp", "Core arithmetic")
        [leaf] = _leaves(engine, session_id)
        mark_leaf_done(engine, session_id, leaf.id, tokens=42)

        text = get_leaf(engine, session_id, leaf.id)

        assert f"[id={leaf.id}] 1 Core arithmetic" in text
        assert "phase: imp" in text
        assert "[x] done" in text
        assert "parent: none (top-level)" in text
        assert "children: none (genuine leaf)" in text
        assert "tokens: 42" in text
        assert "started_at: -" not in text  # was actually set

    def test_shows_parent_and_children_for_a_parent_leaf(self, engine_and_session):
        engine, session_id = engine_and_session
        add_leaf(engine, session_id, "imp", "Core arithmetic")
        [root] = _leaves(engine, session_id)
        add_leaf(engine, session_id, "imp", "Add evaluate()", parent_id=root.id)
        [child] = [leaf for leaf in _leaves(engine, session_id) if leaf.parent_id == root.id]

        root_text = get_leaf(engine, session_id, root.id)
        assert "parent: none (top-level)" in root_text
        assert f"[id={child.id}]" in root_text
        assert "1 children" in root_text

        child_text = get_leaf(engine, session_id, child.id)
        assert f"parent: [id={root.id}] 1 Core arithmetic" in child_text
        assert "children: none (genuine leaf)" in child_text

    def test_rejects_a_nonexistent_leaf(self, engine_and_session):
        engine, session_id = engine_and_session
        assert "Error" in get_leaf(engine, session_id, 999)


class TestRenderPlanMarkdown:
    """render_plan_markdown must match plan.md's OLD bullet syntax
    byte-for-byte (no [id=N] tags) -- it's consumed by two things that
    already parse that exact format unchanged: Just-Finish-It-Fleet's src/main.js's
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


class TestPlanJudgeRows:
    """The dashboard's Task | Judge table: the rule ("Judge") and Laya side
    by side, the LLM tie-break's pick, and what decided. Observed on stui run
    12: an older row stored Laya's raw letter "A" -- the table must still read
    GOOD / BREAKDOWN / REDO for those."""

    def test_one_row_per_node_in_plan_order_with_both_scores(self, engine_and_session):
        from JFI.models import PlannerVerdict

        engine, session_id = engine_and_session
        parent = add_leaf(engine, session_id, "imp", "Scaffold: package.json, index.html, .gitignore")
        child = add_leaf(engine, session_id, "imp", "setRange()", parent_id=parent)
        legacy = add_leaf(engine, session_id, "imp", "Range filter module")
        with get_session(engine) as db:
            for leaf_id, status in ((parent, "BREAKDOWN"), (child, "GOOD"), (legacy, "BREAKDOWN")):
                leaf = db.get(Leaf, leaf_id)
                leaf.plan_status = status
                db.add(leaf)
            leaf = db.get(Leaf, child)
            leaf.notes, leaf.references = "clamp to the data's range", ["contract:range", "app.js L10-40"]
            db.add(leaf)
            db.add(PlannerVerdict(session_id=session_id, node_id=parent, level="architect", laya_model="english",
                                  laya_verdict="GOOD", answer_confidence=0.95, rule_verdict="BREAKDOWN",
                                  tiebreak_verdict="BREAKDOWN", decided_by="llm", final_status="BREAKDOWN"))
            db.add(PlannerVerdict(session_id=session_id, node_id=child, level="lead", laya_model="english",
                                  laya_verdict="GOOD", answer_confidence=0.6, rule_verdict="GOOD",
                                  decided_by="agree", final_status="GOOD"))
            db.add(PlannerVerdict(session_id=session_id, node_id=legacy, level="architect", laya_model="english",
                                  laya_verdict="A", answer_confidence=0.64, fallback="low_confidence",
                                  final_status="BREAKDOWN"))
            db.commit()

        from JFI.tool.plan_db_tools import plan_judge_rows
        rows = plan_judge_rows(engine, session_id)

        assert [r["#"] for r in rows] == ["1", "1.1", "2"]
        assert (rows[0]["Judge"], rows[0]["Laya"], rows[0]["LLM"], rows[0]["Final"], rows[0]["Decided by"]) == \
            ("BREAKDOWN", "GOOD (0.95)", "BREAKDOWN", "BREAKDOWN", "llm")
        assert rows[1]["Task"] == "· setRange()" and rows[1]["Decided by"] == "agree"
        assert rows[1]["Status"] == "todo" and rows[0]["Status"] == ""
        assert (rows[2]["Laya"], rows[2]["Decided by"]) == ("GOOD (0.64)", "rule")
        assert rows[1]["Notes"] == "clamp to the data's range"
        assert rows[1]["References"] == "contract:range, app.js L10-40"
        assert (rows[0]["Notes"], rows[0]["References"]) == ("", "")


class TestPlanStatusForTheDashboards:
    """Phase 10: the planner / Dev / reviewer push the plan fields as they
    change the plan (during episodes the header and dashboards used to stand
    still -- only the old turn loop pushed them). The plan text itself stays
    plain; the judge's scores live in the Task | Judge table."""

    FLEET_LINE = __import__("re").compile(r"^(\s*)- \[(.)\] ([\d.]+) (.*)$")  # parsePlanLines' leaf shape

    def test_plan_lines_carry_no_status(self, engine_and_session):
        """The user: "I don't need to know if it's GOOD or BREAKDOWN in the
        Full plan. The table has it." """
        engine, session_id = engine_and_session
        leaf_id = add_leaf(engine, session_id, "imp", "Core arithmetic")
        mark_leaf_done(engine, session_id, leaf_id)
        with get_session(engine) as db:
            leaf = db.get(Leaf, leaf_id)
            leaf.plan_status, leaf.review_status = "GOOD", "passed"
            db.add(leaf)
            db.commit()

        line = render_plan_markdown(engine, session_id).splitlines()[1]
        assert line == "- [x] 1 Core arithmetic"
        assert self.FLEET_LINE.match(line).group(3) == "1"

    def test_a_broken_down_node_shows_breakdown_not_good_in_the_table(self, engine_and_session):
        """Observed on stui run 15: the planner marks a node GOOD once it has
        been split (settled), so the CSS component the Lead had just split
        into files read GOOD -- as if it were ready to build."""
        from JFI.tool.plan_db_tools import plan_judge_rows
        engine, session_id = engine_and_session
        parent = add_leaf(engine, session_id, "imp", "Create the CSS files")
        child = add_leaf(engine, session_id, "imp", "Fill variables.css", parent_id=parent)
        with get_session(engine) as db:
            for leaf_id in (parent, child):
                leaf = db.get(Leaf, leaf_id)
                leaf.plan_status = "GOOD"
                db.add(leaf)
            db.commit()

        assert [r["Final"] for r in plan_judge_rows(engine, session_id)] == ["BREAKDOWN", "GOOD"]

    def test_status_fields_are_what_set_status_takes(self, engine_and_session):
        from JFI.tool.plan_db_tools import plan_status_fields
        engine, session_id = engine_and_session
        first = add_leaf(engine, session_id, "imp", "Core arithmetic")
        add_leaf(engine, session_id, "imp", "Parse input")
        mark_leaf_done(engine, session_id, first)

        fields = plan_status_fields(engine, session_id)
        assert (fields["plan"], fields["phase_plan"]) == ((1, 2), (1, 2))
        assert fields["plan_markdown"].startswith("## Implementation")

    def test_status_fields_carry_the_judge_table_for_the_fleet(self, engine_and_session):
        """The fleet dashboard has no DB access: its Plan view showed only the
        markdown checklist while Streamlit had the Task | Judge table, runbook
        and design. The same rows now ride along in the status snapshot."""
        from JFI.models import DesignEntry, RunbookEntry
        from JFI.tool.plan_db_tools import plan_status_fields
        engine, session_id = engine_and_session
        add_leaf(engine, session_id, "imp", "Core arithmetic")
        with get_session(engine) as db:
            db.add(RunbookEntry(session_id=session_id, name="test", command="npx vitest run"))
            db.add(DesignEntry(session_id=session_id, kind="stack", key="lang", text="JavaScript"))
            db.commit()

        detail = plan_status_fields(engine, session_id)["plan_detail"]
        assert [r["Task"] for r in detail["rows"]] == ["Core arithmetic"]
        assert detail["runbook"] == [{"Name": "test", "Command": "npx vitest run", "Verified": ""}]
        assert detail["design"] == [{"Kind": "stack", "Key": "lang", "Text": "JavaScript"}]


class TestMakePlanDbTools:
    def test_only_the_read_tools_are_bound(self, engine_and_session):
        """The v1 plan-editing tools (add_leaf, split_leaf, review_leaf, ...)
        were removed with the v1 pipeline; the reviewer only reads."""
        engine, session_id = engine_and_session
        add_leaf(engine, session_id, "imp", "Core arithmetic")
        tools = make_plan_db_tools(engine, session_id)

        assert set(tools) == {"get_plan", "get_leaf"}
        assert "Core arithmetic" in tools["get_plan"]()


def test_notes_become_points_with_numbered_steps():
    """Observed 2026-10-01: a node's notes are one long paragraph with steps
    "(1) ... (5)" inside, unreadable as a single box in the dashboards; and
    code like "str (e) == e.message" must not become step "e"."""
    from JFI.tool.plan_db_tools import note_points

    notes = ("Define CalcError(Exception) with .message, str (e) == e.message. Steps: (1) strip the line; "
             "(2) split into exactly 3 tokens, e.g. '2 + 2'; (3) compute (a ** b). Edge cases: 0 ** 0 is fine.")
    assert note_points(notes) == [
        ("", "Define CalcError(Exception) with .message, str (e) == e.message."), ("", "Steps:"),
        ("1", "strip the line"), ("2", "split into exactly 3 tokens, e.g. '2 + 2'"),
        ("3", "compute (a ** b)."), ("", "Edge cases: 0 ** 0 is fine.")]
    assert note_points(None) == [] and note_points("One sentence") == [("", "One sentence")]
