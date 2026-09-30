"""Phase 1 of the v2 rewrite: the schema (laya_plan.md §13). No behaviour
change -- these pin that new projects get every v2 table/column, that OLD
project DBs are upgraded in place without losing rows, and that the column
upgrade now reaches MySQL/Postgres too (it used to be SQLite-only, so those
backends silently never got new columns)."""
from __future__ import annotations

import pytest
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from JFI.models import (
    DesignEntry, Directive, Episode, Leaf, LeafStatus, Phase, PlanEvent, PlannerVerdict, RunbookEntry,
    SessionRecord, get_engine, get_session,
)
from JFI.models import db as db_module

V2_TABLES = {"episode", "plannerverdict", "planevent", "runbookentry", "designentry", "directive"}


def _columns(engine, table):
    return {c["name"] for c in inspect(engine).get_columns(table)}


def test_fresh_db_has_every_v2_table_and_column(tmp_path):
    engine = get_engine(tmp_path)
    assert V2_TABLES <= set(inspect(engine).get_table_names())
    for table, added in db_module.ADDED_COLUMNS.items():
        assert set(added) <= _columns(engine, table), table


def _make_old_db(tmp_path):
    """A project DB as it looked before v2: today's schema minus every column
    added since, with rows written by the old code."""
    engine = get_engine(tmp_path)
    with engine.connect() as conn:
        for table, added in db_module.ADDED_COLUMNS.items():
            for (idx,) in conn.exec_driver_sql(f"SELECT name FROM sqlite_master WHERE type='index' "
                                               f"AND tbl_name='{table}'").fetchall():
                cols = {row[2] for row in conn.exec_driver_sql(f"PRAGMA index_info('{idx}')")}
                if cols & set(added):
                    conn.exec_driver_sql(f'DROP INDEX "{idx}"')
            for name in added:
                conn.exec_driver_sql(f'ALTER TABLE "{table}" DROP COLUMN "{name}"')
        conn.exec_driver_sql("INSERT INTO sessionrecord (session_id, repo_path, goal_text, iteration, queue_size, "
                             "is_paused, digest_block_count, created_at, updated_at) "
                             "VALUES ('old', '.', 'old goal', 2, 0, 0, 0, '2026-01-01', '2026-01-01')")
        conn.exec_driver_sql("INSERT INTO leaf (session_id, parent_id, phase, sort_key, description, status, "
                             "created_at) VALUES ('old', NULL, 'IMP', 10, 'an old leaf', 'DONE', '2026-01-01')")
        conn.commit()
    for table, added in db_module.ADDED_COLUMNS.items():
        assert not set(added) & _columns(engine, table), "fixture must really be the old schema"
    engine.dispose()


def test_old_sqlite_db_is_upgraded_in_place_and_old_rows_survive(tmp_path):
    _make_old_db(tmp_path)
    engine = get_engine(tmp_path)  # reopening runs the column upgrade
    for table, added in db_module.ADDED_COLUMNS.items():
        assert set(added) <= _columns(engine, table), table
    with get_session(engine) as db:
        record = db.get(SessionRecord, "old")
        leaf = db.exec(select(Leaf).where(Leaf.session_id == "old")).one()
    assert (record.goal_text, record.iteration, record.pipeline_version) == ("old goal", 2, "v1")
    assert (leaf.description, leaf.status, leaf.phase) == ("an old leaf", LeafStatus.DONE, Phase.IMP)
    assert (leaf.level, leaf.plan_status, leaf.redo_count, leaf.paused, leaf.files) == (None, None, 0, False, None)


def test_upgrade_is_idempotent(tmp_path):
    get_engine(tmp_path)
    engine = get_engine(tmp_path)  # second open: nothing left to add, must not fail
    assert "plan_status" in _columns(engine, "leaf")


