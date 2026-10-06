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

import html
import os
import re
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence

from sqlmodel import select

from JFI.models import Leaf, Phase, PlanEvent, RunbookEntry, get_session
from JFI.models.enums import LeafStatus
from JFI.tool.evidence_tools import case_problem, read_evidence
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
    if node.notes:
        bits.append(f"notes: {node.notes}")
    if node.references:
        bits.append(f"references: {', '.join(node.references)}")
    if node.files:
        bits.append(f"files: {', '.join(node.files)}")
    if node.depends_on:
        bits.append(f"depends_on: {node.depends_on}")
    if node.cases:
        bits.append(f"cases: {', '.join(node.cases)}")
    bits.append(f"status: {node.plan_status or 'unjudged'}" + (f" ({node.redo_reason})" if node.redo_reason else ""))
    return "\n  ".join(bits)


# No space before "(": on the stui runs "copied verbatim (L1376-1402)" and
# "exactly (…)" read as functions verbatim() and exactly(), and valid nodes
# were refused as duplicates of each other.
_CALL = re.compile(r"\b([A-Za-z_]\w*)\(")


def target_symbol(description: str) -> Optional[str]:
    """The function a node is FOR: the first `name(` in "implement name(...)
    in file: ...". Later mentions are the helpers it calls."""
    match = _CALL.search(description or "")
    return match.group(1) if match else None


def _duplicate_of(nodes: Sequence[Leaf], description: str, files: Sequence[str]) -> Optional[Leaf]:
    """An unfinished node already for the same function in the same file.
    Observed on the first real v2 run: the Task splitting "parse()" also
    added an "implement evaluate()" leaf (evaluate's stub was in the same
    file), and the Task splitting "evaluate()" added another -- so Dev
    implemented evaluate twice."""
    symbol = target_symbol(description)
    if symbol is None or not files:
        return None
    parents = {n.parent_id for n in nodes}
    return next((n for n in nodes if n.id not in parents and n.status != LeafStatus.DONE
                 and target_symbol(n.description) == symbol and set(n.files or []) & set(files)), None)


def _sources(files: Sequence[str]) -> set:
    return {f for f in files if "test" not in f.lower()}


def _file_owner(nodes: Sequence[Leaf], files: Sequence[str]) -> Optional[Leaf]:
    """The Lead file node that already owns one of these (non-test) files.
    Observed on the stui run: the entry component's Lead added a second
    index.html node although the scaffold component's Lead had one, so two
    Dev leaves would each rewrite the page. Test files are exempt: sharing
    one is allowed."""
    wanted = _sources(files)
    if not wanted:
        return None
    return next((n for n in nodes if n.level == "lead" and n.status != LeafStatus.DONE
                 and _sources(n.files or []) & wanted), None)


# ------------------------------------------------------------------ writes

KINDS = ("project", "component", "code", "artifact", "section", "implement", "integrate", "modify", "delete",
         "fill", "passage", "compare")


def _normalise_kind(kind: Optional[str]) -> Optional[str]:
    """The first known kind in what the model sent. Observed on the stui run
    (gemma): kinds came back as the schema's own list, "code/artifact/section",
    copied literally."""
    for token in re.split(r"[^a-z]+", (kind or "").lower()):
        if token in KINDS:
            return token
    return None


def _plain(text: Optional[str]) -> str:
    """Observed on the react_counter run: the Task planner wrote "&lt;head&gt;"
    for <head> in descriptions and notes, which Dev and the dashboards then
    showed escaped. Node text is plain text; entities are decoded."""
    return html.unescape(text or "").strip()


# Observed on the react_counter run: notes that open with where to edit
# ("Replace the JFI comment at index.html L3. ...") and only then, if at all,
# say what the item is for. The location belongs in references.
_LOCATION_FIRST = re.compile(r"^(replace|fill|swap|substitute)\b[^.]{0,60}?\bJFI\b", re.IGNORECASE)


def _notes_problem(notes: str) -> Optional[str]:
    if _LOCATION_FIRST.match(notes):
        return ("Error: notes must start with WHAT this item must do and why -- the behaviour or result, its "
                "inputs and outputs, edge cases -- not with where to edit (\"Replace the JFI comment at ...\"). "
                "Put the location in references (e.g. \"index.html L3\") and rewrite the notes.")
    return None


