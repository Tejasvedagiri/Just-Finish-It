"""The plan-tree tool surface backed by JFI.models.Leaf, replacing the
free-form write_file/append_to_file/replace_in_file text surgery plan.md
required -- see /todo.md's "SQLite/Pydantic persistence rewrite" section
for why (the renumbering bug that needed JFI.tool.plan_renumber as a
bolt-on repair pass, and frontend/src/main.js's own independently-hand-
rolled regex parser hitting its own bug).

Nine tools instead of raw file edits: get_plan (read the whole tree),
get_leaf (one leaf's own full, never-truncated detail -- the single-node
counterpart to get_plan), add_leaf (create a leaf/parent), start_leaf/mark_leaf_done (timing + status on a
real leaf only -- see Leaf's own docstring on why a parent never carries
these), split_leaf (turn an existing leaf into a parent with new children,
the exact operation plan_renumber.py existed to make safe under the old
dot-string scheme -- here it just needs one INSERT per new child and zero
renumbering, since sort_key is already gap-numbered), reorder_leaf (move a
leaf among its own siblings -- the other half of what plan_renumber.py
used to do by hand-deriving every descendant's number), merge_leaf (the
undo for a single-child split_leaf: folds an only child back into its
parent, collapsing the pointless nesting split_leaf can't itself avoid
creating when a leaf turns out to have just one real sub-piece after all),
and delete_leaf (removes a genuinely wrong/duplicate leaf outright --
added after a real session hit exactly this gap: no delete/merge tool
existed, so it fell back to direct SQL against the live WAL-mode DB to fix
a mis-split rather than a proper, guarded tool call).

What runner.py wires into TOOL_MAP per session, the same way
make_context_tools/make_load_tool are -- see make_plan_db_tools at the
bottom.
"""

from typing import Callable, Dict

from sqlmodel import select

from JFI.models import Leaf, LeafStatus, Phase, SessionRecord, build_indexes, display_number, get_session
from JFI.models._util import utcnow

# Duplicated from simple_session_manager.PHASE_SECTION rather than imported
# from it -- that module already imports FROM this one (make_plan_db_tools),
# so importing back would be circular. Only these two phases ever had a
# checklist under the old plan.md format; keep in sync if that ever changes.
_MARKDOWN_PHASE_SECTIONS = {"imp": "Implementation", "testing": "Testing"}

# ---------------------------------------------------------------------------
# Depth guardrails -- a structural backstop for the runaway-recursive-
# splitting failure observed in a real run (StockUI's portfolio-api-wiring
# session): a single "investigate the SectorPie bug" leaf was atomized by
# the Journeyman/Function-Breakdown planner passes into 30+ leaves nested 7+
# levels deep (e.g. "1.1.2.2.2.1.1.1"), including near-duplicate leaves like
# "record the finding" and "close the leaf" as two separate checkboxes for
# what was really one action. Prose guidance alone (see the Journeyman
# prompt's own "work ONE item at a time" rule) didn't stop it, because
# open-ended diagnostic work has no natural one-tool-call bottom -- there's
# always one more question to imagine asking. These two constants are
# enforced mechanically in add_leaf/split_leaf below (see _check_depth),
# independent of whether the model reads or correctly applies the prompt's
# own guidance on when to stop.
#
# MAX_LEAF_DEPTH is the general cap: real implementation work bottoms out
# in a handful of levels (the Journeyman prompt's own docstring already
# says "a parent commonly bottoms out 3-4 levels down"), so 5 gives some
# headroom over that stated norm without permitting runaway recursion.
MAX_LEAF_DEPTH = 5

# MAX_INVESTIGATION_DEPTH is a STRICTER cap for leaves that are themselves
# open-ended diagnostic/exploratory work, detected by keyword match on the
# leaf's own description or any ancestor's (a child of an investigation
# leaf is investigation work too, even if its own wording doesn't repeat
# the keyword) -- see _is_investigation_branch. 2 permits splitting "1.
# Investigate bug X" into a couple of named angles ("1.1 check the fetch
# layer", "1.2 check the render layer") but not recursively re-splitting
# each of those further -- the actual step-by-step diagnostic work happens
# INSIDE whichever leaf is reached, via ordinary tool calls and
# context_save, never via more add_leaf/split_leaf calls.
MAX_INVESTIGATION_DEPTH = 2

