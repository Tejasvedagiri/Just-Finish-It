"""Dev's work queue (laya_plan.md §6, G6).

The queue is every real leaf (no children) the planner settled as GOOD, in
tree order, and a leaf waits for its blockers:

- its own `depends_on` (Task orders leaves inside one file);
- the `depends_on` of every ancestor (Lead orders files, Architect orders
  components): a leaf waits for every leaf under any node its ancestors
  depend on.

Task sees only one file, so without the ancestor rule Dev could implement
`list_todos()` in api/ before `get_session()` in db/, and the unit test for
`list_todos` would hit the other stub's NotImplementedError.
"""

from typing import Dict, Iterable, List, Optional, Sequence, Set

from JFI.models import Leaf
from JFI.models.enums import LeafStatus
from JFI.planner.nodes import GOOD, children_of

FINISHED = (LeafStatus.DONE, LeafStatus.SKIPPED)


def tree_order(nodes: Sequence[Leaf]) -> List[Leaf]:
    out: List[Leaf] = []
    stack = list(reversed(children_of(nodes, None)))
    while stack:
        node = stack.pop()
        out.append(node)
        stack.extend(reversed(children_of(nodes, node.id)))
    return out


def dev_leaves(nodes: Sequence[Leaf]) -> List[Leaf]:
    """Every leaf Dev owns, finished or not, in tree order."""
    parents = {n.parent_id for n in nodes}
    return [n for n in tree_order(nodes) if n.id not in parents and not n.paused and n.plan_status == GOOD]


def _leaves_under(nodes: Sequence[Leaf], node_id: int) -> Iterable[int]:
    parents = {n.parent_id for n in nodes}
    frontier = [node_id]
    while frontier:
        current = frontier.pop()
        if current not in parents:
            yield current
        frontier.extend(c.id for c in children_of(nodes, current))


def blockers(nodes: Sequence[Leaf], leaf: Leaf) -> Set[int]:
    by_id = {n.id: n for n in nodes}
    wanted: Set[int] = set(leaf.depends_on or [])
    parent = by_id.get(leaf.parent_id) if leaf.parent_id is not None else None
    while parent is not None:
        wanted |= set(parent.depends_on or [])
        parent = by_id.get(parent.parent_id) if parent.parent_id is not None else None
    out: Set[int] = set()
    for node_id in wanted:
        if node_id in by_id:
            out.update(_leaves_under(nodes, node_id))
    out.discard(leaf.id)
    return out


def next_leaf(nodes: Sequence[Leaf], deferred: Dict[int, int]) -> Optional[Leaf]:
    """The first unfinished leaf whose blockers are all finished. `deferred`
    maps a leaf to the leaf it must wait for (a missing dependency found at
    test time). A dependency cycle across levels would otherwise stall the
    queue, so the first unfinished leaf is returned then."""
    todo = [n for n in dev_leaves(nodes) if n.status not in FINISHED]
    if not todo:
        return None
    todo_ids = {n.id for n in todo}
    for leaf in todo:
        if deferred.get(leaf.id) in todo_ids:
            continue
        if not blockers(nodes, leaf) & todo_ids:
            return leaf
    return todo[0]