def _too_long(description: str) -> str:
    """Observed on the stui run: the Architect packed exports, line ranges and
    steps into node descriptions, and 14 of its tool calls came back "too
    long" -- the old message said only "say less", so it retried with almost
    the same text. Say where the detail belongs instead."""
    return (f"Error: the description is {len(description)} characters; the limit is {item_max_chars()} "
            "(PLANNER_ITEM_MAX_CHARS). A description only names the work and where it goes, e.g. "
            "\"Create src/views/news.js: news feed + filter chips\". Put the details (exports, steps, edge "
            "cases) in notes, and where to look (design entries, line ranges, docs) in references.")


def _unknown_ids(nodes: Sequence[Leaf], parent_id: Optional[int], missing: Sequence[int]) -> str:
    """Observed on the stui runs: ~29 depends_on errors gave sibling positions
    ([1], [1, 2]) instead of ids, and the old message didn't say which ids
    exist."""
    siblings = children_of(nodes, parent_id)
    known = "; ".join(f"{n.id} ({n.description[:50]})" for n in siblings[:12]) or "none yet"
    return (f"Error: depends_on names unknown node(s) {list(missing)}. depends_on takes node ids (the id= that "
            f"add_node returned), not positions. Ids here: {known}.")


_TEST_FILE = re.compile(r"(^|/)(tests?|__tests__|spec)/|(\.|_)(test|spec)\.[^/]+$|(^|/)test_[^/]+$", re.I)


def repo_path(path: str) -> str:
    path = path.strip().replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    return path.strip("/")


def _test_path_problem(engine, session_id: str, files: Sequence[str]) -> Optional[str]:
    """A test file that doesn't follow the runbook's test_dir. Observed on the
    QA machine: tests landed both beside their source and in __tests__/,
    while test_one only ran __tests__/{test_id}.test.js."""
    tests = [f.replace("\\", "/") for f in files if _TEST_FILE.search(f.replace("\\", "/"))]
    if not tests:
        return None
    with get_session(engine) as db:
        entries = {r.name: r.command for r in db.exec(select(RunbookEntry).where(
            RunbookEntry.session_id == session_id, RunbookEntry.name.in_(("test_dir", "test_naming"))))}
    test_dir = (entries.get("test_dir") or "").strip()
    if not test_dir:
        return None
    naming = f" ({entries['test_naming']})" if entries.get("test_naming") else ""
    if re.search(r"\b(beside|next to|same (dir|folder)|colocated|alongside)\b", test_dir, re.I):
        source_dirs = {f.replace("\\", "/").rsplit("/", 1)[0] for f in files if f not in tests and "/" in f}
        wrong = [t for t in tests if source_dirs and t.rsplit("/", 1)[0] not in source_dirs]
        where = "beside their source files"
    else:
        # Observed on the react_counter run: test_dir "." (the repo root) became
        # the folder "/", so every path was refused -- test_index.py,
        # ./test_index.py, tests/test_index.py -- and the Task planner escalated.
        folder = repo_path(test_dir)
        if folder in ("", "."):
            wrong = [t for t in tests if "/" in repo_path(t)]
            where = "at the repo root"
        else:
            wrong = [t for t in tests if not repo_path(t).startswith(folder + "/")]
            where = f"under {folder}/"
    if not wrong:
        return None
    return (f"Error: the runbook puts tests {where}{naming}, but {', '.join(wrong)} isn't. Use the path the "
            f"runbook's test_dir and test_naming give.")


def _normalise_cases(cases: Sequence[str]) -> tuple[list, Optional[str]]:
    names = [str(c).strip().lower() for c in (cases or []) if str(c).strip()]
    for name in names:
        problem = case_problem(name)
        if problem:
            return [], problem
    return list(dict.fromkeys(names)), None


