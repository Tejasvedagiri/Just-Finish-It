"""End-to-end dummy-session test for JFI.models -- no LLM, no JFI session,
no tmux, nothing external. Just: build a session directory's worth of DB
data by hand (the same shape a real session would produce), the way the
earlier design/smoke-testing pass for this branch did manually via ad-hoc
shell commands, and assert every table round-trips correctly through a
real SQLite file on disk.

Complements test_models.py's per-table/per-feature unit tests with ONE
holistic check: does a full dummy session -- every table populated at
once, like a real run would leave behind -- actually come back out intact
after the DB file is closed and reopened fresh. This is the test to run
first when in doubt about the DB layer; the granular tests in
test_models.py are for pinning down which specific piece broke.
"""

from datetime import timedelta

from sqlmodel import select

from JFI.models import (
    ActivityEvent,
    BackgroundProcess,
    ContextEntry,
    DonePhase,
    HistoryMessage,
    ImplementedFile,
    Leaf,
    LeafStatus,
    LogEvent,
    Phase,
    QueuedItem,
    SessionRecord,
    UnlockedTool,
    build_indexes,
    display_number,
    get_engine,
    get_session,
)
from JFI.models._util import utcnow


def _seed_dummy_session(engine, session_id: str) -> None:
    """Writes one row into every table, shaped like a real session
    mid-imp would actually leave behind -- a plan tree with a done leaf
    and an in-progress one, a couple of unlocked tools, a touched file, a
    context-cache fact, a queued follow-up, a running dev server, one
    completed phase, three conversation turns, and one activity event."""
    now = utcnow()

    with get_session(engine) as db:
        db.add(SessionRecord(
            session_id=session_id,
            repo_path="/tmp/dummy-repo",
            goal_text="Build a calculator CLI",
            task_type="python",
            phase=Phase.IMP,
            stage=None,
            state="streaming",
            iteration=1,
            queue_size=1,
            is_paused=False,
            current_task="1.2 Add evaluate()",
            current_task_started_at=now,
            tokens_used=4200,
            tokens_budget=15000,
            tokens_read=3000,
            tokens_written=1200,
            digest_summary="Set up the project skeleton",
            digest_block_count=2,
        ))
        db.add(UnlockedTool(session_id=session_id, tool_name="write_file"))
        db.add(UnlockedTool(session_id=session_id, tool_name="execute_command"))
        db.add(ImplementedFile(session_id=session_id, file_path="src/calculator.py"))
        db.add(ContextEntry(session_id=session_id, key="src/calculator.py", value="has OPERATORS dict, evaluate()"))
        db.add(QueuedItem(session_id=session_id, position=0, text="also handle division by zero"))
        db.add(BackgroundProcess(
            session_id=session_id, handle="bg1", command="python -m http.server",
            pid=9999, host="127.0.0.1", port=8000, status="running",
            started_at=now - timedelta(seconds=20),
        ))
        db.add(DonePhase(session_id=session_id, phase=Phase.PLANNER))
        db.add(DonePhase(session_id=session_id, phase=Phase.PRODUCT_OWNER))
        db.add(HistoryMessage(session_id=session_id, seq=1, role="user", content="My goal is: build a calculator CLI"))
        db.add(HistoryMessage(
            session_id=session_id, seq=3, role="assistant", content="Building the OPERATORS table.",
            tool_calls=[{"id": "call_1", "function": {"name": "write_file", "arguments": '{"file_path": "src/calculator.py"}'}}],
        ))
        db.add(LogEvent(session_id=session_id, seq=2, tag="rule", text="PHASE: IMPLEMENT"))
        db.add(LogEvent(session_id=session_id, seq=4, tag="assistant", text="Building the OPERATORS table."))
        db.add(ActivityEvent(session_id=session_id, severity="good", text="ticked 1/2"))
        db.commit()

        root = Leaf(session_id=session_id, parent_id=None, phase=Phase.IMP, sort_key=10, description="Core arithmetic")
        db.add(root)
        db.commit()
        db.refresh(root)

        db.add(Leaf(
            session_id=session_id, parent_id=root.id, phase=Phase.IMP, sort_key=10,
            description="Build OPERATORS dispatch table", status=LeafStatus.DONE,
            started_at=now - timedelta(seconds=90), ended_at=now - timedelta(seconds=30), tokens=850,
        ))
        db.add(Leaf(
            session_id=session_id, parent_id=root.id, phase=Phase.IMP, sort_key=20,
            description="Add evaluate()", status=LeafStatus.TODO,
        ))
        db.commit()


