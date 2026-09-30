"""Read access to the plan tree (JFI.models.Leaf), plus the progress and
markdown helpers the status bar, dashboard and export use.

The planner writes the tree through its own node tools (JFI.planner.nodes)
and Dev finishes leaves through its gated mark_leaf_done (JFI.imp.dev). What
is left here for the model is read-only: get_plan (the whole tree) and
get_leaf (one leaf's full detail), which the reviewer uses. runner.py wires
them into TOOL_MAP per session -- see make_plan_db_tools at the bottom.
"""

from typing import Callable, Dict, Optional

from sqlmodel import select

from JFI.models import Leaf, LeafStatus, Phase, build_indexes, display_number, get_session

# The plan-markdown sections; the fleet's parsePlanLines keys on these
# headings. v2 plans only have imp leaves; testing stays so a v1 session's
# plan still renders in export-db.
_MARKDOWN_PHASE_SECTIONS = {"imp": "Implementation", "testing": "Testing"}

def _load_leaves(engine, session_id: str) -> list[Leaf]:
    with get_session(engine) as db:
        return list(db.exec(select(Leaf).where(Leaf.session_id == session_id)))


def _render_plan(engine, session_id: str) -> str:
    leaves = _load_leaves(engine, session_id)
    if not leaves:
        return "The plan is empty. Use add_leaf to start building it."

    by_id, siblings_by_parent = build_indexes(leaves)

    lines = []

    # A leaf's checkbox depends on whether it HAS children, never on its
    # depth -- root-level leaves are exactly as likely to be genuine (no
    # children yet) as any other, so this must run at depth 0 too, not
    # just recursively below it (an earlier version had a separate,
    # unconditional root-printing loop here that always omitted the
    # checkbox, which is wrong the moment a root-level item has no
    # children of its own -- caught by test_plan_db_tools_wiring.py).
    def render_subtree(parent_id, phase, depth: int) -> None:
        for leaf in siblings_by_parent.get((parent_id, phase), []):
            children = siblings_by_parent.get((leaf.id, phase), [])
            number = display_number(leaf, by_id, siblings_by_parent)
            indent = "  " * depth
            if children:
                lines.append(f"{indent}[id={leaf.id}] {number}. {leaf.description}")
            else:
                mark = {"done": "x", "skipped": "o"}.get(leaf.status.value, " ")
                lines.append(f"{indent}[id={leaf.id}] [{mark}] {number} {leaf.description}")
            render_subtree(leaf.id, phase, depth + 1)

    phases_in_order = []
    for leaf in leaves:
        if leaf.parent_id is None and leaf.phase not in phases_in_order:
            phases_in_order.append(leaf.phase)

    for phase in phases_in_order:
        lines.append(f"## {phase.value}")
        render_subtree(None, phase, 0)
        lines.append("")

    return "\n".join(lines).strip()


def display_status(plan_status: Optional[str], has_children: bool) -> Optional[str]:
    """The status a person should see. The planner marks a node GOOD once it
    has been broken down ("settled", so planning can finish), which read as
    "ready to build" on every parent -- observed on stui run 15, where the
    CSS component the Lead had just split into files showed "· GOOD". A
    parent shows the verdict that made it one: BREAKDOWN."""
    if has_children and plan_status:
        return "BREAKDOWN"
    return plan_status


def render_plan_markdown(engine, session_id: str) -> str:
    """Renders the plan tree as the old plan.md bullet syntax (no [id=N]
    tags, which only mean something to a model calling the tools):
    Just-Finish-It-Fleet's parsePlanLines/buildPlanTree (the fleet's
    checklist tab) parses exactly this, and the Streamlit dashboard shows it
    as the Full plan.

    No judge or review status here: the dashboard's Task | Judge table
    (plan_judge_rows) shows those, and the user asked to keep the plan itself
    plain.
    """
    leaves = _load_leaves(engine, session_id)
    if not leaves:
        return ""

    by_id, siblings_by_parent = build_indexes(leaves)
    lines = []

    def render_subtree(parent_id, phase, depth: int) -> None:
        for leaf in siblings_by_parent.get((parent_id, phase), []):
            children = siblings_by_parent.get((leaf.id, phase), [])
            number = display_number(leaf, by_id, siblings_by_parent)
            indent = "  " * depth
            if children:
                lines.append(f"{indent}- {number}. {leaf.description}")
            else:
                mark = {"done": "x", "skipped": "○"}.get(leaf.status.value, " ")
                lines.append(f"{indent}- [{mark}] {number} {leaf.description}")
            render_subtree(leaf.id, phase, depth + 1)

    rendered_any = False
    for phase_key, section_name in _MARKDOWN_PHASE_SECTIONS.items():
        phase_enum = Phase(phase_key)
        if not siblings_by_parent.get((None, phase_enum)):
            continue
        rendered_any = True
        lines.append(f"## {section_name}")
        render_subtree(None, phase_enum, 0)
        lines.append("")

    return ("\n".join(lines).strip() + "\n") if rendered_any else ""