INVESTIGATION_KEYWORDS = (
    "investigate", "diagnose", "audit", "figure out", "root cause",
    "reproduce", "determine why", "debug", "explore",
)


def _depth(leaf_id: int, leaves_by_id: dict) -> int:
    """1 for a root leaf (no parent) -- matches the number of segments in
    its own display_number (e.g. "1.1.2" is depth 3) -- incrementing once
    per ancestor hop up to the root. `seen` guards against a cyclic
    parent_id chain (should never happen, but a depth check must never
    infinite-loop even if one somehow did)."""
    depth = 1
    current = leaves_by_id.get(leaf_id)
    seen = set()
    while current is not None and current.parent_id is not None and current.parent_id not in seen:
        seen.add(current.parent_id)
        depth += 1
        current = leaves_by_id.get(current.parent_id)
    return depth


def _is_investigation_branch(leaf: Leaf, leaves_by_id: dict) -> bool:
    """True if `leaf` or any of its ancestors' description text matches an
    investigation/diagnostic keyword -- see INVESTIGATION_KEYWORDS."""
    current = leaf
    seen = set()
    while current is not None:
        text = current.description.lower()
        if any(keyword in text for keyword in INVESTIGATION_KEYWORDS):
            return True
        if current.parent_id is None or current.parent_id in seen:
            return False
        seen.add(current.parent_id)
        current = leaves_by_id.get(current.parent_id)
    return False


def _check_depth(parent: Leaf, leaves_by_id: dict) -> str | None:
    """None if it's fine to add a new leaf/children under `parent`; an
    Error string (ready to return straight to the model) if that would
    exceed the applicable depth cap. Shared by add_leaf (parent = the
    named parent_id) and split_leaf (parent = the leaf being split, since
    its new children land one level under it)."""
    new_depth = _depth(parent.id, leaves_by_id) + 1
    if _is_investigation_branch(parent, leaves_by_id):
        cap, kind = MAX_INVESTIGATION_DEPTH, "an investigation/diagnostic"
    else:
        cap, kind = MAX_LEAF_DEPTH, "a"
    if new_depth <= cap:
        return None
    return (
        f"Error: this would put the new leaf at depth {new_depth} under {kind} branch "
        f"(max {cap} for {'investigation/diagnostic leaves' if kind != 'a' else 'any branch'}). "
        f"Stop splitting -- accept leaf {parent.id} as already atomic enough and execute it "
        f"directly (with as many ordinary tool calls as it actually needs), rather than adding "
        f"another layer of plan structure for it. If it's genuinely diagnostic/investigative work, "
        f"record findings with context_save as you go instead of a new leaf per step."
    )


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


