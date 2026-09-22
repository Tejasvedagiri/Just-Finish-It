"""``uv run export-db`` -- dumps a project's `.jfi/JFI.db` (see JFI.models,
which covers everything that used to live under a session's own
JFI/<session>/ folder except .lock) back into plain text files a human can
read, one pair per session.

**Debugging only.** This is a one-way, read-only export: JFI itself never
reads these files back. The entire point of the DB rewrite (see /todo.md)
was retiring markdown as a SOURCE OF TRUTH because two independent parsers
over plan.md (this project's own Python planner-phase scanner and
frontend/src/main.js's regex tree-parser) had each hit their own real bugs.
Reintroducing a markdown file that anything parses back in would just grow
a third one.

One `.jfi/JFI.db` per PROJECT now (not one session.db per session) -- every
table is keyed by session_id, so a single connection covers every session
ever run in that project; this command lists them all and exports each to
the SAME flat `.jfi/` folder JFI itself uses (no per-session subfolders at
all -- see SimpleSessionManager.__init__), so a project's own .gitignore
only ever needs the single `.jfi/` entry.

Usage:
    uv run export-db                  # uses ./.jfi/JFI.db, exports every session
    uv run export-db <project-dir>     # uses <project-dir>/.jfi/JFI.db
    uv run export-db <path/to/x.db>    # uses that db file directly
    uv run export-db --session=<name>  # (combined with any of the above) one session only

Each session's export writes two files, session_id-PREFIXED since `.jfi/`
is flat and one run routinely exports every session at once, and prints
both paths:
    plan_export.md   -- session summary, plan tree, and every other table
                         except the conversation (unlocked tools,
                         implemented files, context entries, queued items,
                         background processes, done phases)
    log_export.txt   -- LogEvent (every tag: SYSTEM/RULE/REASONING/
                         ASSISTANT/USER/TOOL_CALL/TOOL_RESULT/ERROR), in
                         seq order -- the full run.log-shaped view; this
                         table alone now carries everything the old
                         run.log file did (see pt_console_manager.py's
                         _log method), so there is nothing left to merge
                         in from HistoryMessage.
"""

import sys
from datetime import datetime, timezone
from pathlib import Path


