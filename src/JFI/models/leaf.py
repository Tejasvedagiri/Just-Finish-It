"""The plan tree, as DB rows instead of plan.md's markdown checkbox tree.

Replaces two independently-fragile things at once: (1) dot-numbered strings
("1.1.2") hand-edited via replace_in_file, whose renumbering after a split
needed a bolt-on repair tool (JFI.tool.plan_renumber) because a stale read
between two edits could desync; (2) frontend/src/main.js's own separate
regex tree-parser (parsePlanLines/buildPlanTree) over that same markdown,
which had its own documented bug (a trailing-period parent number once
flattened the whole tree into bogus top-level roots). One schema, no
regex, no hand-maintained numbering.

A "leaf" and a "parent" are the same table row shape -- exactly like
plan.md, where the distinction is structural (does anything else point at
this row as its parent?) rather than a stored flag. Only rows with no
children are meant to carry a real `status`/timing; a caller updating a
parent-with-children row's status is a bug the same way ticking a plan.md
parent bullet's checkbox was (see PLAN_FORMAT_RULES's "A checkbox on a
parent hands the implementer a fake duplicate task alongside its own real
children").
"""

from datetime import datetime
from typing import Optional

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
    # needed plan_renumber.py's repair pass under the old dot-string scheme.
    sort_key: int = Field(default=0, index=True)

    description: str
    status: LeafStatus = Field(default=LeafStatus.TODO, index=True)

    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    # Folds in what task_history tracked as a second, separately-joined
    # list in the old status snapshot (see frontend/src/main.js's
    # historyByKey) -- one row now carries its own cost instead of two
    # parallel structures keyed by "phase:number" strings.
    tokens: Optional[int] = None

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