def render_plan_markdown(engine, session_id: str) -> str:
    """Renders the DB-backed plan tree as markdown matching plan.md's OLD
    bullet syntax byte-for-byte (no [id=N] tags, which are only meaningful
    to the model calling the tools, never to a human reading a dashboard
    or the existing plan.md-format parsers) -- so two things that already
    consume that exact syntax keep working completely unchanged:
    frontend/src/main.js's parsePlanLines/buildPlanTree (the fleet
    dashboard's checklist tab), and a human just wanting something
    readable (the Streamlit dashboard's Plan section).

    Only "imp"/"testing" leaves render -- the only two phases that ever
    had a checklist under the old format (see PHASE_SECTION in
    simple_session_manager.py, duplicated above as
    _MARKDOWN_PHASE_SECTIONS to avoid a circular import).
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
    """Genuine leaves (no children of their own) within one phase, in the
    same top-to-bottom order a markdown plan.md file's lines would have
    given -- a depth-first walk, each level sorted by sort_key. What
    _pending_items got for free from plain file order; the DB has to
    reconstruct it since rows carry no inherent document position."""
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


def has_leaves(engine, session_id: str) -> bool:
    """Cheap existence check used by SimpleSessionManager to decide
    whether a session's plan lives in the DB (use the *_db functions
    below) or still only in plan.md (fall back to the original regex
    methods) -- see those methods' own docstrings for why both paths
    coexist during the migration instead of a flag-day cutover."""
    with get_session(engine) as db:
        return db.exec(select(Leaf).where(Leaf.session_id == session_id)).first() is not None


def plan_progress_db(engine, session_id: str) -> tuple[int, int]:
    """(resolved, total) across every phase -- DB-backed equivalent of
    SimpleSessionManager.plan_progress(). A DONE or SKIPPED leaf counts as
    resolved, same rule as the old "- [x]"/"- [○]" markdown markers."""
    leaves = _load_leaves(engine, session_id)
    phases_present = {leaf.phase for leaf in leaves}
    genuine = [leaf for phase in phases_present for leaf in _iter_leaves_in_document_order(leaves, phase)]
    resolved = sum(1 for leaf in genuine if leaf.status in (LeafStatus.DONE, LeafStatus.SKIPPED))
    return resolved, len(genuine)


def phase_progress_db(engine, session_id: str, phase: str) -> tuple[int, int]:
    """(resolved, total) within one phase -- DB-backed equivalent of
    SimpleSessionManager.phase_progress(phase)."""
    leaves = _load_leaves(engine, session_id)
    genuine = _iter_leaves_in_document_order(leaves, Phase(phase))
    resolved = sum(1 for leaf in genuine if leaf.status in (LeafStatus.DONE, LeafStatus.SKIPPED))
    return resolved, len(genuine)


def pending_leaves_db(engine, session_id: str, phase: str) -> list[Leaf]:
    """Genuine, still-TODO leaves within one phase, in document order --
    DB-backed equivalent of SimpleSessionManager._pending_items(section)."""
    leaves = _load_leaves(engine, session_id)
    ordered = _iter_leaves_in_document_order(leaves, Phase(phase))
    return [leaf for leaf in ordered if leaf.status == LeafStatus.TODO]


def render_pending_lines(engine, session_id: str, phase: str) -> list[str]:
    """Pending leaves in `phase`, one prompt-ready line each (e.g.
    "[id=7] 1.2 Add evaluate()") -- what get_system_message's work-queue
    section shows the model when this session's plan lives in the DB,
    replacing the raw "- [ ] ..." plan.md lines _pending_items extracted
    when it lived in the file."""
    leaves = _load_leaves(engine, session_id)
    by_id, siblings_by_parent = build_indexes(leaves)
    pending = pending_leaves_db(engine, session_id, phase)
    lines = []
    for leaf in pending:
        number = display_number(leaf, by_id, siblings_by_parent)
        lines.append(f"[id={leaf.id}] {number} {leaf.description}")
    return lines


def current_task_title_db(engine, session_id: str, phase: str, max_len: int = 140):
    """DB-backed equivalent of SimpleSessionManager.current_task_title(phase)."""
    pending = pending_leaves_db(engine, session_id, phase)
    if not pending:
        return None
    title = pending[0].description
    if len(title) > max_len:
        title = title[: max_len - 1].rstrip() + "…"
    return title


def skip_current_task_db(engine, session_id: str, phase: str):
    """DB-backed equivalent of SimpleSessionManager.skip_current_task(phase)
    (Ctrl+K) -- marks the first pending leaf SKIPPED, returns its
    description, or None if nothing is pending."""
    pending = pending_leaves_db(engine, session_id, phase)
    if not pending:
        return None
    leaf = pending[0]
    with get_session(engine) as db:
        row = db.get(Leaf, leaf.id)
        row.status = LeafStatus.SKIPPED
        db.add(row)
        db.commit()
    return leaf.description


def skip_remaining_tasks_db(engine, session_id: str, phase: str) -> int:
    """DB-backed equivalent of SimpleSessionManager.skip_remaining_tasks(phase)
    (Ctrl+Q) -- marks every pending leaf in `phase` SKIPPED, returns how many."""
    pending = pending_leaves_db(engine, session_id, phase)
    if not pending:
        return 0
    with get_session(engine) as db:
        for leaf in pending:
            row = db.get(Leaf, leaf.id)
            row.status = LeafStatus.SKIPPED
            db.add(row)
        db.commit()
    return len(pending)


def reorder_leaf(engine, session_id: str, leaf_id: int, after_leaf_id: int = 0) -> str:
    """Moves `leaf_id` to sit right after `after_leaf_id` among its OWN
    current siblings (same parent + phase) -- 0/omitted moves it to the
    very front instead. The DB-backed replacement for the old
    plan_renumber.py workflow: fixing a leaf-ordering bug used to mean
    re-deriving every descendant number of a parent by hand via a
    standalone script; here it's one call, and rebalances every sibling's
    sort_key to a fresh 10/20/30/... sequence in the new order so a later
    insert always has room again."""
    with get_session(engine) as db:
        leaf = db.get(Leaf, leaf_id)
        if leaf is None:
            return f"Error: no leaf with id={leaf_id}."
        siblings = sorted(
            (
                s for s in _load_leaves(engine, session_id)
                if s.parent_id == leaf.parent_id and s.phase == leaf.phase and s.id != leaf_id
            ),
            key=lambda s: s.sort_key,
        )
        if after_leaf_id:
            if not any(s.id == after_leaf_id for s in siblings):
                return (
                    f"Error: leaf {after_leaf_id} is not a sibling of leaf {leaf_id} "
                    "(same parent and phase required)."
                )
            new_order = []
            for sibling in siblings:
                new_order.append(sibling)
                if sibling.id == after_leaf_id:
                    new_order.append(leaf)
        else:
            new_order = [leaf] + siblings

        for index, item in enumerate(new_order):
            row = db.get(Leaf, item.id)
            row.sort_key = (index + 1) * 10
            db.add(row)
        db.commit()

    return f"Reordered leaf id={leaf_id} to sit right after {after_leaf_id or 'the start'}."


def _next_sort_key(engine, session_id: str, parent_id: int | None, phase: str) -> int:
    """Gap-numbered (10, 20, 30, ...) so a future split_leaf can insert
    between two existing siblings without renumbering anything -- see
    Leaf's own docstring."""
    siblings = [
        leaf for leaf in _load_leaves(engine, session_id)
        if leaf.parent_id == parent_id and leaf.phase.value == phase
    ]
    return (max((leaf.sort_key for leaf in siblings), default=0)) + 10


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


