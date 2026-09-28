"""runbook_set / runbook_get -- the v2 runbook (laya_plan.md §3.1): how to set
up, run, stop, view, test and build the app. Operating the app is never a
plan node; it lives here, where every role and phase can pull the one entry
it needs instead of rediscovering "which interpreter / which port / how to
stop it" (the observed failure CONTEXT_CACHE_RULES' run_commands key was a
workaround for).

A brief carries only runbook_index() -- one line of entry names; the model
pulls a command with runbook_get(name). Not wired into v1 prompts.
"""

import re
from typing import Callable, Dict, Optional

from sqlmodel import select

from JFI.models import RunbookEntry, get_session
from JFI.models._util import utcnow
from JFI.tool.result_cap import cap_result

_NAME = re.compile(r"^[a-z][a-z0-9_]{0,39}$")

#: The entries Architect drafts for every project (§4.3); others are allowed.
STANDARD_ENTRIES = ("setup", "run", "stop", "view", "test", "test_one", "build", "logs", "e2e")


def runbook_set(engine, session_id: str, name: str, command: str, notes: str = "",
                verified: bool = False, role: str = "") -> str:
    """Upserts one entry. Changing an entry's command resets `verified`
    unless the caller verifies the new command in the same call."""
    name = (name or "").strip().lower()
    command = (command or "").strip()
    if not _NAME.match(name):
        return (f"Error: runbook name {name!r} must be lowercase letters, digits or _ (e.g. "
                f"{', '.join(STANDARD_ENTRIES)}).")
    if not command:
        return f"Error: runbook entry {name!r} needs a command."
    with get_session(engine) as db:
        row = db.exec(select(RunbookEntry).where(RunbookEntry.session_id == session_id,
                                                 RunbookEntry.name == name)).first()
        created = row is None
        if created:
            row = RunbookEntry(session_id=session_id, name=name, command=command)
        elif row.command != command:
            row.command, row.verified, row.verified_at = command, False, None
        row.notes = notes if notes else row.notes
        if verified:
            row.verified, row.verified_at = True, utcnow()
        row.updated_by, row.updated_at = role, utcnow()
        db.add(row)
        db.commit()
        state = "verified" if row.verified else "unverified"
    return f"{'Added' if created else 'Updated'} runbook entry {name!r} ({state})."


def runbook_get(engine, session_id: str, name: Optional[str] = None) -> str:
    with get_session(engine) as db:
        rows = list(db.exec(select(RunbookEntry).where(RunbookEntry.session_id == session_id)
                            .order_by(RunbookEntry.name)))
    if not name:
        if not rows:
            return "The runbook is empty."
        body = "\n".join(_render(r) for r in rows)
        return cap_result(body, "Call runbook_get(name) for one entry.")
    name = name.strip().lower()
    row = next((r for r in rows if r.name == name), None)
    if row is None:
        return f"Error: no runbook entry {name!r}. {runbook_index_from(rows)}"
    return _render(row)


def _render(row: RunbookEntry) -> str:
    state = "verified" if row.verified else "unverified"
    return f"{row.name} ({state}): {row.command}" + (f"\n  notes: {row.notes}" if row.notes else "")


def runbook_index_from(rows) -> str:
    if not rows:
        return "Runbook: empty."
    names = ", ".join(r.name + (" ✓" if r.verified else "") for r in sorted(rows, key=lambda r: r.name))
    return f"Runbook: {names} -- runbook_get(name) for the command (✓ = verified)."


def runbook_index(engine, session_id: str) -> str:
    """The one line a v2 brief carries (laya_plan.md §0: pull, don't push)."""
    with get_session(engine) as db:
        rows = list(db.exec(select(RunbookEntry).where(RunbookEntry.session_id == session_id)))
    return runbook_index_from(rows)


def make_runbook_tools(engine, session_id: str, role: str = "") -> Dict[str, Callable]:
    return {
        "runbook_set": lambda name, command, notes="", verified=False: runbook_set(
            engine, session_id, name, command, notes, verified, role),
        "runbook_get": lambda name=None: runbook_get(engine, session_id, name),
    }


RUNBOOK_TOOL_SCHEMAS = [
    {"type": "function", "function": {
        "name": "runbook_set",
        "description": ("Add or update one runbook entry: how to operate the app (setup, run, stop, view, "
                        "test, test_one, build, logs, e2e, ...). Operating the app is never a plan node -- "
                        "it goes here. Set verified=true only right after the command actually ran "
                        "successfully."),
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string", "description": "e.g. run, test_one"},
            "command": {"type": "string", "description": "the exact command; may contain {placeholders}"},
            "notes": {"type": "string", "description": "e.g. needed env vars, the port"},
            "verified": {"type": "boolean"},
        }, "required": ["name", "command"]},
    }},
    {"type": "function", "function": {
        "name": "runbook_get",
        "description": "Get one runbook entry's command by name, or every entry when no name is given.",
        "parameters": {"type": "object", "properties": {"name": {"type": "string"}}},
    }},
]
