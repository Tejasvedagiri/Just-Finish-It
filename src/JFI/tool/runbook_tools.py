"""runbook_set / runbook_get -- the v2 runbook (laya_plan.md §3.1): how to set
up, run, stop, view, test and build the app. Operating the app is never a
plan node; it lives here, where every role and phase can pull the one entry
it needs instead of rediscovering "which interpreter / which port / how to
stop it" (an observed v1 failure its context cache's run_commands key was a
workaround for).

A brief carries only runbook_index() -- one line of entry names; the model
pulls a command with runbook_get(name). Not wired into v1 prompts.
"""

import re
import shutil
from typing import Callable, Dict, Optional

from sqlmodel import select

from JFI.models import RunbookEntry, get_session
from JFI.models._util import utcnow
from JFI.tool.result_cap import cap_result

_NAME = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
_KILL_BY_NAME = re.compile(r"\btaskkill\b[^|&]*\s/IM\b|\bpkill\b|\bkillall\b|Stop-Process\s+-Name\b", re.I)
#: Entries later roles execute and read the exit code of. stop/view may be
#: instructions (Ctrl+C, a URL to open), so they aren't checked.
COMMAND_ENTRIES = ("setup", "run", "test", "test_one", "build", "e2e", "evidence_one", "compare_one")
_KNOWN_RUNNERS = {"npm", "npx", "node", "pnpm", "yarn", "bun", "deno", "python", "python3", "py", "uv", "pip",
                  "pytest", "go", "cargo", "make", "dotnet", "mvn", "gradle", "java", "ruby", "bundle", "php",
                  "composer", "bash", "sh", "cmd", "powershell", "pwsh", "docker", "vite", "vitest", "jest",
                  "tsc", "rustc", "gcc", "g++", "cmake", "ctest", "echo", "curl"}


def _starts_with_a_program(command: str) -> bool:
    first = command.strip().split()[0].strip("\"'") if command.strip() else ""
    name = first.replace("\\", "/").rsplit("/", 1)[-1].lower()
    name = re.sub(r"\.(exe|cmd|bat|ps1)$", "", name)
    # A shell group, `{ echo scale=10; cat {input_file}; } | bc -l`, runs programs too.
    return (name in _KNOWN_RUNNERS or "/" in first.replace("\\", "/") or first.startswith((".", "{", "("))
            or shutil.which(first) is not None)


_OPENS_BROWSER = re.compile(r"^\s*(start|open|xdg-open|explorer)\s+(\"\"\s+)?https?://", re.I)

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
    # Both observed on the stui runs, and a prompt rule alone didn't stop them.
    if _KILL_BY_NAME.search(command):
        return ("Error: that command kills every process with that name on the machine, not just this app. "
                "Stop only this app: Ctrl+C in its terminal, or stop_background_process for one started "
                "with start_background_process.")
    if name in COMMAND_ENTRIES and not _starts_with_a_program(command):
        # Observed on the stui run (gemma): e2e = "Compare current view with
        # portfolio-dashboard.html visually" -- a sentence, not a command.
        return (f"Error: runbook entry {name!r} must be a command that runs, starting with a program "
                f"(e.g. npm, npx, node, python, uv, pytest, go, cargo, make, or a script path), got "
                f"{command.split()[0]!r}. Write a check that exits non-zero on failure.")
    if name == "e2e" and _OPENS_BROWSER.match(command):
        return ("Error: e2e must be a check that exits non-zero when the app is broken (the reviewer runs it "
                "and reads the exit code); opening a browser checks nothing. E.g. build, then a small script "
                "that asserts on the output.")
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