def add_leaf(engine, session_id: str, phase: str, description: str, parent_id: int = 0) -> str:
    try:
        phase_enum = Phase(phase)
    except ValueError:
        return f"Error: unknown phase {phase!r} -- expected one of {[p.value for p in Phase]}."

    real_parent_id = parent_id or None
    with get_session(engine) as db:
        if real_parent_id is not None:
            parent = db.get(Leaf, real_parent_id)
            if parent is None:
                return f"Error: no leaf with id={real_parent_id} exists to parent this under."
            if parent.status != LeafStatus.TODO or parent.started_at is not None:
                return (
                    f"Error: leaf {real_parent_id} already looks like a real leaf (has "
                    "status/timing of its own), not a parent -- split_leaf it first if it "
                    "needs children."
                )
            depth_error = _check_depth(parent, {leaf.id: leaf for leaf in _load_leaves(engine, session_id)})
            if depth_error:
                return depth_error

        sort_key = _next_sort_key(engine, session_id, real_parent_id, phase)
        leaf = Leaf(
            session_id=session_id, parent_id=real_parent_id, phase=phase_enum,
            sort_key=sort_key, description=description,
        )
        db.add(leaf)
        db.commit()
        db.refresh(leaf)
        new_id = leaf.id

    number = display_number(leaf, *build_indexes(_load_leaves(engine, session_id)))
    return f"Added leaf id={new_id} (number {number}) under parent_id={real_parent_id or 'none (root)'}."


