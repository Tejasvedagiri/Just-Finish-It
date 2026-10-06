"""design_set / design_get -- the v2 design table (laya_plan.md §3.2): the
stack, components, contracts between them, conventions, assumptions and
what's out of scope.

Lead, Task and Dev each see only one node, so the design (with the runbook)
is their only view of the whole app. Keyed entries let an episode pull the
one contract it needs instead of the whole design; a brief carries only
design_index(). Written by Architect (Lead may add conventions). Not wired
into v1 prompts.
"""

from typing import Callable, Dict, Optional

from sqlmodel import select

from JFI.models import DesignEntry, get_session
from JFI.models._util import utcnow
from JFI.tool.result_cap import cap_result

KINDS = ("stack", "component", "contract", "convention", "assumption", "out_of_scope", "outline", "reference")


def design_set(engine, session_id: str, kind: str, key: str, text: str, role: str = "") -> str:
    kind, key, text = (kind or "").strip().lower(), (key or "").strip(), (text or "").strip()
    if kind not in KINDS:
        return f"Error: design kind {kind!r} must be one of {', '.join(KINDS)}."
    if not key or not text:
        return "Error: a design entry needs both a key and text."
    with get_session(engine) as db:
        row = db.exec(select(DesignEntry).where(DesignEntry.session_id == session_id,
                                                DesignEntry.kind == kind, DesignEntry.key == key)).first()
        created = row is None
        if created:
            row = DesignEntry(session_id=session_id, kind=kind, key=key, text=text, created_by=role)
        else:
            row.text = text
        row.updated_at = utcnow()
        db.add(row)
        db.commit()
    return f"{'Added' if created else 'Updated'} design {kind} {key!r}."


def design_get(engine, session_id: str, kind: Optional[str] = None, key: Optional[str] = None) -> str:
    with get_session(engine) as db:
        rows = list(db.exec(select(DesignEntry).where(DesignEntry.session_id == session_id)
                            .order_by(DesignEntry.kind, DesignEntry.key)))
    if not kind:
        if key:
            matches = [r for r in rows if r.key == key.strip()]
            if not matches:
                return f"Error: no design entry with key {key!r}. {design_index_from(rows)}"
            return cap_result("\n".join(_render(r) for r in matches), "Pass kind too, to narrow it down.")
        return design_index_from(rows)
    kind = kind.strip().lower()
    if kind not in KINDS:
        return f"Error: design kind {kind!r} must be one of {', '.join(KINDS)}."
    of_kind = [r for r in rows if r.kind == kind]
    if key:
        row = next((r for r in of_kind if r.key == key.strip()), None)
        if row is None:
            return f"Error: no design {kind} {key!r}. {design_index_from(rows)}"
        return _render(row)
    if not of_kind:
        return f"No design {kind} entries yet. {design_index_from(rows)}"
    return cap_result("\n".join(_render(r) for r in of_kind), f"Call design_get('{kind}', key) for one entry.")


def _render(row: DesignEntry) -> str:
    return f"{row.kind} / {row.key}: {row.text}"


def design_index_from(rows) -> str:
    if not rows:
        return "Design: empty."
    by_kind: Dict[str, list] = {}
    for r in rows:
        by_kind.setdefault(r.kind, []).append(r.key)
    parts = "; ".join(f"{k}: {', '.join(sorted(v))}" for k, v in sorted(by_kind.items()))
    return f"Design: {parts} -- design_get(kind, key) for one entry."


def design_index(engine, session_id: str) -> str:
    """The one line a v2 brief carries (laya_plan.md §0: pull, don't push)."""
    with get_session(engine) as db:
        rows = list(db.exec(select(DesignEntry).where(DesignEntry.session_id == session_id)))
    return design_index_from(rows)


def references_text(engine, session_id: str, references) -> list[str]:
    """A node's references for its brief: a "kind:key" naming a design entry
    is shown with that entry's text, so the episode doesn't spend turns on
    design_get (16% of all tool calls on the real runs); anything else (a
    source range, a doc, a URL) is shown as written."""
    lines = []
    with get_session(engine) as db:
        rows = list(db.exec(select(DesignEntry).where(DesignEntry.session_id == session_id)))
    by_ref = {f"{r.kind}:{r.key}": r for r in rows}
    for ref in references or []:
        row = by_ref.get(str(ref).strip())
        lines.append(f"{ref} -- {row.text}" if row else str(ref))
    return lines


def make_design_tools(engine, session_id: str, role: str = "") -> Dict[str, Callable]:
    return {
        "design_set": lambda kind, key, text: design_set(engine, session_id, kind, key, text, role),
        "design_get": lambda kind=None, key=None: design_get(engine, session_id, kind, key),
    }


DESIGN_TOOL_SCHEMAS = [
    {"type": "function", "function": {
        "name": "design_set",
        "description": ("Add or update one design entry: the stack, a component, a contract between "
                        "components, a convention, an assumption, something out of scope, a document "
                        "outline section, or a reference: the ground truth the build must match (a command, "
                        "page, mockup, expected output, API docs, old program) and what must match / may differ."),
        "parameters": {"type": "object", "properties": {
            "kind": {"type": "string", "enum": list(KINDS)},
            "key": {"type": "string", "description": "e.g. persistence, api->persistence"},
            "text": {"type": "string"},
        }, "required": ["kind", "key", "text"]},
    }},
    {"type": "function", "function": {
        "name": "design_get",
        "description": ("Read the design: no arguments gives the index of kinds and keys; a kind gives all "
                        "entries of that kind; kind and key give one entry."),
        "parameters": {"type": "object", "properties": {
            "kind": {"type": "string", "enum": list(KINDS)},
            "key": {"type": "string"},
        }},
    }},
]
