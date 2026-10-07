"""Phase 6 of the v2 rewrite: Dev, one short episode per leaf (laya_plan.md
§6, G6, G11, G13, D19, D27). Real SQLite, real files and real test runs --
the leaf's test really runs under pytest in a subprocess, since the gate's
whole point is that "done" means the test passed, not that the model said
so."""
from __future__ import annotations

import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from JFI import runner
from JFI.imp.dev import Imp, not_implemented_symbol
from JFI.imp.queue import next_leaf
from JFI.models import Episode, HistoryMessage, Leaf, SessionRecord, get_engine, get_session
from JFI.models._util import utcnow
from JFI.models.enums import LeafStatus
from JFI.planner.nodes import GOOD, add_node, load_nodes
from JFI.tool.checkpoint_tools import leaf_diff
from JFI.tool.code_tools import scaffold_file
from JFI.tool.note_tools import get_reviewer_notes
from JFI.tool.runbook_tools import runbook_set
from test.test_planner import LLM, Console, call, turn

PYTEST = f'"{sys.executable}" -m pytest -q -p no:cacheprovider'


@pytest.fixture
def engine(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # v1's write_file / replace_in_file work relative to cwd
    engine = get_engine(tmp_path / ".jfi")
    with get_session(engine) as db:
        db.add(SessionRecord(session_id="s", repo_path=str(tmp_path), pipeline_version="v2"))
        db.commit()
    runbook_set(engine, "s", "test_one", PYTEST + " {test_id}")
    return engine


def _node(engine, role, scope, description, **kw):
    text = add_node(engine, "s", role, scope, description, **kw)
    node_id = int(text.split("id=")[1].split()[0].rstrip("."))
    with get_session(engine) as db:
        row = db.get(Leaf, node_id)
        row.plan_status = GOOD
        db.add(row)
        db.commit()
    return node_id


def _imp(engine, root, turns, replan=None):
    console = Console(turns)
    imp = Imp(console, engine, "s", root, LLM(), dict(runner.TOOL_MAP), replan or (lambda: None))
    return imp, console


def _calc(engine, root):
    """Architect component -> Lead file (stubs on disk) -> Task leaves, the
    shape phase 5 produces."""
    scaffold_file(root, "calc/ops.py", "arithmetic", [
        {"signature": "def add(a: int, b: int) -> int:", "does": "sum"},
        {"signature": "def total(xs: list) -> int:", "does": "sum a list with add()"}])
    scaffold_file(root, "tests/test_ops.py", "tests for calc/ops.py")
    comp = _node(engine, "architect", None, "calc component")
    file_node = _node(engine, "lead", comp, "calc/ops.py", files=["calc/ops.py", "tests/test_ops.py"])
    return file_node


CHECK = f'"{sys.executable}" -c "print(1)"'


def _parents_check(*parents):
    """The turn each parent gets once everything under it is done (bottom-up)."""
    return [turn(call("mark_leaf_done", leaf_id=node, summary="part works", check=CHECK)) for node in parents]


ADD_IMPL = "def add(a: int, b: int) -> int:\n    return a + b\n"
TOTAL_IMPL = "def total(xs: list) -> int:\n    out = 0\n    for x in xs:\n        out = add(out, x)\n    return out\n"
TESTS = "from calc.ops import add, total\n\n\ndef test_add():\n    assert add(2, 3) == 5\n\n\n" \
        "def test_total():\n    assert total([1, 2, 3]) == 6\n"


def _leaf_episodes(engine):
    """Leaf episodes only: a finish-up episode also runs whenever stubs are left on disk."""
    with get_session(engine) as db:
        return sum(e.mode == "leaf" for e in db.exec(Episode.__table__.select()).all())


def _status(engine, leaf_id):
    with get_session(engine) as db:
        return db.get(Leaf, leaf_id)


def test_a_leaf_is_done_only_when_its_own_test_passes(engine, tmp_path):
    """The completion gate: mark_leaf_done runs the test itself. A failing
    test is refused with its output, a missing proof is refused, and the
    leaf is done only after the fix passes."""
    file_node = _calc(engine, tmp_path)
    leaf = _node(engine, "task", file_node, "implement add(a, b) in calc/ops.py: sum",
                 kind="implement", files=["calc/ops.py", "tests/test_ops.py"], done_when="add(2, 3) == 5")
    wrong = "def add(a: int, b: int) -> int:\n    return a - b\n"
    turns = [turn(call("replace_symbol", path="calc/ops.py", name="add", new_source=wrong),
                  call("write_file", file_path="tests/test_ops.py", content=TESTS.replace(", total", "")
                       .split("\n\n\ndef test_total")[0] + "\n")),
             turn(call("mark_leaf_done", leaf_id=leaf, summary="add")),
             turn(call("mark_leaf_done", leaf_id=leaf, summary="add", test_id="tests/test_ops.py::test_add")),
             turn(call("replace_symbol", path="calc/ops.py", name="add", new_source=ADD_IMPL),
                  call("mark_leaf_done", leaf_id=leaf, summary="add", test_id="tests/test_ops.py::test_add"))]
    turns += _parents_check(file_node, file_node - 1)
    imp, console = _imp(engine, tmp_path, turns)
    result = imp.run()

    assert result.complete
    with get_session(engine) as db:
        modes = [e.mode for e in db.exec(Episode.__table__.select()).all()]
    # One episode for the leaf, then its file and its component are checked
    # (bottom-up); total()'s stub is left for the finish-up.
    assert modes == ["leaf", "check", "check", "finish-up"]
    assert _status(engine, leaf).status == LeafStatus.DONE
    assert "return a + b" in (tmp_path / "calc/ops.py").read_text()
    if shutil.which("git"):  # the passing leaf is checkpointed, so the reviewer can see its own diff
        diff = leaf_diff(engine, _status(engine, leaf).session_id, tmp_path, leaf)
        assert "+    return a + b" in diff and "tests/test_ops.py" in diff
    with get_session(engine) as db:
        tool_results = [m.content for m in db.exec(HistoryMessage.__table__.select()).all()
                        if m.role == "tool" and m.content.startswith("Error")]
    assert any("pass test_id" in t for t in tool_results)
    assert any("failed (exit" in t and "assert -1 == 5" in t for t in tool_results)


def test_a_finished_leaf_reports_its_numbered_title_tokens_and_time(engine, tmp_path):
    """After the v2 cutover nothing called record_task_tokens and Dev's
    status task was the bare description: the fleet's checklist, which finds
    the current leaf and files its cost by the leading number, never marked
    one current and its tasks-vs-tokens heatmap stayed empty."""
    file_node = _calc(engine, tmp_path)
    leaf = _node(engine, "task", file_node, "implement add(a, b) in calc/ops.py: sum",
                 kind="implement", files=["calc/ops.py", "tests/test_ops.py"], done_when="add(2, 3) == 5")
    tests = TESTS.replace(", total", "").split("\n\n\ndef test_total")[0] + "\n"
    turns = [turn(call("replace_symbol", path="calc/ops.py", name="add", new_source=ADD_IMPL),
                  call("write_file", file_path="tests/test_ops.py", content=tests),
                  call("mark_leaf_done", leaf_id=leaf, summary="add", test_id="tests/test_ops.py::test_add"))]
    imp, console = _imp(engine, tmp_path, turns)
    statuses, recorded = [], []
    console.set_status = lambda **k: statuses.append(k)
    console.record_task_tokens = lambda *a, **k: recorded.append((a, k))
    assert imp.run().complete

    title = "1.1.1 implement add(a, b) in calc/ops.py: sum"
    assert any(s.get("task") == title and s.get("task_started_at") for s in statuses)
    (args, kwargs), = recorded
    assert args[0] == title and kwargs == {"phase": "imp"}
    assert args[1] == _status(engine, leaf).tokens > 0
    assert args[2] <= args[3]


def test_a_test_hitting_another_stub_requeues_instead_of_fixing(engine, tmp_path):
    """G6: total()'s test reaches add(), which is still a stub. That's a
    missing dependency, not a bug: the leaf is deferred behind add()'s leaf,
    and the wait doesn't count as a failed attempt."""
    file_node = _calc(engine, tmp_path)
    total = _node(engine, "task", file_node, "implement total(xs) in calc/ops.py: sum with add()",
                  kind="implement", files=["calc/ops.py", "tests/test_ops.py"], done_when="total([1,2,3]) == 6")
    add = _node(engine, "task", file_node, "implement add(a, b) in calc/ops.py: sum", kind="implement",
                files=["calc/ops.py", "tests/test_ops.py"], done_when="add(2, 3) == 5")
    turns = [turn(call("replace_symbol", path="calc/ops.py", name="total", new_source=TOTAL_IMPL),
                  call("write_file", file_path="tests/test_ops.py", content=TESTS),
                  call("mark_leaf_done", leaf_id=total, summary="t", test_id="tests/test_ops.py::test_total")),
             turn(call("replace_symbol", path="calc/ops.py", name="add", new_source=ADD_IMPL),
                  call("mark_leaf_done", leaf_id=add, summary="a", test_id="tests/test_ops.py::test_add")),
             turn(call("mark_leaf_done", leaf_id=total, summary="t", test_id="tests/test_ops.py::test_total"))]
    imp, _ = _imp(engine, tmp_path, turns)
    result = imp.run()

    assert result.complete and _leaf_episodes(engine) == 3
    assert _status(engine, total).status == LeafStatus.DONE and _status(engine, total).attempt_count == 1
    assert _status(engine, add).status == LeafStatus.DONE


def test_not_implemented_symbol_reads_the_raising_frame():
    output = ('  File "calc/ops.py", line 9, in total\n    out = add(out, x)\n'
              '  File "calc/ops.py", line 3, in add\n    raise NotImplementedError\nE   NotImplementedError\n')
    assert not_implemented_symbol(output) == "add"
    assert not_implemented_symbol("AssertionError: 1 != 2") is None


def test_queue_waits_for_leaves_under_nodes_an_ancestor_depends_on(engine):
    """G6: Task only orders leaves inside one file, so a leaf in api/ must
    still wait for db/'s leaves when the Architect said api depends on db."""
    db_comp = _node(engine, "architect", None, "db component")
    api_comp = _node(engine, "architect", None, "api component", depends_on=[db_comp])
    with get_session(engine) as db:  # api first in tree order, so only depends_on can put db first
        for node_id, key in ((api_comp, 10), (db_comp, 20)):
            row = db.get(Leaf, node_id)
            row.sort_key = key
            db.add(row)
        db.commit()
    api_file = _node(engine, "lead", api_comp, "api/routes.py")
    db_file = _node(engine, "lead", db_comp, "db/session.py")
    api_leaf = _node(engine, "task", api_file, "implement list_todos()")
    db_leaf = _node(engine, "task", db_file, "implement get_session()")

    # Bottom-up: db's leaf, then its file, then the db component, then api.
    for expected in (db_leaf, db_file, db_comp, api_leaf, api_file, api_comp):
        node = next_leaf(load_nodes(engine, "s"), {})
        assert node.id == expected
        with get_session(engine) as db:
            row = db.get(Leaf, node.id)
            row.status = LeafStatus.DONE
            db.add(row)
            db.commit()
    assert next_leaf(load_nodes(engine, "s"), {}) is None


def _started(engine, leaf, attempts):
    with get_session(engine) as db:
        row = db.get(Leaf, leaf)
        row.attempt_count, row.started_at = attempts, utcnow()
        db.add(row)
        db.commit()


def test_restart_briefs_a_partial_attempt(engine, tmp_path, monkeypatch):
    """G13: a leaf started before (a crash, a stop) restarts as a fresh
    episode told its files may be half-edited. One that then can't be
    finished or split is skipped with a reviewer note -- every leaf still
    reaches a finished state (D27)."""
    monkeypatch.setenv("MAX_EPISODE_TURNS", "1")
    file_node = _calc(engine, tmp_path)
    leaf = _node(engine, "task", file_node, "implement add(a, b) in calc/ops.py: sum", kind="implement")
    _started(engine, leaf, 1)
    imp, _ = _imp(engine, tmp_path, [turn(call("read_symbol", path="calc/ops.py", name="add"))])
    result = imp.run()

    with get_session(engine) as db:
        messages = db.exec(HistoryMessage.__table__.select()).all()
    assert any("previous attempt" in (m.content or "") for m in messages if m.role == "system")
    assert _status(engine, leaf).status == LeafStatus.SKIPPED
    assert "couldn't be finished or split" in get_reviewer_notes(engine, "s")
    assert result.complete


def test_attempt_cap_splits_without_another_episode(engine, tmp_path, monkeypatch):
    monkeypatch.setenv("MAX_DEV_ATTEMPTS", "2")
    file_node = _calc(engine, tmp_path)
    leaf = _node(engine, "task", file_node, "implement add(a, b) in calc/ops.py: sum", kind="implement")
    _started(engine, leaf, 2)
    splits = []
    imp, _ = _imp(engine, tmp_path, [], replan=lambda: splits.append(1))
    imp.run()
    assert splits == [1] and _leaf_episodes(engine) == 0


def test_overflow_sends_the_leaf_to_task_and_dev_continues_with_the_pieces(engine, tmp_path, monkeypatch):
    """D19: a Dev episode that runs out of budget proves the leaf too big;
    Task splits it and Dev works the pieces."""
    file_node = _calc(engine, tmp_path)
    big = _node(engine, "task", file_node, "implement add(a, b) in calc/ops.py: sum", kind="implement")

    def replan():
        _node(engine, "task", big, "implement add(a, b) in calc/ops.py: the actual sum", kind="implement")
        with get_session(engine) as db:  # what the planner does once the split has its pieces
            db.get(Leaf, big).plan_status = GOOD
            db.commit()

    turns = [turn(call("read_symbol", path="calc/ops.py", name="add"))] * 6 + [  # 3 for the leaf, 3 for its wrap-up
        turn(call("replace_symbol", path="calc/ops.py", name="add", new_source=ADD_IMPL),
             call("mark_leaf_done", leaf_id=big + 1, summary="a", check=CHECK))]
    # Split, the leaf is a parent: its own check comes after its piece.
    turns += _parents_check(big, file_node, file_node - 1)
    monkeypatch.setenv("MAX_EPISODE_TURNS", "3")
    imp, _ = _imp(engine, tmp_path, turns, replan=replan)
    result = imp.run()

    assert result.complete
    with get_session(engine) as db:
        episodes = [e for e in db.exec(Episode.__table__.select()).all() if e.mode in ("leaf", "wrap-up")]
    assert [(e.mode, e.end_reason) for e in episodes] == [("leaf", "turn_cap"), ("wrap-up", "turn_cap"),
                                                          ("leaf", "finish")]
    assert _status(engine, big + 1).status == LeafStatus.DONE
    assert _status(engine, big).status == LeafStatus.DONE and _status(engine, big).ended_at >= \
        _status(engine, big + 1).ended_at


def test_a_command_or_path_as_test_id_is_refused_with_the_right_id(engine, tmp_path):
    """Observed on the QA machine: Dev passed test_id="npx vitest run
    __tests__/loader.test.js"; the gate put it into test_one and ran
    "npx vitest run __tests__/npx vitest run __tests__/loader.test.js.test.js"."""
    runbook_set(engine, "s", "test_one", "npx vitest run __tests__/{test_id}.test.js", notes="e.g. loader")
    file_node = _calc(engine, tmp_path)
    leaf = _node(engine, "task", file_node, "implement add(a, b) in calc/ops.py: sum", kind="implement")
    imp, _ = _imp(engine, tmp_path, [])
    gate = imp._mark_leaf_done(_status(engine, leaf))

    for wrong in ("npx vitest run __tests__/loader.test.js", "__tests__/loader.test.js"):
        answer = gate(leaf, "s", test_id=wrong)
        assert answer.startswith("Error: test_id is only the id") and "try test_id='loader'" in answer
    assert _status(engine, leaf).status != LeafStatus.DONE


def test_a_finished_leaf_that_ran_out_of_turns_is_marked_done_not_split(engine, tmp_path, monkeypatch):
    """Observed on the calc run (node 8): the leaf's test passed on turn 13,
    Dev spent turns 14-15 on extra checks and the episode ended on the cap
    before mark_leaf_done -- so the finished leaf was re-split (~7 minutes).
    Now a short wrap-up episode checks first."""
    file_node = _calc(engine, tmp_path)
    leaf = _node(engine, "task", file_node, "implement add(a, b) in calc/ops.py: sum", kind="implement")
    tests = TESTS.replace(", total", "").split("\n\n\ndef test_total")[0] + "\n"
    turns = [turn(call("replace_symbol", path="calc/ops.py", name="add", new_source=ADD_IMPL)),
             turn(call("write_file", file_path="tests/test_ops.py", content=tests)),
             turn(call("read_file", path="calc/ops.py")),  # extra checks until the cap
             turn(call("mark_leaf_done", leaf_id=leaf, summary="done", test_id="tests/test_ops.py::test_add"))]
    monkeypatch.setenv("MAX_EPISODE_TURNS", "3")
    splits = []
    imp, _ = _imp(engine, tmp_path, turns, replan=lambda: splits.append(1))
    imp.run()

    assert _status(engine, leaf).status == LeafStatus.DONE
    assert splits == []


def test_setup_is_fixed_first_and_the_finish_up_reports_leftover_markers(engine, tmp_path):
    """§6: setup runs (and gets fixed) before any leaf; after the last leaf
    the build runs and leftover JFI: markers are planned work nobody did."""
    fail = f'"{sys.executable}" -c "import sys; sys.exit(3)"'
    ok = f'"{sys.executable}" -c "print(1)"'
    runbook_set(engine, "s", "setup", fail)
    runbook_set(engine, "s", "build", ok)
    scaffold_file(tmp_path, "calc/ops.py", "arithmetic", [{"signature": "def add(a, b):", "does": "sum"}])

    turns = [turn(call("runbook_set", name="setup", command=ok), call("mark_leaf_done", leaf_id=0, summary="s")),
             turn(call("mark_leaf_done", leaf_id=0, summary="left them"))]
    imp, _ = _imp(engine, tmp_path, turns)
    imp.run()

    assert "setup ✓" in runner_index(engine) and "build ✓" in runner_index(engine)
    assert "unfinished markers after imp: calc/ops.py" in get_reviewer_notes(engine, "s")


def runner_index(engine):
    from JFI.tool.runbook_tools import runbook_index
    return runbook_index(engine, "s")


def test_run_phase_sends_v2_sessions_to_the_new_imp(engine, tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "make_llm_stream", lambda prefixes: LLM())
    file_node = _calc(engine, tmp_path)
    leaf = _node(engine, "task", file_node, "implement add(a, b) in calc/ops.py: sum", kind="implement")
    added = []
    ssm = SimpleNamespace(db_engine=engine, session_id="s", session_path=Path(tmp_path) / ".jfi", history=[],
                          add_message=lambda role, content: added.append((role, content)))
    console = Console([turn(call("replace_symbol", path="calc/ops.py", name="add", new_source=ADD_IMPL),
                            call("write_file", file_path="tests/test_ops.py",
                                 content=TESTS.split("\n\n\ndef test_total")[0].replace(", total", "") + "\n"),
                            call("mark_leaf_done", leaf_id=leaf, summary="a", test_id="tests/test_ops.py"))])

    monkeypatch.setenv("MAX_EPISODE_TURNS", "1")  # the finish-up episode for total()'s leftover stub

    assert runner.run_phase(console, {}, ssm, "imp")
    assert _status(engine, leaf).status == LeafStatus.DONE
    assert added == [("assistant", "IMP_COMPLETE")] and console.done_phases == ["imp"]


def test_setup_fix_is_checked_against_the_corrected_command(engine, tmp_path):
    """Observed on the first real v2 run: Dev fixed the runbook's setup
    (python3 -> python) on its first try, but the gate kept re-running the
    command captured when the episode started, until the turn cap."""
    fail = f'"{sys.executable}" -c "import sys; sys.exit(9)"'
    ok = f'"{sys.executable}" -c "print(1)"'
    runbook_set(engine, "s", "setup", fail)
    turns = [turn(call("runbook_set", name="setup", command=ok), call("mark_leaf_done", leaf_id=0, summary="s"))]
    imp, _ = _imp(engine, tmp_path, turns)
    imp.run()
    with get_session(engine) as db:
        episodes = db.exec(Episode.__table__.select()).all()
    assert [(e.mode, e.end_reason, e.turns) for e in episodes] == [("setup", "finish", 1)]


def test_dev_works_bottom_up_and_a_reopened_node_reopens_the_parts_above_it(engine):
    """The user, after the first real run: "If we have 1, 1.1, 1.1.1, 1.1.2,
    1.1.3: first impl 1.1.1, 1.1.2, 1.1.3, then 1.1, then 1." Every node is
    Dev's, parents last; reopening a leaf (review, changed evidence) or adding
    a part under a finished parent sends the parents above it back too."""
    from JFI.planner.nodes import reopen_with_ancestors

    comp = _node(engine, "architect", None, "1")
    file_node = _node(engine, "lead", comp, "1.1")
    leaves = [_node(engine, "task", file_node, f"1.1.{n}") for n in (1, 2, 3)]
    order = []
    while (node := next_leaf(load_nodes(engine, "s"), {})) is not None:
        order.append(node.id)
        with get_session(engine) as db:
            db.get(Leaf, node.id).status = LeafStatus.DONE
            db.commit()
    assert order == leaves + [file_node, comp]

    assert reopen_with_ancestors(engine, "s", leaves[1]) == [file_node, comp]
    assert next_leaf(load_nodes(engine, "s"), {}).id == file_node  # the leaf itself is the reviewer's to reopen

    with get_session(engine) as db:
        for node_id in (file_node, comp):
            db.get(Leaf, node_id).status = LeafStatus.DONE
        db.commit()
    new = _node(engine, "task", file_node, "1.1.4")
    assert [_status(engine, n).status for n in (new, file_node, comp)] == [LeafStatus.TODO] * 3