def start_leaf(engine, session_id: str, leaf_id: int) -> str:
    with get_session(engine) as db:
        leaf = db.get(Leaf, leaf_id)
        if leaf is None:
            return f"Error: no leaf with id={leaf_id}."
        leaf.started_at = utcnow()
        db.add(leaf)

        record = db.get(SessionRecord, session_id)
        if record is not None:
            record.current_task = leaf.description
            record.current_task_started_at = leaf.started_at
            db.add(record)
        db.commit()
    return f"Started leaf id={leaf_id}."


def mark_leaf_done(engine, session_id: str, leaf_id: int, tokens: int = 0) -> str:
    with get_session(engine) as db:
        leaf = db.get(Leaf, leaf_id)
        if leaf is None:
            return f"Error: no leaf with id={leaf_id}."
        has_children = db.exec(select(Leaf).where(Leaf.parent_id == leaf_id)).first() is not None
        if has_children:
            return f"Error: leaf {leaf_id} has children -- only real leaves get marked done, never a parent."
        leaf.status = LeafStatus.DONE
        leaf.ended_at = utcnow()
        if leaf.started_at is None:
            leaf.started_at = leaf.ended_at
        if tokens:
            leaf.tokens = tokens
        db.add(leaf)

        record = db.get(SessionRecord, session_id)
        if record is not None and record.current_task == leaf.description:
            record.current_task = None
            record.current_task_started_at = None
            db.add(record)
        db.commit()
    return f"Marked leaf id={leaf_id} done."


def split_leaf(engine, session_id: str, leaf_id: int, into: list[str]) -> str:
    if len(into) < 2:
        return "Error: split_leaf needs at least 2 new child descriptions -- a single child is pointless nesting."

    with get_session(engine) as db:
        leaf = db.get(Leaf, leaf_id)
        if leaf is None:
            return f"Error: no leaf with id={leaf_id}."

        depth_error = _check_depth(leaf, {other.id: other for other in _load_leaves(engine, session_id)})
        if depth_error:
            return depth_error

        # Turning this row into a parent -- it stops carrying real
        # status/timing itself, exactly like a plan.md parent bullet never
        # had a checkbox (see Leaf's own docstring).
        leaf.status = LeafStatus.TODO
        leaf.started_at = None
        leaf.ended_at = None
        leaf.tokens = None
        db.add(leaf)
        db.commit()

        new_ids = []
        for index, description in enumerate(into):
            child = Leaf(
                session_id=session_id, parent_id=leaf_id, phase=leaf.phase,
                sort_key=(index + 1) * 10, description=description,
            )
            db.add(child)
            db.commit()
            db.refresh(child)
            new_ids.append(child.id)

    return f"Split leaf id={leaf_id} into {len(into)} children: {new_ids}."


def merge_leaf(engine, session_id: str, leaf_id: int) -> str:
    """The undo for split_leaf when it (or a Journeyman/Function-Breakdown
    pass) leaves a parent with exactly one child -- pointless nesting per
    CORE_PLAN_RULES' own "at least 2 children" rule, but with no way to fix
    it before this existed (observed live: a session correctly identified
    the single-child parent, correctly ruled out fabricating a fake second
    child as busywork, and was left stuck rationalizing around a rule it
    had no tool to satisfy). Folds `leaf_id` (the only child) up into its
    parent: the parent absorbs the child's description and becomes a real,
    actionable leaf itself; the child row is removed. One level per call,
    same as split_leaf is one level -- call again on the result if merging
    cascades further up."""
    with get_session(engine) as db:
        leaf = db.get(Leaf, leaf_id)
        if leaf is None:
            return f"Error: no leaf with id={leaf_id}."
        if leaf.parent_id is None:
            return f"Error: leaf {leaf_id} is already top-level -- nothing to merge it into."

        parent = db.get(Leaf, leaf.parent_id)
        if parent is None:
            return f"Error: leaf {leaf_id}'s parent_id={leaf.parent_id} does not exist (data integrity issue)."

        siblings = list(db.exec(select(Leaf).where(Leaf.parent_id == leaf.parent_id)))
        if len(siblings) != 1:
            return (
                f"Error: parent {parent.id} has {len(siblings)} children, not 1 -- merge_leaf only "
                f"folds a SINGLE child into its parent (merging one of several would silently orphan "
                f"the rest). Reduce it to exactly this leaf first, e.g. by merging/deleting the others."
            )

        has_grandchildren = db.exec(select(Leaf).where(Leaf.parent_id == leaf_id)).first() is not None
        if has_grandchildren:
            return (
                f"Error: leaf {leaf_id} itself has children -- merge_leaf only folds a genuine leaf "
                "(no children of its own) into its parent, never a whole subtree. Resolve/merge its "
                "own children first."
            )

        # Captured as plain values before commit -- ORM attributes on `leaf`/
        # `parent` become unreadable once this `with` block exits and
        # expires them (see mark_leaf_done/split_leaf's own returns, which
        # avoid the same trap by only ever using ids/plain values below).
        parent_id = parent.id
        child_description = leaf.description

        parent.description = child_description
        db.add(parent)
        db.delete(leaf)
        # No SessionRecord.current_task update needed: the parent now
        # carries the exact same description text the leaf had, so a
        # current_task pointing at the leaf's description already matches
        # the (now real) parent leaf -- nothing actually changed to fix up.
        db.commit()

    return f"Merged leaf id={leaf_id} into parent id={parent_id}, which is now a real leaf: {child_description!r}."