def _cases_problem(nodes: Sequence[Leaf], role: str, scope_id: Optional[int], node_id: Optional[int], kind: Optional[str],
                   cases: Sequence[str], depends_on: Sequence[int], root: Optional[Path]) -> Optional[str]:
    """The ground-truth rules (docs/old_new.md), checked here rather than
    trusted to the prompt: a Lead's case is on one file node only; a compare
    leaf names cases whose evidence exists, that its file node owns, and
    comes after the leaf that builds them."""
    if kind == "compare":
        if not cases:
            return ("Error: a compare leaf needs cases=[...]: the case(s) it checks, each with its evidence in "
                    "evidences/<case>.*.")
        if not depends_on:
            return ("Error: a compare leaf needs depends_on=[<the leaf that builds what it checks>]: it runs "
                    "after that leaf.")
        if root is not None:
            missing = [c for c in cases if read_evidence(root, c) is None]
            if missing:
                return (f"Error: no evidence for {', '.join(missing)} in evidences/. A compare leaf checks evidence "
                        f"the Lead captured; list_evidence shows what exists.")
        scope = next((n for n in nodes if n.id == scope_id), None)
        if role == "task" and scope is not None and scope.cases:
            foreign = [c for c in cases if c not in scope.cases]
            if foreign:
                return (f"Error: {', '.join(foreign)} isn't one of this file's cases ({', '.join(scope.cases)}). "
                        f"Only check the cases in SCOPE.")
    elif cases and role == "lead":
        taken = {c: n.id for n in nodes if n.level == "lead" and n.id != node_id for c in (n.cases or [])}
        clash = [c for c in cases if c in taken]
        if clash:
            return (f"Error: case {clash[0]} is already on node {taken[clash[0]]}. A case belongs to one file: the "
                    f"file whose code produces it.")
    elif cases and role != "lead":
        return "Error: only the Lead puts cases on a file node, and only Task's compare leaves check them."
    return None


