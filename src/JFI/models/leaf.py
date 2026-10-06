"""The plan tree, as DB rows instead of plan.md's markdown checkbox tree.

Replaces two independently-fragile things at once: (1) dot-numbered strings
("1.1.2") hand-edited via replace_in_file, whose renumbering after a split
needed a bolt-on repair tool because a stale read between two edits could
desync; (2) Just-Finish-It-Fleet's src/main.js's own separate
regex tree-parser (parsePlanLines/buildPlanTree) over that same markdown,
which had its own documented bug (a trailing-period parent number once
flattened the whole tree into bogus top-level roots). One schema, no
regex, no hand-maintained numbering.

A "leaf" and a "parent" are the same table row shape -- exactly like
plan.md, where the distinction is structural (does anything else point at
this row as its parent?) rather than a stored flag. Only rows with no
children are meant to carry a real `status`/timing; a caller updating a
parent-with-children row's status is a bug the same way ticking a plan.md
parent bullet's checkbox was: it handed the implementer a fake duplicate
task alongside the parent's own real children.
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import JSON, Column
from sqlmodel import Field, SQLModel

from JFI.models._util import utcnow
from JFI.models.enums import LeafStatus, Phase


class Leaf(SQLModel, table=True):
    __tablename__ = "leaf"

    id: Optional[int] = Field(default=None, primary_key=True)
    session_id: str = Field(index=True, foreign_key="sessionrecord.session_id")
    parent_id: Optional[int] = Field(default=None, index=True, foreign_key="leaf.id")
    phase: Phase = Field(index=True)

    # Sibling order within (session_id, parent_id, phase) -- an integer
    # gap-numbered sequence (10, 20, 30, ...) rather than 1/2/3 so a new
    # leaf can be inserted between two existing ones (sort_key = 15)
    # without renumbering every sibling after it, the exact operation that
    # needed a repair pass under the old dot-string scheme.
    sort_key: int = Field(default=0, index=True)

    description: str
    status: LeafStatus = Field(default=LeafStatus.TODO, index=True)

    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    # Folds in what task_history tracked as a second, separately-joined
    # list in the old status snapshot (see Just-Finish-It-Fleet's src/main.js's
    # historyByKey) -- one row now carries its own cost instead of two
    # parallel structures keyed by "phase:number" strings.
    tokens: Optional[int] = None

    # Program Manager's per-leaf verdict (see JFI.tool.plan_db_tools.
    # review_leaf) -- "approved"/"rejected"/None (not yet reviewed).
    # Deliberately separate from `status` above: `status` tracks
    # IMPLEMENTATION progress (TODO/DONE/SKIPPED), this tracks REVIEW
    # progress -- a leaf can be `status=TODO, review_status=approved`
    # (ready for imp) just as easily as `status=TODO, review_status=None`
    # (never reviewed) or `review_status=rejected` (sent back for rework).
    review_status: Optional[str] = Field(default=None, index=True)
    # The expected-changes note from the most recent rejection -- what
    # Program Manager wants fixed before re-review. Cleared on approval.
    review_note: Optional[str] = None
    # How many times in a row THIS leaf has been rejected with no change
    # to its description in between -- see review_leaf's own circuit-
    # breaker check. Reset to 0 on approval or on any description change.
    rejection_count: int = Field(default=0)
    # (v2 reuses review_status for the reviewer's per-leaf verdict,
    # "passed"/"failed"; the v1 values above stay readable -- laya_plan.md §13.)

    # --- v2 planner (laya_plan.md §6, §13) -- unused by v1 sessions ---------
    # Which role created the node: "architect" / "lead" / "task". Decides who
    # breaks it down (next layer) and who redoes it (the same role).
    level: Optional[str] = Field(default=None, index=True)
    # component: "component"/"project"; file: "code"/"artifact"/"section";
    # leaf: "implement"/"modify"/"delete"/"fill"/"passage"/"compare".
    kind: Optional[str] = None
    # The planning status Laya (or its fallback) sets: NULL = unjudged,
    # "GOOD" / "BREAKDOWN" / "REDO". Separate from `status` (Dev's progress).
    plan_status: Optional[str] = Field(default=None, index=True)
    redo_count: int = Field(default=0)
    # Last judge reason: "operational" / "vague" / "duplicate" / "design" / "too_big".
    redo_reason: Optional[str] = None
    escalation_count: int = Field(default=0)
    # True while an escalation has sent this node's parent back for a redo:
    # nothing under it is judged, broken down or implemented meanwhile.
    paused: bool = Field(default=False)
    # Observable finish condition; on implement/modify leaves it's the unit test case.
    done_when: Optional[str] = None
    # What the next layer needs beyond the short description: what to build and
    # how, edge cases, what not to touch.
    notes: Optional[str] = None
    # Where the context lives: design entries ("contract:main->calc"), source
    # ranges ("page.html L1376-1402"), docs or URLs.
    references: Optional[list[str]] = Field(default=None, sa_column=Column(JSON))
    # Files the node creates or changes, plus its test file.
    files: Optional[list[str]] = Field(default=None, sa_column=Column(JSON))
    # Leaf ids that must be done first (validated acyclic on write).
    depends_on: Optional[list[int]] = Field(default=None, sa_column=Column(JSON))
    # Optional tools Laya picked for this node's episodes (G1.3); only adds.
    tools: Optional[list[str]] = Field(default=None, sa_column=Column(JSON))
    # Dev episodes started on this leaf (restart cap, G13).
    attempt_count: int = Field(default=0)
    # The reviewer's failure text when a done leaf is re-opened (G8).
    fix_note: Optional[str] = None
    reopened_count: int = Field(default=0)
    # The git checkpoint taken when the leaf passed (JFI.tool.checkpoint_tools).
    checkpoint: Optional[str] = None
    # Ground-truth cases (docs/old_new.md): on a Lead file node, the cases its
    # file must match; on a compare leaf, the cases it checks. Each case has
    # its evidence in .jfi/evidence/<session_id>/<task number>_<case>.*.
    cases: Optional[list[str]] = Field(default=None, sa_column=Column(JSON))
    # A compare leaf's evidence fingerprint when it passed: if the evidence is
    # edited or re-captured afterwards, the leaf is compared again.
    evidence_hash: Optional[str] = None

    created_at: datetime = Field(default_factory=utcnow)


def build_indexes(leaves: list["Leaf"]) -> tuple[dict, dict]:
    """One pass over an already-fetched leaf list, building the two indexes
    `display_number` needs: `by_id` (id -> Leaf, for walking up to the
    root) and `siblings_by_parent` ((parent_id, phase) -> that parent's
    same-phase children, sorted by sort_key, for this leaf's own position
    among them). Keyed by phase as well as parent_id so root-level leaves
    from different phases each start their own numbering at 1 -- matching
    plan.md's own convention that "## Implementation" and "## Testing" were
    SEPARATELY-numbered trees, not one numbering shared across both. Build
    once per render (export-db, a dashboard query) and reuse across every
    `display_number` call in that render -- never per leaf."""
    by_id = {leaf.id: leaf for leaf in leaves}
    siblings_by_parent: dict = {}
    for leaf in leaves:
        siblings_by_parent.setdefault((leaf.parent_id, leaf.phase), []).append(leaf)
    for group in siblings_by_parent.values():
        group.sort(key=lambda leaf: leaf.sort_key)
    return by_id, siblings_by_parent


def display_number(leaf: "Leaf", by_id: dict, siblings_by_parent: dict) -> str:
    """Recomputes a plan.md-style dot-number ("1.1.2") from the tree shape,
    purely for display (export-db, the dashboard) -- never stored, never
    authoritative, so there is nothing here that can desync the way the
    old hand-edited numbers could."""
    path = []
    node: Optional[Leaf] = leaf
    while node is not None:
        siblings = siblings_by_parent.get((node.parent_id, node.phase), [])
        index = next((i for i, sibling in enumerate(siblings) if sibling.id == node.id), 0)
        path.append(str(index + 1))
        node = by_id.get(node.parent_id) if node.parent_id is not None else None
    return ".".join(reversed(path))