class TestDummySessionEndToEnd:
    def test_db_file_is_actually_created_on_disk(self, tmp_path):
        assert not (tmp_path / ".jfi" / "JFI.db").exists()

        get_engine(tmp_path)

        assert (tmp_path / ".jfi" / "JFI.db").exists()
        assert (tmp_path / ".jfi" / "JFI.db").stat().st_size > 0

    def test_every_table_round_trips_correctly_after_reopening_the_file(self, tmp_path):
        session_dir = tmp_path / "JFI" / "calc-session"
        session_id = "calc-session"

        write_engine = get_engine(session_dir)
        _seed_dummy_session(write_engine, session_id)

        # Fresh engine, same path -- proves this came off disk, not the
        # first engine's own in-process session cache.
        read_engine = get_engine(session_dir)
        with get_session(read_engine) as db:
            record = db.get(SessionRecord, session_id)
            unlocked_tools = list(db.exec(select(UnlockedTool).where(UnlockedTool.session_id == session_id)))
            implemented_files = list(db.exec(select(ImplementedFile).where(ImplementedFile.session_id == session_id)))
            context_entries = list(db.exec(select(ContextEntry).where(ContextEntry.session_id == session_id)))
            queued_items = list(db.exec(select(QueuedItem).where(QueuedItem.session_id == session_id)))
            background_processes = list(db.exec(select(BackgroundProcess).where(BackgroundProcess.session_id == session_id)))
            done_phases = list(db.exec(select(DonePhase).where(DonePhase.session_id == session_id)))
            messages = list(db.exec(select(HistoryMessage).where(HistoryMessage.session_id == session_id)))
            log_events = list(db.exec(select(LogEvent).where(LogEvent.session_id == session_id)))
            activity_events = list(db.exec(select(ActivityEvent).where(ActivityEvent.session_id == session_id)))
            leaves = list(db.exec(select(Leaf).where(Leaf.session_id == session_id)))

        # SessionRecord
        assert record is not None
        assert record.goal_text == "Build a calculator CLI"
        assert record.task_type == "python"
        assert record.phase == Phase.IMP
        assert record.tokens_used == 4200 and record.tokens_budget == 15000

        # UnlockedTool / ImplementedFile / ContextEntry
        assert {t.tool_name for t in unlocked_tools} == {"write_file", "execute_command"}
        assert [f.file_path for f in implemented_files] == ["src/calculator.py"]
        assert context_entries[0].value == "has OPERATORS dict, evaluate()"

        # QueuedItem / BackgroundProcess / DonePhase
        assert queued_items[0].text == "also handle division by zero"
        assert background_processes[0].status == "running"
        assert background_processes[0].port == 8000
        assert {p.phase for p in done_phases} == {Phase.PLANNER, Phase.PRODUCT_OWNER}

        # HistoryMessage / LogEvent (the history.jsonl.gz + run.log replacement)
        assert len(messages) == 2
        assistant_msg = next(m for m in messages if m.role == "assistant")
        assert assistant_msg.tool_calls[0]["function"]["name"] == "write_file"
        assert log_events[0].text == "PHASE: IMPLEMENT"

        # ActivityEvent
        assert activity_events[0].text == "ticked 1/2"

        # Leaf tree + display numbering
        assert len(leaves) == 3
        by_id, siblings = build_indexes(leaves)
        numbers = {leaf.description: display_number(leaf, by_id, siblings) for leaf in leaves}
        assert numbers == {
            "Core arithmetic": "1",
            "Build OPERATORS dispatch table": "1.1",
            "Add evaluate()": "1.2",
        }
        done_leaf = next(leaf for leaf in leaves if leaf.status == LeafStatus.DONE)
        assert done_leaf.tokens == 850
        assert (done_leaf.ended_at - done_leaf.started_at).total_seconds() == 60

    def test_export_db_renders_the_dummy_session_correctly(self, tmp_path):
        """The same dummy data, but through the actual `uv run export-db`
        code path rather than direct queries -- confirms the CLI command
        a human would run to eyeball a session works against this exact
        shape of data, not just the ORM layer in isolation."""
        from export_db import _export_one

        engine = get_engine(tmp_path)
        _seed_dummy_session(engine, "calc-session")

        plan_path, log_path = _export_one(tmp_path, engine, "calc-session")

        assert plan_path.exists() and log_path.exists()
        plan_text = plan_path.read_text()
        assert "Build a calculator CLI" in plan_text
        assert "[x] 1.1 Build OPERATORS dispatch table" in plan_text
        assert "[ ] 1.2 Add evaluate()" in plan_text
        assert "write_file, execute_command" in plan_text
        assert "src/calculator.py" in plan_text

        log_text = log_path.read_text()
        assert "PHASE: IMPLEMENT" in log_text
        assert "Building the OPERATORS table." in log_text
