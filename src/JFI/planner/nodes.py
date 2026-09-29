"""The v2 plan tree: Leaf rows with the v2 fields (laya_plan.md §6, §13.1),
and the model-facing tools that change it.

Rules enforced here, in code, not just in prompts:
- `level` is set from the calling role, never by the model; a role can only
  update/delete nodes it created (REDO goes back to the creator, D5).
- Lead and Task add nodes only under the node their episode is about.
- A description is capped at PLANNER_ITEM_MAX_CHARS (default 200): it is
  also the text Laya judges, inside a ~320-token state budget (§4.2).
- depends_on must name existing nodes and stay acyclic (G6).
- Depth is still capped by MAX_LEAF_DEPTH.
"""

import os
from typing import Callable, Dict, List, Optional, Sequence

from sqlmodel import select

from JFI.models import Leaf, Phase, PlanEvent, get_session
from JFI.models.enums import LeafStatus
from JFI.tool.result_cap import cap_result

GOOD, BREAKDOWN, REDO = "GOOD", "BREAKDOWN", "REDO"
DEFAULT_ITEM_MAX_CHARS = 200
DEFAULT_ESCALATION_CAP = 1
MAX_LEAF_DEPTH = 5


def item_max_chars() -> int:
    try:
        return max(40, int(os.environ.get("PLANNER_ITEM_MAX_CHARS", DEFAULT_ITEM_MAX_CHARS)))
    except ValueError:
        return DEFAULT_ITEM_MAX_CHARS


def escalation_cap() -> int:
    try:
        return max(0, int(os.environ.get("PLANNER_ESCALATION_CAP", DEFAULT_ESCALATION_CAP)))
    except ValueError:
        return DEFAULT_ESCALATION_CAP


# ------------------------------------------------------------------ queries

def load_nodes(engine, session_id: str) -> List[Leaf]:
    with get_session(engine) as db:
        return list(db.exec(select(Leaf).where(Leaf.session_id == session_id, Leaf.level.is_not(None))))


def children_of(nodes: Sequence[Leaf], node_id: Optional[int]) -> List[Leaf]:
    return sorted((n for n in nodes if n.parent_id == node_id), key=lambda n: (n.sort_key, n.id))


def depth_of(nodes_by_id: Dict[int, Leaf], node: Leaf) -> int:
    depth, current, seen = 1, node, set()
    while current.parent_id is not None and current.parent_id not in seen:
        seen.add(current.parent_id)
        current = nodes_by_id.get(current.parent_id)
        if current is None:
            break
        depth += 1
    return depth


def path_of(nodes_by_id: Dict[int, Leaf], node: Leaf) -> List[str]:
    """Ancestor descriptions, root first."""
    path, current = [], node
    while current.parent_id is not None and current.parent_id in nodes_by_id:
        current = nodes_by_id[current.parent_id]
        path.append(current.description)
    return list(reversed(path))


def _descendants(nodes: Sequence[Leaf], node_id: int) -> List[Leaf]:
    out, frontier = [], [node_id]
    while frontier:
        parent = frontier.pop()
        for n in nodes:
            if n.parent_id == parent:
                out.append(n)
                frontier.append(n.id)
    return out


def _cycles(nodes: Sequence[Leaf], changed_id: Optional[int], depends_on: Sequence[int]) -> bool:
    graph = {n.id: list(n.depends_on or []) for n in nodes}
    if changed_id is not None:
        graph[changed_id] = list(depends_on)
    visiting, done = set(), set()

    def visit(v):
        if v in done:
            return False
        if v in visiting:
            return True
        visiting.add(v)
        if any(visit(w) for w in graph.get(v, [])):
            return True
        visiting.discard(v)
        done.add(v)
        return False

    return any(visit(v) for v in list(graph))


def render_node(node: Leaf) -> str:
    bits = [f"[id={node.id}] ({node.level}{'/' + node.kind if node.kind else ''}) {node.description}"]
    if node.done_when:
        bits.append(f"done_when: {node.done_when}")
    if node.files:
        bits.append(f"files: {', '.join(node.files)}")
    if node.depends_on:
        bits.append(f"depends_on: {node.depends_on}")
    bits.append(f"status: {node.plan_status or 'unjudged'}" + (f" ({node.redo_reason})" if node.redo_reason else ""))
    return "\n  ".join(bits)


# ------------------------------------------------------------------ writes