def _iter_leaves_in_document_order(leaves: list[Leaf], phase) -> list[Leaf]:
    """Genuine leaves (no children of their own) within one phase, in plan
    order: a depth-first walk, each level sorted by sort_key."""
    by_id, siblings_by_parent = build_indexes(leaves)
    ordered: list[Leaf] = []

    def walk(parent_id) -> None:
        for leaf in siblings_by_parent.get((parent_id, phase), []):
            children = siblings_by_parent.get((leaf.id, phase), [])
            if children:
                walk(leaf.id)
            else:
                ordered.append(leaf)

    walk(None)
    return ordered


def plan_progress_db(engine, session_id: str) -> tuple[int, int]:
    """(resolved, total) leaves across every phase. A DONE or SKIPPED leaf
    counts as resolved: a skipped one is no longer pending, just not done
    by the model."""
    leaves = _load_leaves(engine, session_id)
    phases_present = {leaf.phase for leaf in leaves}
    genuine = [leaf for phase in phases_present for leaf in _iter_leaves_in_document_order(leaves, phase)]
    resolved = sum(1 for leaf in genuine if leaf.status in (LeafStatus.DONE, LeafStatus.SKIPPED))
    return resolved, len(genuine)


def phase_progress_db(engine, session_id: str, phase: str) -> tuple[int, int]:
    """(resolved, total) leaves within one phase, same rule as
    plan_progress_db."""
    leaves = _load_leaves(engine, session_id)
    genuine = _iter_leaves_in_document_order(leaves, Phase(phase))
    resolved = sum(1 for leaf in genuine if leaf.status in (LeafStatus.DONE, LeafStatus.SKIPPED))
    return resolved, len(genuine)


def built_files(engine, session_id: str) -> list[str]:
    """The files of every done leaf, in plan order, each once."""
    leaves = _load_leaves(engine, session_id)
    files: dict[str, None] = {}
    for phase in dict.fromkeys(leaf.phase for leaf in leaves):
        for leaf in _iter_leaves_in_document_order(leaves, phase):
            if leaf.status == LeafStatus.DONE:
                files.update(dict.fromkeys(leaf.files or []))
    return list(files)


def get_plan(engine, session_id: str) -> str:
    return _render_plan(engine, session_id)


def get_leaf(engine, session_id: str, leaf_id: int) -> str:
    """One leaf's own full detail -- description, phase, status, parent,
    children, timing -- always complete, never truncated (there is no
    file anywhere for a "fuller" version; this and get_plan() ARE the
    complete text). The single-node counterpart to get_plan()'s whole-tree
    view, for focusing on one leaf/node without re-reading everything."""
    leaves = _load_leaves(engine, session_id)
    by_id, siblings_by_parent = build_indexes(leaves)
    leaf = by_id.get(leaf_id)
    if leaf is None:
        return f"Error: no leaf with id={leaf_id}."

    number = display_number(leaf, by_id, siblings_by_parent)
    children = siblings_by_parent.get((leaf.id, leaf.phase), [])

    lines = [f"[id={leaf.id}] {number} {leaf.description}", f"phase: {leaf.phase.value}"]
    if children:
        lines.append(f"status: (parent -- {len(children)} children, no status/timing of its own)")
    else:
        mark = {"done": "[x] done", "skipped": "[o] skipped"}.get(leaf.status.value, "[ ] todo")
        lines.append(f"status: {mark}")

    if leaf.parent_id is None:
        lines.append("parent: none (top-level)")
    else:
        parent = by_id.get(leaf.parent_id)
        if parent is None:
            lines.append(f"parent: id={leaf.parent_id} (not found -- data integrity issue)")
        else:
            parent_number = display_number(parent, by_id, siblings_by_parent)
            lines.append(f"parent: [id={parent.id}] {parent_number} {parent.description}")

    if children:
        child_summaries = ", ".join(
            f"[id={child.id}] {display_number(child, by_id, siblings_by_parent)}" for child in children
        )
        lines.append(f"children: {child_summaries}")
    else:
        lines.append("children: none (genuine leaf)")

    lines.append(f"started_at: {leaf.started_at or '-'}")
    lines.append(f"ended_at: {leaf.ended_at or '-'}")
    lines.append(f"tokens: {leaf.tokens if leaf.tokens is not None else '-'}")

    return "\n".join(lines)


