"""JFI.models -- the DB-backed session layer that replaces everything under
JFI/<session>/ except .lock (see /todo.md's "SQLite/Pydantic persistence
rewrite" section, and JFI.models's own package docstring for the full
file-to-table map). Covers: the env-driven DB_BACKEND/DATABASE_URL
resolution, round-tripping every table through a real SQLite file
(including a second engine re-opening the same file, so it's a real
disk-persistence check, not just an in-memory session cache hit), the
per-phase display-numbering that replaces plan.md's hand-edited
dot-numbers, and the uniqueness constraints the tables that used to be
JSON-blob lists (unlocked_tools, implemented_files, done_phases) now get
from the schema instead of hand-rolled de-duplication.

`_isolate_cwd` (conftest.py, autouse) already chdirs every test into its
own tmp_path, so `get_engine(tmp_path / "JFI" / "demo")` below lands under
that tmp dir with no extra setup.
"""

import pytest
from sqlalchemy.exc import IntegrityError
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
    database_url,
    display_number,
    get_engine,
    get_session,
)


class TestDatabaseUrl:
    def test_sqlite_is_the_default_backend(self, monkeypatch, tmp_path):
        monkeypatch.delenv("DB_BACKEND", raising=False)
        url = database_url(tmp_path)
        assert url == f"sqlite:///{tmp_path / '.jfi' / 'JFI.db'}"

    def test_sqlite_creates_the_project_root(self, monkeypatch, tmp_path):
        project_root = tmp_path / "brand-new"
        assert not project_root.exists()
        database_url(project_root)
        assert project_root.exists()

    def test_one_db_file_is_shared_across_every_session_in_a_project(self, monkeypatch, tmp_path):
        """The single most load-bearing architecture requirement: ONE
        `.jfi/JFI.db` per PROJECT, not one per session -- every table is
        keyed by session_id, so two different sessions in the same project
        must resolve to the exact same DB file."""
        monkeypatch.delenv("DB_BACKEND", raising=False)
        assert database_url(tmp_path) == database_url(tmp_path)
        engine_a = get_engine(tmp_path)
        with get_session(engine_a) as db:
            db.add(SessionRecord(session_id="session-a", repo_path=str(tmp_path)))
            db.commit()

        engine_b = get_engine(tmp_path)
        with get_session(engine_b) as db:
            db.add(SessionRecord(session_id="session-b", repo_path=str(tmp_path)))
            db.commit()
            both = list(db.exec(select(SessionRecord)))
        assert {r.session_id for r in both} == {"session-a", "session-b"}
        assert (tmp_path / ".jfi" / "JFI.db").exists()

    def test_postgres_without_database_url_raises_a_clear_error(self, monkeypatch, tmp_path):
        monkeypatch.setenv("DB_BACKEND", "postgres")
        monkeypatch.delenv("DATABASE_URL", raising=False)
        with pytest.raises(ValueError, match="DATABASE_URL"):
            database_url(tmp_path)

    def test_mysql_with_database_url_passes_it_through(self, monkeypatch, tmp_path):
        monkeypatch.setenv("DB_BACKEND", "mysql")
        monkeypatch.setenv("DATABASE_URL", "mysql+pymysql://u:p@host/db")
        assert database_url(tmp_path) == "mysql+pymysql://u:p@host/db"

    def test_unknown_backend_raises_a_clear_error(self, monkeypatch, tmp_path):
        monkeypatch.setenv("DB_BACKEND", "mongodb")
        with pytest.raises(ValueError, match="Unknown DB_BACKEND"):
            database_url(tmp_path)