def _render_plan_export(session_id: str, engine) -> str:
    from sqlmodel import select

    from JFI.models import (
        BackgroundProcess,
        ContextEntry,
        DonePhase,
        ImplementedFile,
        Leaf,
        QueuedItem,
        SessionNote,
        SessionRecord,
        UnlockedTool,
        build_indexes,
        display_number,
        get_session,
    )

    with get_session(engine) as db:
        record = db.get(SessionRecord, session_id)
        leaves = list(db.exec(select(Leaf).where(Leaf.session_id == session_id)))
        unlocked_tools = list(db.exec(select(UnlockedTool).where(UnlockedTool.session_id == session_id)))
        implemented_files = list(db.exec(select(ImplementedFile).where(ImplementedFile.session_id == session_id)))
        context_entries = list(db.exec(select(ContextEntry).where(ContextEntry.session_id == session_id)))
        queued_items = list(
            db.exec(select(QueuedItem).where(QueuedItem.session_id == session_id).order_by(QueuedItem.position))
        )
        background_processes = list(
            db.exec(select(BackgroundProcess).where(BackgroundProcess.session_id == session_id))
        )
        done_phases = list(db.exec(select(DonePhase).where(DonePhase.session_id == session_id)))
        session_notes = {
            note.kind: note.text
            for note in db.exec(select(SessionNote).where(SessionNote.session_id == session_id))
        }

    lines = [
        f"# {session_id} — DB export ({datetime.now(timezone.utc).isoformat(timespec='seconds')})",
        "",
        "_Debugging only — a one-way snapshot, never read back in by JFI._",
        "",
    ]

    if record is None:
        lines.append("_No SessionRecord row found for this session._")
    else:
        lines += [
            f"- repo: `{record.repo_path}`",
            f"- goal: {record.goal_text}",
            f"- task_type: {record.task_type or '—'}",
            f"- phase: {record.phase.value if record.phase else '—'}"
            f" / stage: {record.stage or '—'} / state: {record.state or '—'}",
            f"- iteration: {record.iteration}, queue_size: {record.queue_size}, paused: {record.is_paused}",
        ]
        if record.tokens_used is not None:
            lines.append(f"- tokens: {record.tokens_used}/{record.tokens_budget}")
        if record.tokens_read is not None or record.tokens_written is not None:
            lines.append(f"- tokens read/written: {record.tokens_read or 0}/{record.tokens_written or 0}")
        if record.current_task:
            lines.append(f"- current task: {record.current_task}")
        if record.awaiting_prompt:
            lines.append(f"- ⛔ awaiting input: {record.awaiting_prompt}")
        if record.digest_summary:
            lines.append(f"- digest: {record.digest_summary} ({record.digest_block_count} blocks)")
        lines.append(f"- done phases: {', '.join(p.phase.value for p in done_phases) or '—'}")
    lines.append("")

    by_id, siblings_by_parent = build_indexes(leaves)

    # A leaf's checkbox depends on whether it HAS children, never on its
    # depth -- called at depth 0 for root-level leaves too (see the loop
    # below), not just recursively, so a root-level item with no children
    # of its own still gets its checkbox instead of being rendered as if
    # it were always a parent (caught by test_plan_db_tools_wiring.py's
    # equivalent bug in JFI.tool.plan_db_tools's own renderer).
    def render_subtree(parent_id, phase, depth: int) -> None:
        for leaf in siblings_by_parent.get((parent_id, phase), []):
            children = siblings_by_parent.get((leaf.id, phase), [])
            number = display_number(leaf, by_id, siblings_by_parent)
            indent = "  " * depth
            if children:
                lines.append(f"{indent}- {number}. {leaf.description}")
            else:
                mark = {"done": "x", "skipped": "○"}.get(leaf.status.value, " ")
                timing = ""
                if leaf.started_at and leaf.ended_at:
                    seconds = (leaf.ended_at - leaf.started_at).total_seconds()
                    timing = f" ({seconds:.0f}s)"
                lines.append(f"{indent}- [{mark}] {number} {leaf.description}{timing}")
            render_subtree(leaf.id, phase, depth + 1)

    phases_in_order = []
    for leaf in leaves:
        if leaf.parent_id is None and leaf.phase not in phases_in_order:
            phases_in_order.append(leaf.phase)

    for phase in phases_in_order:
        lines.append(f"## {phase.value}")
        lines.append("")
        render_subtree(None, phase, 0)
        lines.append("")

    if not phases_in_order:
        lines.append("_No leaves recorded yet._")
        lines.append("")

    lines.append("## Unlocked tools")
    lines.append(", ".join(t.tool_name for t in unlocked_tools) or "_none_")
    lines.append("")

    lines.append("## Implemented files")
    lines += [f"- `{f.file_path}`" for f in implemented_files] or ["_none_"]
    lines.append("")

    lines.append("## Context entries")
    for entry in context_entries:
        lines.append(f"- **{entry.key}**: {entry.value}")
    if not context_entries:
        lines.append("_none_")
    lines.append("")

    lines.append("## Queued items")
    lines += [f"{i.position}. {i.text}" for i in queued_items] or ["_none_"]
    lines.append("")

    if session_notes:
        lines.append("## Pending review/feedback notes")
        _NOTE_LABELS = {
            "reviewer_notes": "Notes for reviewer (from implementation)",
            "review_report": "Review report (reviewer found issues)",
            "plan_feedback": "Product Owner feedback (plan rejected)",
        }
        for kind, text in session_notes.items():
            lines.append(f"### {_NOTE_LABELS.get(kind, kind)}")
            lines.append(text)
            lines.append("")

    lines.append("## Background processes")
    from JFI.models._util import utcnow

    for proc in background_processes:
        # started_at/exited_at come back from SQLite as naive datetimes
        # (see JFI.models._util's own docstring) -- utcnow() matches that,
        # unlike datetime.now(timezone.utc) which would raise subtracting
        # an aware value from a naive one.
        end = proc.exited_at or utcnow()
        elapsed = (end - proc.started_at).total_seconds()
        lines.append(f"- `{proc.handle}` ({proc.status}, {elapsed:.0f}s): `{proc.command}`")
    if not background_processes:
        lines.append("_none_")
    lines.append("")

    return "\n".join(lines) + "\n"