def delete_leaf(engine, session_id: str, leaf_id: int) -> str:
    """Removes a genuinely wrong or duplicate leaf outright -- e.g. two
    byte-identical leaves created by mistake, or one that turned out to
    describe work that doesn't need doing. Refuses on a parent (has
    children -- delete/merge those first so nothing gets silently
    orphaned) or on a leaf already marked DONE (that's a real completed-
    work record, not a mistake to erase; if it genuinely needs undoing
    that's a call for a human, not this tool)."""
    with get_session(engine) as db:
        leaf = db.get(Leaf, leaf_id)
        if leaf is None:
            return f"Error: no leaf with id={leaf_id}."
        has_children = db.exec(select(Leaf).where(Leaf.parent_id == leaf_id)).first() is not None
        if has_children:
            return f"Error: leaf {leaf_id} has children -- delete/merge those first, never delete a parent outright."
        if leaf.status == LeafStatus.DONE:
            return f"Error: leaf {leaf_id} is already marked done -- delete_leaf refuses to erase completed work."

        description = leaf.description
        db.delete(leaf)

        record = db.get(SessionRecord, session_id)
        if record is not None and record.current_task == description:
            record.current_task = None
            record.current_task_started_at = None
            db.add(record)
        db.commit()

    return f"Deleted leaf id={leaf_id} ({description!r})."


def make_plan_db_tools(engine, session_id: str) -> Dict[str, Callable]:
    """{"get_plan": ..., "get_leaf": ..., "add_leaf": ..., "start_leaf": ...,
    "mark_leaf_done": ..., "split_leaf": ..., "reorder_leaf": ...,
    "merge_leaf": ..., "delete_leaf": ...} bound to one session's DB engine
    -- what runner.py wires into TOOL_MAP, the same way execute_command/
    context_save are rebound per session in _run_session."""
    return {
        "get_plan": lambda: get_plan(engine, session_id),
        "get_leaf": lambda leaf_id: get_leaf(engine, session_id, leaf_id),
        "add_leaf": lambda phase, description, parent_id=0: add_leaf(engine, session_id, phase, description, parent_id),
        "start_leaf": lambda leaf_id: start_leaf(engine, session_id, leaf_id),
        "mark_leaf_done": lambda leaf_id, tokens=0: mark_leaf_done(engine, session_id, leaf_id, tokens),
        "split_leaf": lambda leaf_id, into: split_leaf(engine, session_id, leaf_id, into),
        "reorder_leaf": lambda leaf_id, after_leaf_id=0: reorder_leaf(engine, session_id, leaf_id, after_leaf_id),
        "merge_leaf": lambda leaf_id: merge_leaf(engine, session_id, leaf_id),
        "delete_leaf": lambda leaf_id: delete_leaf(engine, session_id, leaf_id),
    }