def add_node(engine, session_id: str, role: str, scope_id: Optional[int], description: str,
             done_when: str = "", files: Sequence[str] = (), depends_on: Sequence[int] = (),
             kind: Optional[str] = None, parent_id: Optional[int] = None, notes: str = "",
             references: Sequence[str] = (), cases: Sequence[str] = (), root: Optional[Path] = None) -> str:
    description, done_when, notes = _plain(description), _plain(done_when), _plain(notes)
    if not description:
        return "Error: a node needs a description."
    if _notes_problem(notes):
        return _notes_problem(notes)
    if len(description) > item_max_chars():
        return _too_long(description)
    if parent_id == 0:
        # Observed on the stui run (gemma): the Architect passed parent_id=0
        # for top-level nodes, copying finish(0, ...), and got "no node 0" seven
        # times. 0 is never a node id; read it as "top level".
        parent_id = None
    kind = _normalise_kind(kind)
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
        return _unknown_ids(nodes, parent_id, missing)
    cases, problem = _normalise_cases(cases)
    problem = problem or _cases_problem(nodes, role, scope_id, None, kind, cases, depends_on, root)
    if problem:
        return problem
    test_problem = _test_path_problem(engine, session_id, files)
    if test_problem:
        return test_problem
    duplicate = _duplicate_of([n for n in nodes if n.id != parent_id], description, files)
    if duplicate is not None:
        return (f"Error: node {duplicate.id} already covers {target_symbol(description)}() in "
                f"{', '.join(duplicate.files or [])}. Only add nodes for the work YOUR node names.")
    if role == "lead":
        owner = _file_owner(nodes, files)
        if owner is not None:
            return (f"Error: node {owner.id} (another component's file node) already owns "
                    f"{', '.join(_sources(files) & _sources(owner.files or []))}. A file belongs to one node; "
                    "leave it to that node, and put anything yours needs from it in the design (design_set).")
    siblings = children_of(nodes, parent_id)
    sort_key = (max((s.sort_key for s in siblings), default=0) // 10 + 1) * 10
    with get_session(engine) as db:
        node = Leaf(session_id=session_id, parent_id=parent_id, phase=Phase.IMP, sort_key=sort_key,
                    description=description, level=role, kind=kind, done_when=done_when or None,
                    notes=notes or None, references=list(references) or None,
                    files=list(files) or None, depends_on=list(depends_on) or None, cases=cases or None)
        db.add(node)
        db.commit()
        db.refresh(node)
        return f"Added node id={node.id}" + (f" under {parent_id}" if parent_id else " (top level)") + "."


def update_node(engine, session_id: str, role: str, node_id: int, description: Optional[str] = None,
                done_when: Optional[str] = None, files: Optional[Sequence[str]] = None,
                depends_on: Optional[Sequence[int]] = None, kind: Optional[str] = None,
                notes: Optional[str] = None, references: Optional[Sequence[str]] = None,
                cases: Optional[Sequence[str]] = None, root: Optional[Path] = None) -> str:
    nodes = load_nodes(engine, session_id)
    by_id = {n.id: n for n in nodes}
    node = by_id.get(node_id)
    if node is None:
        return f"Error: no node {node_id}."
    if node.level != role:
        return f"Error: node {node_id} was written by the {node.level}; only the {node.level} can change it."
    if node.status == LeafStatus.DONE:
        return f"Error: node {node_id} is done; it can't be rewritten."
    if notes is not None:
        notes = _plain(notes)
        if _notes_problem(notes):
            return _notes_problem(notes)
    if description is not None:
        description = _plain(description)
        if not description or len(description) > item_max_chars():
            return _too_long(description) if description else "Error: a description can't be empty."
    if depends_on is not None:
        missing = [d for d in depends_on if d not in by_id]
        if missing:
            return _unknown_ids(nodes, node.parent_id, missing)
        if _cycles(nodes, node_id, depends_on):
            return "Error: that depends_on would create a cycle."
    if files is not None:
        test_problem = _test_path_problem(engine, session_id, files)
        if test_problem:
            return test_problem
    if cases is not None:
        cases, problem = _normalise_cases(cases)
        problem = problem or _cases_problem(
            nodes, role, node.parent_id if role == "task" else None, node_id, _normalise_kind(kind) or node.kind,
            cases, depends_on if depends_on is not None else (node.depends_on or []), root)
        if problem:
            return problem
    with get_session(engine) as db:
        row = db.get(Leaf, node_id)
        if description is not None:
            row.description = description
        if done_when is not None:
            row.done_when = _plain(done_when) or None
        if notes is not None:
            row.notes = notes or None
        if references is not None:
            row.references = list(references) or None
        if files is not None:
            row.files = list(files) or None
        if depends_on is not None:
            row.depends_on = list(depends_on) or None
        if cases is not None:
            row.cases = list(cases) or None
        if _normalise_kind(kind) is not None:
            row.kind = _normalise_kind(kind)
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

def make_node_tools(engine, session_id: str, role: str, scope_id: Optional[int],
                    root: Optional[Path] = None) -> Dict[str, Callable]:
    return {
        "add_node": lambda description, done_when="", files=(), depends_on=(), kind=None, parent_id=None,
        notes="", references=(), cases=(): add_node(engine, session_id, role, scope_id, description, done_when, files,
                                                    depends_on, kind, parent_id, notes, references, cases, root),
        "update_node": lambda node_id, description=None, done_when=None, files=None, depends_on=None, kind=None,
        notes=None, references=None, cases=None: update_node(engine, session_id, role, node_id, description,
                                                             done_when, files, depends_on, kind, notes, references,
                                                             cases, root),
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
    "description": {"type": "string", "description": "<verb> <what> in <where>: <expected result>; max 200 chars. "
                                                "Details go in notes; where to look goes in references"},
    "notes": {"type": "string", "description": "what this item must do and why, first: the behaviour or result, inputs "
                                          "and outputs, edge cases, the contract to follow, what not to touch. Plain "
                                          "text. Never start with where to edit -- that goes in references"},
    "references": {"type": "array", "items": {"type": "string"},
                   "description": "where the context lives: design entries as kind:key (e.g. contract:main->calc), "
                                  "source ranges (e.g. page.html L1376-1402), docs or URLs"},
    "done_when": {"type": "string", "description": "the observable finish condition (on a function: its test case)"},
    "files": {"type": "array", "items": {"type": "string"}, "description": "files it creates/changes + test file"},
    "depends_on": {"type": "array", "items": {"type": "integer"}, "description": "node ids that must be done first"},
    "kind": {"type": "string", "description": "ONE word. Architect: component or project. Lead: code or artifact. "
                                              "Task: implement, integrate, modify, delete, fill or compare."},
    "cases": {"type": "array", "items": {"type": "string"},
              "description": "ground-truth cases (evidence in evidences/<case>.*). Lead: the cases this file's code "
                             "must match. Task, on a compare leaf: the case(s) it checks"},
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