def _render_log_export(session_id: str, engine) -> str:
    """Reads LogEvent alone, in seq order -- it now carries every tag
    (SYSTEM/RULE/REASONING/ASSISTANT/USER/TOOL_CALL/TOOL_RESULT/ERROR),
    see pt_console_manager.py's own _log method, so there is nothing left
    to merge in from HistoryMessage (that table is the structured
    conversation state sent back to the model; this is the human-readable
    activity log, a separate concern that happens to overlap in content)."""
    from sqlmodel import select

    from JFI.models import LogEvent, get_session

    with get_session(engine) as db:
        events = list(
            db.exec(select(LogEvent).where(LogEvent.session_id == session_id).order_by(LogEvent.seq))
        )

    lines = [f"# {session_id} — log export", ""]
    for event in events:
        lines.append(f"[{event.created_at.strftime('%H:%M:%S')}] {event.tag.upper()}: {event.text}")
    if not events:
        lines.append("_No log recorded yet._")
    return "\n".join(lines) + "\n"


def _list_session_ids(engine) -> list[str]:
    from sqlmodel import select

    from JFI.models import SessionRecord, get_session

    with get_session(engine) as db:
        return sorted(db.exec(select(SessionRecord.session_id)))


def _export_one(project_root: Path, engine, session_id: str) -> tuple[Path, Path]:
    """Writes this session's two export files directly into the flat
    `.jfi/` folder, session_id-PREFIXED rather than in their own
    subfolder (`.jfi/<session_id>_plan_export.md`, not
    `.jfi/<session_id>/plan_export.md`) -- `.jfi/` holds no per-session
    subfolders at all now (see SimpleSessionManager.__init__), and this
    command routinely exports every session in one run, so a shared
    unprefixed filename would have each session's export silently
    overwrite the previous one's instead of leaving both behind."""
    jfi_dir = project_root / ".jfi"
    jfi_dir.mkdir(parents=True, exist_ok=True)

    plan_path = jfi_dir / f"{session_id}_plan_export.md"
    plan_path.write_text(_render_plan_export(session_id, engine))

    log_path = jfi_dir / f"{session_id}_log_export.txt"
    log_path.write_text(_render_log_export(session_id, engine))

    return plan_path, log_path


def _resolve_db(arg: str | None) -> tuple[Path, Path]:
    """Returns (project_root, db_path) -- project_root is what get_engine
    needs, db_path is just for the "no JFI.db found" error message.

    A literal db-file arg (`uv run export-db <path/to/x.db>`) lives at
    `<project_root>/.jfi/JFI.db`, so project_root is TWO parents up from
    it (`.jfi/`'s own parent), not one -- get_engine appends `.jfi/JFI.db`
    onto whatever project_root it's given, so passing `base.parent`
    (`.jfi/` itself) here would double that suffix into `.jfi/.jfi/JFI.db`.
    """
    base = Path(arg).resolve() if arg else Path.cwd()
    if base.is_file():
        return base.parent.parent, base
    db_path = base / ".jfi" / "JFI.db"
    if not db_path.exists():
        raise FileNotFoundError(f"No JFI.db found at {db_path}")
    return base, db_path


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--session=")]
    session_filter = next((a.split("=", 1)[1] for a in sys.argv[1:] if a.startswith("--session=")), None)
    arg = args[0] if args else None

    try:
        project_root, db_path = _resolve_db(arg)
    except FileNotFoundError as exc:
        print(f"export-db: {exc}", file=sys.stderr)
        raise SystemExit(1)

    from JFI.models import get_engine

    engine = get_engine(project_root)
    session_ids = _list_session_ids(engine)
    if session_filter:
        session_ids = [s for s in session_ids if s == session_filter]

    if not session_ids:
        print(f"export-db: no sessions found in {db_path}.", file=sys.stderr)
        raise SystemExit(1)

    for session_id in session_ids:
        plan_path, log_path = _export_one(project_root, engine, session_id)
        print(f"Exported {session_id} -> {plan_path}, {log_path}")


if __name__ == "__main__":
    main()
