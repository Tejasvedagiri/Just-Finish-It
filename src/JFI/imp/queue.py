"""Dev's work queue (laya_plan.md §6, G6).

The queue is every node the planner settled as GOOD -- leaves AND the
parents above them -- children first: 1.1.1, 1.1.2, 1.1.3, then 1.1, then 1.
A parent's turn is Dev checking the part as a whole (its done_when, its
ground-truth cases) once everything under it is done. A node waits for its
blockers:

- everything under it (a parent comes after its children);
- its own `depends_on` (Task orders leaves inside one file);
- the `depends_on` of every ancestor (Lead orders files, Architect orders
  components): a node waits for every node under any node its ancestors
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


def post_order(nodes: Sequence[Leaf]) -> List[Leaf]:
    """Children before their parent: 1.1.1, 1.1.2, 1.1, 1.2.1, 1.2, 1."""
    out: List[Leaf] = []
    stack = [(node, False) for node in reversed(children_of(nodes, None))]
    while stack:
        node, expanded = stack.pop()
        if expanded:
            out.append(node)
            continue
        stack.append((node, True))
        stack.extend((child, False) for child in reversed(children_of(nodes, node.id)))
    return out


def dev_nodes(nodes: Sequence[Leaf]) -> List[Leaf]:
    """Every node Dev finishes, finished or not, children first."""
    return [n for n in post_order(nodes) if not n.paused and n.plan_status == GOOD]


def _nodes_under(nodes: Sequence[Leaf], node_id: int) -> Iterable[int]:
    """The node and everything below it."""
    frontier = [node_id]
    while frontier:
        current = frontier.pop()
        yield current
        frontier.extend(c.id for c in children_of(nodes, current))


def _ancestors(by_id: Dict[int, Leaf], node: Leaf) -> List[Leaf]:
    out = []
    parent = by_id.get(node.parent_id) if node.parent_id is not None else None
    while parent is not None:
        out.append(parent)
        parent = by_id.get(parent.parent_id) if parent.parent_id is not None else None
    return out


def blockers(nodes: Sequence[Leaf], node: Leaf) -> Set[int]:
    by_id = {n.id: n for n in nodes}
    ancestors = _ancestors(by_id, node)
    wanted: Set[int] = set(node.depends_on or [])
    for parent in ancestors:
        wanted |= set(parent.depends_on or [])
    out: Set[int] = set(_nodes_under(nodes, node.id))
    for node_id in wanted:
        if node_id in by_id:
            out.update(_nodes_under(nodes, node_id))
    # A node that depends on its own ancestor would otherwise wait for that
    # ancestor, which waits for it.
    out -= {a.id for a in ancestors}
    out.discard(node.id)
    return out


def next_leaf(nodes: Sequence[Leaf], deferred: Dict[int, int]) -> Optional[Leaf]:
    """The first unfinished node whose blockers are all finished. `deferred`
    maps a node to the node it must wait for (a missing dependency found at
    test time). A dependency cycle across levels would otherwise stall the
    queue, so the first unfinished node is returned then."""
    todo = [n for n in dev_nodes(nodes) if n.status not in FINISHED]
    if not todo:
        return None
    todo_ids = {n.id for n in todo}
    for leaf in todo:
        if deferred.get(leaf.id) in todo_ids:
            continue
        if not blockers(nodes, leaf) & todo_ids:
            return leaf
    return todo[0]