def add_node(engine, session_id: str, role: str, scope_id: Optional[int], description: str,
             done_when: str = "", files: Sequence[str] = (), depends_on: Sequence[int] = (),
             kind: Optional[str] = None, parent_id: Optional[int] = None) -> str:
    description = (description or "").strip()
    if not description:
        return "Error: a node needs a description."
    if len(description) > item_max_chars():
        return (f"Error: the description is {len(description)} characters; keep it under {item_max_chars()} "
                "(PLANNER_ITEM_MAX_CHARS). Say less, or split the work into more nodes.")
    if role in ("lead", "task"):
        if parent_id not in (None, scope_id):
            return f"Error: you can only add nodes under node {scope_id}, the node you are working on."
        parent_id = scope_id
    nodes = load_nodes(engine, session_id)
    by_id = {n.id: n for n in nodes}
    if parent_id is not None:
        parent = by_id.get(parent_id)
        if parent is None:
            return f"Error: no node {parent_id}."
        if parent.status == LeafStatus.DONE:
            return f"Error: node {parent_id} is already done; add a new top-level node instead."
        if depth_of(by_id, parent) + 1 > MAX_LEAF_DEPTH:
            return (f"Error: that would be depth {depth_of(by_id, parent) + 1} (max {MAX_LEAF_DEPTH}). "
                    f"Treat node {parent_id} as small enough and stop splitting it.")
    missing = [d for d in depends_on if d not in by_id]
    if missing:
        return f"Error: depends_on names unknown node(s) {missing}."
    siblings = children_of(nodes, parent_id)
    sort_key = (max((s.sort_key for s in siblings), default=0) // 10 + 1) * 10
    with get_session(engine) as db:
        node = Leaf(session_id=session_id, parent_id=parent_id, phase=Phase.IMP, sort_key=sort_key,
                    description=description, level=role, kind=kind, done_when=done_when.strip() or None,
                    files=list(files) or None, depends_on=list(depends_on) or None)
        db.add(node)
        db.commit()
        db.refresh(node)
        return f"Added node id={node.id}" + (f" under {parent_id}" if parent_id else " (top level)") + "."


def update_node(engine, session_id: str, role: str, node_id: int, description: Optional[str] = None,
                done_when: Optional[str] = None, files: Optional[Sequence[str]] = None,
                depends_on: Optional[Sequence[int]] = None, kind: Optional[str] = None) -> str:
    nodes = load_nodes(engine, session_id)
    by_id = {n.id: n for n in nodes}
    node = by_id.get(node_id)
    if node is None:
        return f"Error: no node {node_id}."
    if node.level != role:
        return f"Error: node {node_id} was written by the {node.level}; only the {node.level} can change it."
    if node.status == LeafStatus.DONE:
        return f"Error: node {node_id} is done; it can't be rewritten."
    if description is not None:
        description = description.strip()
        if not description or len(description) > item_max_chars():
            return f"Error: a description must be 1-{item_max_chars()} characters."
    if depends_on is not None:
        missing = [d for d in depends_on if d not in by_id]
        if missing:
            return f"Error: depends_on names unknown node(s) {missing}."
        if _cycles(nodes, node_id, depends_on):
            return "Error: that depends_on would create a cycle."
    with get_session(engine) as db:
        row = db.get(Leaf, node_id)
        if description is not None:
            row.description = description
        if done_when is not None:
            row.done_when = done_when.strip() or None
        if files is not None:
            row.files = list(files) or None
        if depends_on is not None:
            row.depends_on = list(depends_on) or None
        if kind is not None:
            row.kind = kind
        row.plan_status = None  # rewritten -> re-judged
        db.add(row)
        db.commit()
    return f"Updated node {node_id}; it will be judged again."


def delete_node(engine, session_id: str, role: str, node_id: int) -> str:
    nodes = load_nodes(engine, session_id)
    node = next((n for n in nodes if n.id == node_id), None)
    if node is None:
        return f"Error: no node {node_id}."
    if node.level != role:
        return f"Error: node {node_id} was written by the {node.level}; only the {node.level} can delete it."
    if children_of(nodes, node_id):
        return f"Error: node {node_id} has children; it can't be deleted."
    if node.status == LeafStatus.DONE:
        return f"Error: node {node_id} is done; it can't be deleted."
    with get_session(engine) as db:
        for other in db.exec(select(Leaf).where(Leaf.session_id == session_id)):
            if other.depends_on and node_id in other.depends_on:
                other.depends_on = [d for d in other.depends_on if d != node_id] or None
                db.add(other)
        db.delete(db.get(Leaf, node_id))
        db.commit()
    return f"Deleted node {node_id}."


def escalate(engine, session_id: str, role: str, scope_id: int, why: str) -> str:
    """A lower layer can't fix this within the current design (a missing
    contract, the wrong component): send the layer above's node back to its
    creator as REDO and pause that node's subtree until it's settled again
    (§4.7). In a breakdown the scope node IS the layer above's (the Lead
    breaking down an Architect component); in a redo the scope node is the
    caller's own, so it's the scope's parent that goes back."""
    why = (why or "").strip()
    if not why:
        return "Error: say why the layer above has to fix it."
    nodes = load_nodes(engine, session_id)
    by_id = {n.id: n for n in nodes}
    node = by_id.get(scope_id)
    if node is None:
        return f"Error: no node {scope_id}."
    if node.level != role:
        parent = node
    elif node.parent_id is not None:
        parent = by_id[node.parent_id]
    else:
        return "Error: nothing above this node to escalate to; fix it within the design."
    if parent.escalation_count >= escalation_cap():
        return (f"Error: node {parent.id} was already escalated {parent.escalation_count} time(s) (cap "
                f"{escalation_cap()}). Do your best within the current design and note the problem.")
    with get_session(engine) as db:
        row = db.get(Leaf, parent.id)
        row.plan_status, row.redo_reason = REDO, "design"
        row.escalation_count += 1
        db.add(row)
        for child in _descendants(nodes, parent.id):
            c = db.get(Leaf, child.id)
            c.paused = True
            db.add(c)
        db.add(PlanEvent(session_id=session_id, node_id=parent.id, type="escalate",
                         detail=f"from node {scope_id} ({role}): {why}"))
        db.commit()
    return f"Escalated: node {parent.id} goes back to the {parent.level} to redo ({why})."


# ------------------------------------------------------------------ reads

def get_node(engine, session_id: str, node_id: int) -> str:
    node = next((n for n in load_nodes(engine, session_id) if n.id == node_id), None)
    return render_node(node) if node else f"Error: no node {node_id}."


def list_nodes(engine, session_id: str, parent_id: Optional[int] = None) -> str:
    nodes = load_nodes(engine, session_id)
    kids = children_of(nodes, parent_id)
    if not kids:
        return "No nodes there yet."
    return cap_result("\n".join(f"[id={n.id}] {n.description} ({n.plan_status or 'unjudged'})" for n in kids),
                      "Use get_node(id) for one node.")


def render_plan(engine, session_id: str) -> str:
    nodes = load_nodes(engine, session_id)
    if not nodes:
        return "The plan is empty."
    lines: List[str] = []

    def walk(parent_id, depth):
        for n in children_of(nodes, parent_id):
            lines.append("  " * depth + f"[id={n.id}] ({n.level}) {n.description} -- {n.plan_status or 'unjudged'}")
            walk(n.id, depth + 1)

    walk(None, 0)
    return cap_result("\n".join(lines), "Use get_node(id) or list_nodes(parent_id) to look closer.")


# ------------------------------------------------------------------ tool binding + schemas

def make_node_tools(engine, session_id: str, role: str, scope_id: Optional[int]) -> Dict[str, Callable]:
    return {
        "add_node": lambda description, done_when="", files=(), depends_on=(), kind=None, parent_id=None: add_node(
            engine, session_id, role, scope_id, description, done_when, files, depends_on, kind, parent_id),
        "update_node": lambda node_id, description=None, done_when=None, files=None, depends_on=None, kind=None:
            update_node(engine, session_id, role, node_id, description, done_when, files, depends_on, kind),
        "delete_node": lambda node_id: delete_node(engine, session_id, role, node_id),
        "get_node": lambda node_id: get_node(engine, session_id, node_id),
        "list_nodes": lambda parent_id=None: list_nodes(engine, session_id, parent_id),
        "get_plan": lambda: render_plan(engine, session_id),
        "escalate": lambda why: escalate(engine, session_id, role, scope_id, why),
    }


def _fn(name, description, properties, required):
    return {"type": "function", "function": {"name": name, "description": description,
                                             "parameters": {"type": "object", "properties": properties,
                                                            "required": required}}}


_FIELDS = {
    "description": {"type": "string", "description": "<verb> <what> in <where>: <expected result>; max 200 chars"},
    "done_when": {"type": "string", "description": "the observable finish condition (on a function: its test case)"},
    "files": {"type": "array", "items": {"type": "string"}, "description": "files it creates/changes + test file"},
    "depends_on": {"type": "array", "items": {"type": "integer"}, "description": "node ids that must be done first"},
    "kind": {"type": "string", "description": "component/project | code/artifact/section | "
                                              "implement/modify/delete/fill/passage"},
}
NODE_TOOL_SCHEMAS = [
    _fn("add_node", "Add a plan node. Architect adds top-level nodes; Lead and Task add nodes under the node "
        "they are working on.", {**_FIELDS, "parent_id": {"type": "integer"}}, ["description", "done_when"]),
    _fn("update_node", "Rewrite a node you created (it is judged again).",
        {"node_id": {"type": "integer"}, **_FIELDS}, ["node_id"]),
    _fn("delete_node", "Delete a node you created that has no children.", {"node_id": {"type": "integer"}},
        ["node_id"]),
    _fn("get_node", "Read one node's fields.", {"node_id": {"type": "integer"}}, ["node_id"]),
    _fn("list_nodes", "List the children of a node (or the top level), one line each.",
        {"parent_id": {"type": "integer"}}, []),
    _fn("get_plan", "The whole plan tree, one line per node.", {}, []),
    _fn("escalate", "Your node can't be fixed at your layer (a missing contract, the wrong component): send its "
        "parent back to the layer above to redo, with the reason.", {"why": {"type": "string"}}, ["why"]),
]