_LAYA_LETTERS = {"A": "GOOD", "B": "BREAKDOWN", "C": "REDO"}


def plan_judge_rows(engine, session_id: str) -> list[dict]:
    """One row per plan node, in plan order, with its latest verdict: what
    the dashboard's Task | Judge table shows. The two scores side by side --
    `Judge` (the rule) and `Laya` (answer and confidence) -- then `LLM` (the
    tie-break's pick, when they disagreed and Laya was confident), `Final`
    (the node's status) and `Decided by` (agree / rule / llm)."""
    from JFI.models import PlannerVerdict

    leaves = _load_leaves(engine, session_id)
    by_id, siblings_by_parent = build_indexes(leaves)
    with get_session(engine) as db:
        verdicts = {v.node_id: v for v in db.exec(
            select(PlannerVerdict).where(PlannerVerdict.session_id == session_id).order_by(PlannerVerdict.id))}

    rows: list[dict] = []

    def walk(parent_id, phase, depth: int) -> None:
        for leaf in siblings_by_parent.get((parent_id, phase), []):
            v = verdicts.get(leaf.id)
            laya = ""
            if v is not None and v.laya_model:
                answer = _LAYA_LETTERS.get(v.laya_verdict, v.laya_verdict) or "?"
                laya = f"{answer} ({v.answer_confidence:.2f})" if v.answer_confidence is not None else answer
            decided = ""
            if v is not None:
                # Rows from before the two-score judge have no decided_by.
                decided = v.decided_by or ("laya" if v.fallback == "none" and v.laya_model else "rule")
            rows.append({
                "#": display_number(leaf, by_id, siblings_by_parent),
                "Level": leaf.level or "",
                "Task": "· " * depth + leaf.description,
                "Judge": (v.rule_verdict or "") if v else "",
                "Laya": laya,
                "LLM": (v.tiebreak_verdict or "") if v else "",
                "Final": display_status(leaf.plan_status or (v.final_status if v else ""),
                                        bool(siblings_by_parent.get((leaf.id, phase)))),
                "Decided by": decided,
                "Review": leaf.review_status or "",
                "Status": leaf.status.value if not siblings_by_parent.get((leaf.id, phase)) else "",
            })
            walk(leaf.id, phase, depth + 1)

    for phase in dict.fromkeys(leaf.phase for leaf in leaves if leaf.parent_id is None):
        walk(None, phase, 0)
    return rows


def plan_runbook_and_design(engine, session_id: str) -> tuple[list[dict], list[dict]]:
    """The Architect's runbook (how to set up, run and test the project;
    `Verified` = the command has actually run) and design (stack, contracts,
    conventions, outline), as table rows for the dashboards."""
    from JFI.models import DesignEntry, RunbookEntry

    with get_session(engine) as db:
        runbook = [{"Name": r.name, "Command": r.command, "Verified": "✓" if r.verified else ""}
                   for r in db.exec(select(RunbookEntry).where(RunbookEntry.session_id == session_id)
                                    .order_by(RunbookEntry.name))]
        design = [{"Kind": d.kind, "Key": d.key, "Text": d.text}
                  for d in db.exec(select(DesignEntry).where(DesignEntry.session_id == session_id)
                                   .order_by(DesignEntry.kind, DesignEntry.key))]
    return runbook, design


def plan_status_fields(engine, session_id: str) -> dict:
    """The plan fields of AbstractManager.set_status -- overall and imp
    progress, the plan markdown, and `plan_detail` (the Task | Judge rows,
    runbook and design, which the fleet dashboard can't read from the DB
    itself) -- refreshed by the planner, Dev and the reviewer as they change
    the plan."""
    runbook, design = plan_runbook_and_design(engine, session_id)
    return {"plan": plan_progress_db(engine, session_id), "phase_plan": phase_progress_db(engine, session_id, "imp"),
            "plan_markdown": render_plan_markdown(engine, session_id),
            "plan_detail": {"rows": plan_judge_rows(engine, session_id), "runbook": runbook, "design": design}}


def make_plan_db_tools(engine, session_id: str) -> Dict[str, Callable]:
    """{"get_plan": ..., "get_leaf": ...} bound to one session's DB engine --
    what runner.py wires into TOOL_MAP, the same way execute_command and
    context_save are rebound per session in _run_session."""
    return {
        "get_plan": lambda: get_plan(engine, session_id),
        "get_leaf": lambda leaf_id: get_leaf(engine, session_id, leaf_id),
    }