@pytest.mark.parametrize("dialect_module", ["postgresql", "mysql"])
def test_column_upgrade_reaches_postgres_and_mysql(monkeypatch, dialect_module):
    """_ensure_columns used to return early on anything but SQLite. The
    statements are captured, not executed -- no server needed."""
    import importlib

    import sqlalchemy

    dialect = importlib.import_module(f"sqlalchemy.dialects.{dialect_module}").dialect()
    executed = []

    class FakeInspector:
        def has_table(self, table):
            return True

        def get_columns(self, table):
            return [{"name": "id"}, {"name": "description"}]

    class FakeConn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def exec_driver_sql(self, sql):
            executed.append(sql)

        def commit(self):
            executed.append("COMMIT")

    class FakeEngine:
        def __init__(self):
            self.dialect = dialect

        def connect(self):
            return FakeConn()

    monkeypatch.setattr(sqlalchemy, "inspect", lambda engine: FakeInspector())
    db_module._ensure_columns(FakeEngine(), "leaf", {"plan_status": "TEXT", "paused": "BOOLEAN DEFAULT FALSE"})
    assert executed[:2] == ["ALTER TABLE leaf ADD COLUMN plan_status TEXT",
                            "ALTER TABLE leaf ADD COLUMN paused BOOLEAN DEFAULT FALSE"]
    assert executed[-1] == "COMMIT"


def _seed_session(engine, session_id="s"):
    with get_session(engine) as db:
        db.add(SessionRecord(session_id=session_id, repo_path="."))
        db.commit()


def test_runbook_and_design_are_unique_per_session_and_key(tmp_path):
    engine = get_engine(tmp_path)
    _seed_session(engine)
    with get_session(engine) as db:
        db.add(RunbookEntry(session_id="s", name="run", command="uv run app"))
        db.add(DesignEntry(session_id="s", kind="contract", key="api->db", text="get_session()"))
        db.commit()
    for duplicate in (RunbookEntry(session_id="s", name="run", command="python app.py"),
                      DesignEntry(session_id="s", kind="contract", key="api->db", text="other")):
        with get_session(engine) as db, pytest.raises(IntegrityError):
            db.add(duplicate)
            db.commit()


def test_new_v1_sessions_default_to_pipeline_v1(tmp_path):
    engine = get_engine(tmp_path)
    _seed_session(engine, "fresh")
    with get_session(engine) as db:
        assert db.get(SessionRecord, "fresh").pipeline_version == "v1"


def test_export_db_includes_the_v2_tables(tmp_path):
    from export_db import _render_plan_export

    engine = get_engine(tmp_path)
    _seed_session(engine)
    with get_session(engine) as db:
        leaf = Leaf(session_id="s", phase=Phase.IMP, description="implement f() in app.py", level="task",
                    kind="implement", plan_status="GOOD", done_when="f(1) == 2", depends_on=[3])
        db.add(leaf)
        db.add(RunbookEntry(session_id="s", name="test", command="uv run pytest", verified=True))
        db.add(DesignEntry(session_id="s", kind="stack", key="stack", text="Python 3.12"))
        db.add(Episode(session_id="s", node_id=1, role="dev", mode="implement", turns=3, tokens=900,
                       end_reason="done"))
        db.add(PlannerVerdict(session_id="s", node_id=1, level="task", laya_verdict="GOOD",
                              answer_confidence=0.91, final_status="GOOD"))
        db.add(PlanEvent(session_id="s", node_id=1, type="redo", detail="vague"))
        db.add(Directive(session_id="s", node_id=1, text="use httpx"))
        db.commit()
    text = _render_plan_export("s", engine)
    for expected in ("pipeline: v1", "task · implement · GOOD · done when: f(1) == 2 · depends on [3]",
                     "## Runbook", "**test** (verified): `uv run pytest`", "## Design", "## Episodes (1)",
                     "## Planner verdicts (1)", "laya GOOD @ 0.91", "## Plan events (1)", "## Directives",
                     "use httpx (pending)"):
        assert expected in text, expected