class TestRoundTrip:
    def test_leaf_and_session_record_persist_across_a_new_engine(self, tmp_path):
        """Writes through one engine, reads back through a SECOND engine
        pointed at the same path -- the real test that data is actually on
        disk, not just held in the first engine's own session cache."""
        session_dir = tmp_path / "JFI" / "demo"

        engine = get_engine(session_dir)
        with get_session(engine) as db:
            db.add(SessionRecord(session_id="demo", repo_path="/tmp/repo", goal_text="build a thing"))
            db.add(Leaf(session_id="demo", parent_id=None, phase=Phase.IMP, sort_key=10, description="Do the thing"))
            db.commit()

        reopened = get_engine(session_dir)
        with get_session(reopened) as db:
            record = db.get(SessionRecord, "demo")
            leaves = list(db.exec(select(Leaf).where(Leaf.session_id == "demo")))

        assert record is not None
        assert record.goal_text == "build a thing"
        assert len(leaves) == 1
        assert leaves[0].description == "Do the thing"
        assert leaves[0].status == LeafStatus.TODO

    def test_activity_event_persists(self, tmp_path):
        engine = get_engine(tmp_path / "JFI" / "demo")
        with get_session(engine) as db:
            db.add(SessionRecord(session_id="demo", repo_path="/tmp/repo"))
            db.add(ActivityEvent(session_id="demo", severity="good", text="ticked 3/5"))
            db.commit()

        with get_session(engine) as db:
            events = list(db.exec(select(ActivityEvent).where(ActivityEvent.session_id == "demo")))
        assert len(events) == 1
        assert events[0].text == "ticked 3/5"

    def test_session_record_awaiting_options_round_trips_as_json(self, tmp_path):
        """awaiting_options is the one list-shaped field SessionRecord kept
        as a JSON column rather than its own table (see the module
        docstring's rationale) -- confirm it still survives a real write
        and a fresh read, not just an in-memory object."""
        session_dir = tmp_path / "JFI" / "demo"
        engine = get_engine(session_dir)
        with get_session(engine) as db:
            db.add(SessionRecord(
                session_id="demo",
                repo_path="/tmp/repo",
                awaiting_prompt="Retry, or stop the run?",
                awaiting_options=[{"key": "r", "label": "Retry"}, {"key": "s", "label": "Stop"}],
            ))
            db.commit()

        reopened = get_engine(session_dir)
        with get_session(reopened) as db:
            record = db.get(SessionRecord, "demo")
        assert record.awaiting_prompt == "Retry, or stop the run?"
        assert record.awaiting_options == [{"key": "r", "label": "Retry"}, {"key": "s", "label": "Stop"}]

    def test_queued_item_and_unlocked_tool_round_trip(self, tmp_path):
        """The tables that replaced SessionRecord's old queued_items/
        unlocked_tools JSON blobs -- confirm they're independently
        queryable rows, not just list members."""
        session_dir = tmp_path / "JFI" / "demo"
        engine = get_engine(session_dir)
        with get_session(engine) as db:
            db.add(SessionRecord(session_id="demo", repo_path="/tmp/repo"))
            db.add(QueuedItem(session_id="demo", position=0, text="fix the thing"))
            db.add(QueuedItem(session_id="demo", position=1, text="add a test"))
            db.add(UnlockedTool(session_id="demo", tool_name="write_file"))
            db.commit()

        with get_session(engine) as db:
            queued = list(db.exec(select(QueuedItem).where(QueuedItem.session_id == "demo").order_by(QueuedItem.position)))
            unlocked = list(db.exec(select(UnlockedTool).where(UnlockedTool.session_id == "demo")))
        assert [q.text for q in queued] == ["fix the thing", "add a test"]
        assert unlocked[0].tool_name == "write_file"


class TestUniqueConstraints:
    """The tables that replaced a JSON-blob list get a real uniqueness
    guarantee from the schema instead of the caller having to de-dupe a
    Python list by hand -- confirm the DB actually enforces it."""

    def test_unlocked_tool_cannot_be_double_unlocked(self, tmp_path):
        engine = get_engine(tmp_path / "JFI" / "demo")
        with get_session(engine) as db:
            db.add(SessionRecord(session_id="demo", repo_path="/tmp/repo"))
            db.add(UnlockedTool(session_id="demo", tool_name="write_file"))
            db.commit()

        with get_session(engine) as db:
            db.add(UnlockedTool(session_id="demo", tool_name="write_file"))
            with pytest.raises(IntegrityError):
                db.commit()

    def test_implemented_file_cannot_be_recorded_twice(self, tmp_path):
        engine = get_engine(tmp_path / "JFI" / "demo")
        with get_session(engine) as db:
            db.add(SessionRecord(session_id="demo", repo_path="/tmp/repo"))
            db.add(ImplementedFile(session_id="demo", file_path="src/config.py"))
            db.commit()

        with get_session(engine) as db:
            db.add(ImplementedFile(session_id="demo", file_path="src/config.py"))
            with pytest.raises(IntegrityError):
                db.commit()

    def test_a_phase_cannot_be_marked_done_twice(self, tmp_path):
        engine = get_engine(tmp_path / "JFI" / "demo")
        with get_session(engine) as db:
            db.add(SessionRecord(session_id="demo", repo_path="/tmp/repo"))
            db.add(DonePhase(session_id="demo", phase=Phase.PLANNER))
            db.commit()

        with get_session(engine) as db:
            db.add(DonePhase(session_id="demo", phase=Phase.PLANNER))
            with pytest.raises(IntegrityError):
                db.commit()


class TestConversationTables:
    """HistoryMessage (replaces history.jsonl.gz) and LogEvent (replaces
    run.log's SYSTEM/RULE lines) -- see export_db's own merge-by-`seq`
    logic for how the two get reconstructed back into one interleaved view."""

    def test_history_message_round_trips_tool_calls_as_json(self, tmp_path):
        engine = get_engine(tmp_path / "JFI" / "demo")
        with get_session(engine) as db:
            db.add(SessionRecord(session_id="demo", repo_path="/tmp/repo"))
            db.add(HistoryMessage(
                session_id="demo",
                seq=1,
                role="assistant",
                tool_calls=[{"id": "call_1", "function": {"name": "write_file", "arguments": "{}"}}],
            ))
            db.commit()

        with get_session(engine) as db:
            message = db.exec(select(HistoryMessage).where(HistoryMessage.session_id == "demo")).first()
        assert message.tool_calls[0]["function"]["name"] == "write_file"

    def test_log_event_and_history_message_share_one_seq_space_for_merging(self, tmp_path):
        """export_db merge-sorts both tables by `seq` -- confirm two rows
        from DIFFERENT tables with interleaved seq values sort correctly
        against each other, not just within their own table."""
        engine = get_engine(tmp_path / "JFI" / "demo")
        with get_session(engine) as db:
            db.add(SessionRecord(session_id="demo", repo_path="/tmp/repo"))
            db.add(HistoryMessage(session_id="demo", seq=1, role="user", content="My goal is: build a thing"))
            db.add(LogEvent(session_id="demo", seq=2, tag="rule", text="PHASE: PLAN"))
            db.add(HistoryMessage(session_id="demo", seq=3, role="assistant", content="Starting."))
            db.commit()

        with get_session(engine) as db:
            messages = list(db.exec(select(HistoryMessage).where(HistoryMessage.session_id == "demo")))
            events = list(db.exec(select(LogEvent).where(LogEvent.session_id == "demo")))
        merged = sorted(
            [(m.seq, "message", m.content) for m in messages] + [(e.seq, "event", e.text) for e in events]
        )
        assert [kind for _, kind, _ in merged] == ["message", "event", "message"]


class TestBackgroundProcessAndActivityEvent:
    def test_background_process_round_trips(self, tmp_path):
        engine = get_engine(tmp_path / "JFI" / "demo")
        with get_session(engine) as db:
            db.add(SessionRecord(session_id="demo", repo_path="/tmp/repo"))
            db.add(BackgroundProcess(session_id="demo", handle="bg1", command="npm run dev", pid=1234, port=3000))
            db.commit()

        with get_session(engine) as db:
            proc = db.exec(select(BackgroundProcess).where(BackgroundProcess.session_id == "demo")).first()
        assert proc.status == "running"
        assert proc.exited_at is None

    def test_activity_event_round_trips(self, tmp_path):
        engine = get_engine(tmp_path / "JFI" / "demo")
        with get_session(engine) as db:
            db.add(SessionRecord(session_id="demo", repo_path="/tmp/repo"))
            db.add(ActivityEvent(session_id="demo", severity="good", text="ticked 3/5"))
            db.commit()

        with get_session(engine) as db:
            event = db.exec(select(ActivityEvent).where(ActivityEvent.session_id == "demo")).first()
        assert event.severity == "good"


class TestContextEntry:
    def test_context_entry_round_trips_and_enforces_unique_key_per_session(self, tmp_path):
        engine = get_engine(tmp_path / "JFI" / "demo")
        with get_session(engine) as db:
            db.add(SessionRecord(session_id="demo", repo_path="/tmp/repo"))
            db.add(ContextEntry(session_id="demo", key="config.py", value="has load_config()"))
            db.commit()

        with get_session(engine) as db:
            entry = db.exec(select(ContextEntry).where(ContextEntry.session_id == "demo")).first()
            assert entry.value == "has load_config()"

            db.add(ContextEntry(session_id="demo", key="config.py", value="a second, duplicate save"))
            with pytest.raises(IntegrityError):
                db.commit()


class TestDisplayNumbering:
    def _leaves(self, db, session_id="demo"):
        return list(db.exec(select(Leaf).where(Leaf.session_id == session_id)))

    def test_a_simple_tree_numbers_like_plan_md(self, tmp_path):
        engine = get_engine(tmp_path / "JFI" / "demo")
        with get_session(engine) as db:
            root = Leaf(session_id="demo", parent_id=None, phase=Phase.IMP, sort_key=10, description="Core")
            db.add(root)
            db.commit()
            db.refresh(root)

            child_a = Leaf(session_id="demo", parent_id=root.id, phase=Phase.IMP, sort_key=10, description="A")
            child_b = Leaf(session_id="demo", parent_id=root.id, phase=Phase.IMP, sort_key=20, description="B")
            db.add(child_a)
            db.add(child_b)
            db.commit()
            db.refresh(child_a)

            grandchild = Leaf(session_id="demo", parent_id=child_a.id, phase=Phase.IMP, sort_key=10, description="A.1")
            db.add(grandchild)
            db.commit()

            leaves = self._leaves(db)
            by_id, siblings = build_indexes(leaves)
            numbers = {leaf.description: display_number(leaf, by_id, siblings) for leaf in leaves}

        assert numbers == {"Core": "1", "A": "1.1", "B": "1.2", "A.1": "1.1.1"}

    def test_sort_key_order_wins_over_insertion_order(self, tmp_path):
        """A leaf inserted between two existing siblings via a gap sort_key
        (e.g. 15 between 10 and 20) must render in ITS gap position, not
        wherever it happened to be created -- the whole point of gap
        numbering over dot-strings (see Leaf's own docstring)."""
        engine = get_engine(tmp_path / "JFI" / "demo")
        with get_session(engine) as db:
            db.add(Leaf(session_id="demo", parent_id=None, phase=Phase.IMP, sort_key=10, description="First"))
            db.add(Leaf(session_id="demo", parent_id=None, phase=Phase.IMP, sort_key=20, description="Third"))
            db.commit()
            # Inserted last, but its sort_key (15) places it BETWEEN the two above.
            db.add(Leaf(session_id="demo", parent_id=None, phase=Phase.IMP, sort_key=15, description="Second"))
            db.commit()

            leaves = self._leaves(db)
            by_id, siblings = build_indexes(leaves)
            ordered = sorted(leaves, key=lambda leaf: leaf.sort_key)
            numbers = [display_number(leaf, by_id, siblings) for leaf in ordered]

        assert [leaf.description for leaf in ordered] == ["First", "Second", "Third"]
        assert numbers == ["1", "2", "3"]

    def test_implementation_and_testing_phases_number_separately(self, tmp_path):
        """Mirrors plan.md's own convention: "## Implementation" and
        "## Testing" were SEPARATELY-numbered trees, so both phases'
        first root leaf should independently be "1", not "1" and "2"."""
        engine = get_engine(tmp_path / "JFI" / "demo")
        with get_session(engine) as db:
            db.add(Leaf(session_id="demo", parent_id=None, phase=Phase.IMP, sort_key=10, description="Build it"))
            db.add(Leaf(session_id="demo", parent_id=None, phase=Phase.TESTING, sort_key=10, description="Test it"))
            db.commit()

            leaves = self._leaves(db)
            by_id, siblings = build_indexes(leaves)
            numbers = {leaf.description: display_number(leaf, by_id, siblings) for leaf in leaves}

        assert numbers == {"Build it": "1", "Test it": "1"}


class TestDoneLeafHasNoChildren:
    """A parent-with-children row is never meant to carry status/timing --
    see Leaf's module docstring. Not mechanically enforced by the schema
    (a caller COULD set both), but this documents the intended contract
    export_db and future tool-layer code should honor."""

    def test_a_leaf_with_children_can_still_be_queried_by_status(self, tmp_path):
        # No enforcement to test here yet -- this is a placeholder marking
        # the contract until the add_leaf/mark_leaf_done tool layer (see
        # /todo.md scope section 2) exists to actually enforce it.
        engine = get_engine(tmp_path / "JFI" / "demo")
        with get_session(engine) as db:
            root = Leaf(session_id="demo", parent_id=None, phase=Phase.IMP, sort_key=10, description="Core")
            db.add(root)
            db.commit()
            db.refresh(root)
            assert root.status == LeafStatus.TODO
