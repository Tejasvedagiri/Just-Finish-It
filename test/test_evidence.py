"""Ground-truth evidence (docs/old_new.md): the Lead captures what the build
must match into evidences/, Task adds a compare leaf per case, and Dev's
compare leaf is done only when the new code's output matches the evidence.
Real SQLite, real files and real commands throughout -- the ground truth is
always run, never typed, which is the point of the feature."""
from __future__ import annotations

import shutil
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlmodel import select

from JFI import runner
from JFI.episode.brief import ScopeAnchor
from JFI.imp.dev import Imp
from JFI.models import HistoryMessage, Leaf, PlanEvent, RunbookEntry, SessionRecord, get_engine, get_session
from JFI.models.enums import LeafStatus
from JFI.planner.judge import FallbackJudge
from JFI.planner.loop import Planner, ground_truth_hint
from JFI.planner.nodes import GOOD, add_node, load_nodes, update_node
from JFI.review import Reviewer
from JFI.tool.design_tools import design_set
from JFI.tool.evidence_tools import (
    LLM_SOURCE, capture_evidence, compare_case, evidence_hash, mark_reviewed, outputs_match, read_evidence,
    recapture, save_edited_text,
)
from JFI.tool.runbook_tools import runbook_set
from test.test_planner import LLM, Console, call, turn

PY = f'"{sys.executable}"'
# The ground truth in most tests: a tiny calculator that is right. It stands
# in for bc so the tests run anywhere; test_bc_itself uses the real bc.
ORACLE = ("import sys\n"
          "a, op, b = open(sys.argv[1]).read().split()\n"
          "a, b = float(a), float(b)\n"
          "if op == '/' and b == 0:\n"
          "    sys.exit('Runtime error: Divide by zero')\n"
          "r = {'+': a + b, '-': a - b, '*': a * b, '/': a / b if b else 0}[op]\n"
          "print(int(r) if r == int(r) else r)\n")
# The new code under test: a REPL like the calc benchmark's main.py, which adds wrongly.
WRONG_CALC = ("import sys\n"
              "for line in sys.stdin:\n"
              "    line = line.strip()\n"
              "    if line in ('exit', 'quit'):\n"
              "        break\n"
              "    a, op, b = line.split()\n"
              "    a, b = float(a), float(b)\n"
              "    if op == '/' and b == 0:\n"
              "        print('Error: division by zero')\n"
              "        continue\n"
              "    print(a + b + (0.5 if op == '+' else 0) if op == '+' else a - b)\n")
RIGHT_CALC = WRONG_CALC.replace(" + (0.5 if op == '+' else 0)", "")


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # write_file / replace_in_file work relative to cwd
    engine = get_engine(tmp_path / ".jfi")
    with get_session(engine) as db:
        db.add(SessionRecord(session_id="s", repo_path=str(tmp_path), pipeline_version="v2"))
        db.commit()
    (tmp_path / "oracle.py").write_text(ORACLE, encoding="utf-8")
    (tmp_path / "main.py").write_text(WRONG_CALC, encoding="utf-8")
    runbook_set(engine, "s", "evidence_one", f"{PY} oracle.py {{input_file}}")
    runbook_set(engine, "s", "compare_one", f"{{ cat {{input_file}}; echo exit; }} | {PY} main.py")
    return engine, tmp_path


def _good(engine, text):
    node_id = int(text.split("id=")[1].split()[0].rstrip("."))
    with get_session(engine) as db:
        row = db.get(Leaf, node_id)
        row.plan_status = GOOD
        db.add(row)
        db.commit()
    return node_id


# ------------------------------------------------------------------ capture

def test_the_ground_truth_is_run_never_typed(project):
    engine, root = project
    result = capture_evidence(engine, "s", root, "lead", "add", ["1 + 1", "2.5 + 0.25", "-3 + 10"])

    assert result.startswith("Saved evidences/add.txt")
    evidence = read_evidence(root, "add")
    assert [(i.input, i.output, i.error) for i in evidence.items] == [
        ("1 + 1", "2", False), ("2.5 + 0.25", "2.75", False), ("-3 + 10", "7", False)]
    assert evidence.source.startswith("command ") and not evidence.unverified
    text = (root / "evidences" / "add.txt").read_text()
    assert ">>> 1 + 1\n2\n" in text and "# how: " in text


def test_the_ground_truth_failing_on_an_input_is_evidence_too(project):
    """bc answers 1 / 0 with "Divide by zero" on stderr; that's what the new
    code must also treat as an error, in its own words."""
    engine, root = project
    capture_evidence(engine, "s", root, "lead", "divide_by_zero", ["1 / 0"])
    [item] = read_evidence(root, "divide_by_zero").items
    assert item.error and "Divide by zero" in item.output
    assert compare_case(engine, "s", root, "divide_by_zero").ok  # "Error: division by zero" counts


@pytest.mark.skipif(shutil.which("bc") is None, reason="bc isn't installed")
def test_bc_itself_and_the_dash_trap(project):
    """Found while building this: `bc -l <<< ...` under /bin/sh (dash on
    Debian/Ubuntu) failed with "redirection unexpected" on every input, and
    that shell error was saved as bc's answer. A command that didn't run is
    refused and nothing is saved."""
    engine, root = project
    runbook_set(engine, "s", "evidence_one", "bc -l <<< \"scale=10; $(cat {input_file})\"")
    if Path("/bin/sh").resolve().name in ("dash", "busybox"):
        refused = capture_evidence(engine, "s", root, "lead", "add", ["1 + 1"])
        assert refused.startswith("Error: evidence_one didn't run") and "<<<" in refused
        assert read_evidence(root, "add") is None
    runbook_set(engine, "s", "evidence_one", "{ echo scale=10; cat {input_file}; } | bc -l")
    capture_evidence(engine, "s", root, "lead", "add", ["1 + 1", "2.5 + 0.25"])
    assert [i.output for i in read_evidence(root, "add").items] == ["2", "2.75"]


def test_a_command_that_isnt_there_is_refused(project):
    engine, root = project
    runbook_set(engine, "s", "evidence_one", "./no-such-oracle {input_file}")
    assert capture_evidence(engine, "s", root, "lead", "add", ["1 + 1"]).startswith("Error: evidence_one didn't run")
    assert read_evidence(root, "add") is None


def test_answers_from_the_model_are_the_last_resort_and_flagged(project):
    engine, root = project
    with get_session(engine) as db:
        db.delete(db.exec(runner_select_runbook("evidence_one")).first())
        db.commit()
    refused = capture_evidence(engine, "s", root, "lead", "add", ["1 + 1"])
    assert refused.startswith("Error: there's no way to get the ground truth's answers")

    saved = capture_evidence(engine, "s", root, "lead", "add", ["1 + 1"], answers=["2"])
    assert "NOT VERIFIED" in saved
    assert read_evidence(root, "add").source == LLM_SOURCE and read_evidence(root, "add").unverified

    (root / "docs").mkdir()
    (root / "docs" / "api.md").write_text("1 + 1 is 2\n")
    capture_evidence(engine, "s", root, "lead", "add", ["1 + 1"], answers=["2"], from_file="docs/api.md L1-1")
    assert read_evidence(root, "add").source == "file docs/api.md L1-1"


def runner_select_runbook(name):
    return select(RunbookEntry).where(RunbookEntry.session_id == "s", RunbookEntry.name == name)


# ------------------------------------------------------------------ compare

def test_numbers_compare_by_value_and_separators_dont_count():
    """bc prints 4 and 3.50000000000000000000 where Python prints 4.0 and 3.5;
    mysql -N separates columns with tabs where psql -At uses |."""
    assert outputs_match("tokens", "4", "4.0")
    assert outputs_match("tokens", "3.50000000000000000000", "3.5")
    assert outputs_match("tokens", "2026-01\t1204.50", "2026-01|1204.5")
    assert not outputs_match("tokens", "2.75", "2.7")
    assert not outputs_match("tokens", "Holdings", "holdings")
    assert outputs_match("contains", "active 12408", "rows: 1, active=12408 (ok)")
    assert not outputs_match("exact", "4", "4.0")


def test_a_wrong_answer_is_a_mismatch_with_both_sides_shown(project):
    engine, root = project
    capture_evidence(engine, "s", root, "lead", "add", ["1 + 1", "-3 + 10"])
    result = compare_case(engine, "s", root, "add")
    assert not result.ok
    assert "new: 2.5" in result.report and "evidence: 2" in result.report and "MISMATCH" in result.report
    (root / "main.py").write_text(RIGHT_CALC)
    assert compare_case(engine, "s", root, "add").ok


def test_a_data_migration_is_checked_with_a_saved_query(project):
    """The old database is the ground truth: the query is saved in
    evidences/, its result on the old database captured, and the same query
    run on the new one must give the same result."""
    engine, root = project
    for name in ("old.db", "new.db"):
        with sqlite3.connect(root / name) as db:
            db.execute("CREATE TABLE customers (id INTEGER, status TEXT)")
            db.executemany("INSERT INTO customers VALUES (?, ?)", [(1, "active"), (2, "active"), (3, "gone")])
    (root / "query.py").write_text("import sqlite3, sys\n"
                                   "rows = sqlite3.connect(sys.argv[1]).execute(open(sys.argv[2]).read()).fetchall()\n"
                                   "print('\\n'.join(' | '.join(str(c) for c in r) for r in rows))\n")
    runbook_set(engine, "s", "evidence_one", f"{PY} query.py old.db {{input_file}}")
    runbook_set(engine, "s", "compare_one", f"{PY} query.py new.db {{input_file}}")
    sql = "SELECT status, COUNT(*) FROM customers GROUP BY status ORDER BY status;"
    capture_evidence(engine, "s", root, "lead", "customers_by_status", sql=sql)

    assert (root / "evidences" / "customers_by_status.sql").read_text().strip() == sql
    assert compare_case(engine, "s", root, "customers_by_status").ok
    with sqlite3.connect(root / "new.db") as db:
        db.execute("DELETE FROM customers WHERE id = 2")  # a row lost in the migration
    result = compare_case(engine, "s", root, "customers_by_status")
    assert not result.ok and "MISMATCH" in result.report


def test_reviewing_doesnt_count_as_a_change_but_editing_and_recapturing_do(project):
    engine, root = project
    capture_evidence(engine, "s", root, "lead", "add", ["1 + 1"])
    before = evidence_hash(root, ["add"])
    mark_reviewed(root, "add")
    assert evidence_hash(root, ["add"]) == before and read_evidence(root, "add").header["reviewed"]

    text = (root / "evidences" / "add.txt").read_text().replace(">>> 1 + 1\n2", ">>> 1 + 1\n2\n>>> 2 + 2\n4")
    assert save_edited_text(root, "add", text) == "Saved add.txt."
    edited = read_evidence(root, "add")
    assert edited.source == "user" and edited.header["was"].startswith("command") and len(edited.items) == 2
    assert evidence_hash(root, ["add"]) != before

    (root / "evidences" / "add.txt").write_text(text)  # a hand edit that kept the command header
    assert recapture(engine, "s", root, "add").startswith("Re-captured add")
    assert [i.output for i in read_evidence(root, "add").items] == ["2", "4"]


# ------------------------------------------------------------------ the planner

def test_a_goal_with_a_ground_truth_needs_a_reference_from_the_architect(project):
    engine, root = project
    planner = Planner(Console([]), engine, "s", "Build a CLI calculator. Compare it with bc.", root,
                      lambda role: LLM(), FallbackJudge())
    [problem] = planner._reference_problems([], {})
    assert 'the goal says "Compare it with"' in problem and 'design_set("reference"' in problem

    design_set(engine, "s", "reference", "bc", "behavioural: bc -l; may differ: 4 vs 4.0")
    comp = add_node(engine, "s", "architect", None, "evaluator in calc/: + - * /", done_when="evaluates",
                    references=["reference:bc"])
    assert comp.startswith("Added")
    runbook = {"evidence_one": "x {input}", "compare_one": "y {input_file}"}
    assert planner._reference_problems(load_nodes(engine, "s"), runbook) == []
    assert any("compare_one" in p for p in planner._reference_problems(load_nodes(engine, "s"),
                                                                        {"evidence_one": "x {input}"}))

    plain = Planner(Console([]), engine, "s", "Build a FAQ page that looks like a real FAQ section", root,
                    lambda role: LLM(), FallbackJudge())
    assert ground_truth_hint(plain.goal, root) is None


def test_cases_go_down_the_plan_and_none_is_dropped(project):
    engine, root = project
    comp = _good(engine, add_node(engine, "s", "architect", None, "evaluator in calc/", done_when="works",
                                  references=["reference:bc"]))
    planner = Planner(Console([]), engine, "s", "calc vs bc", root, lambda role: LLM(), FallbackJudge())
    component = next(n for n in load_nodes(engine, "s") if n.id == comp)
    lead_finish = planner._finish(ScopeAnchor("lead", comp, "evaluator", "finish"), "lead", "breakdown", component)
    assert lead_finish(comp, "done").startswith("Error: not finished yet. This component rebuilds part of reference:bc")

    file_node = _good(engine, add_node(engine, "s", "lead", comp, "calc/ops.py: + - * /", done_when="tested",
                                       files=["calc/ops.py"], cases=["add", "divide_by_zero"]))
    assert "No evidence for add, divide_by_zero" in lead_finish(comp, "done")
    other = add_node(engine, "s", "lead", comp, "calc/other.py", done_when="x", files=["calc/other.py"], cases=["add"])
    assert other.startswith("Error: case add is already on node")
    capture_evidence(engine, "s", root, "lead", "add", ["1 + 1"])
    capture_evidence(engine, "s", root, "lead", "divide_by_zero", ["1 / 0"])
    assert not lead_finish(comp, "done").startswith("Error")

    file_row = next(n for n in load_nodes(engine, "s") if n.id == file_node)
    task_finish = planner._finish(ScopeAnchor("task", file_node, "ops", "finish"), "task", "breakdown", file_row)
    impl = add_node(engine, "s", "task", file_node, "implement evaluate() in calc/ops.py: + - * /",
                    done_when="evaluate('1 + 1') == 2", files=["calc/ops.py"], kind="implement")
    impl_id = int(impl.split("id=")[1].split()[0].rstrip("."))
    assert task_finish(file_node, "done").startswith("Error: not finished yet. Case(s) add, divide_by_zero")

    no_dep = add_node(engine, "s", "task", file_node, "compare + with evidences/add", done_when="matches",
                      kind="compare", cases=["add"])
    assert no_dep.startswith("Error: a compare leaf needs depends_on")
    foreign = add_node(engine, "s", "task", file_node, "compare power", done_when="matches", kind="compare",
                       cases=["power"], depends_on=[impl_id])
    assert foreign.startswith("Error: power isn't one of this file's cases (add, divide_by_zero)")
    (root / "evidences" / "add.txt").rename(root / "add.txt.bak")
    unseen = add_node(engine, "s", "task", file_node, "compare +", done_when="matches", kind="compare",
                      cases=["add"], depends_on=[impl_id], root=root)
    assert unseen.startswith("Error: no evidence for add in evidences/")
    (root / "add.txt.bak").rename(root / "evidences" / "add.txt")
    for case in ("add", "divide_by_zero"):
        assert add_node(engine, "s", "task", file_node, f"compare {case} with evidences/{case}", done_when="matches",
                        kind="compare", cases=[case], depends_on=[impl_id], files=["calc/ops.py"],
                        root=root).startswith("Added")
    assert not task_finish(file_node, "done").startswith("Error")
    assert update_node(engine, "s", "task", impl_id, cases=["add"]).startswith("Error: only the Lead")


# ------------------------------------------------------------------ Dev's compare leaf

def _compare_leaf(engine, root):
    comp = _good(engine, add_node(engine, "s", "architect", None, "calculator", done_when="works",
                                  references=["reference:bc"]))
    file_node = _good(engine, add_node(engine, "s", "lead", comp, "main.py", done_when="tested", files=["main.py"],
                                       cases=["add"]))
    capture_evidence(engine, "s", root, "lead", "add", ["1 + 1", "-3 + 10"])
    impl = _good(engine, add_node(engine, "s", "task", file_node, "implement + in main.py", done_when="1 + 1 -> 2",
                                  files=["main.py"], kind="implement"))
    with get_session(engine) as db:
        row = db.get(Leaf, impl)
        row.status = LeafStatus.DONE
        db.add(row)
        db.commit()
    return _good(engine, add_node(engine, "s", "task", file_node, "compare + with evidences/add", kind="compare",
                                  done_when="matches", cases=["add"], depends_on=[impl], files=["main.py"]))


def test_a_compare_leaf_is_done_only_when_the_code_matches_the_evidence(project):
    """The add task's code says 1 + 1 = 2.5; bc (here, the oracle) says 2.
    mark_leaf_done compares itself and refuses with both answers; after the
    fix it passes and remembers which evidence it passed against."""
    engine, root = project
    leaf = _compare_leaf(engine, root)
    turns = [turn(call("compare_evidence", case="add")),
             turn(call("mark_leaf_done", leaf_id=leaf, summary="compared")),
             turn(call("write_file", file_path="main.py", content=RIGHT_CALC),
                  call("mark_leaf_done", leaf_id=leaf, summary="fixed +"))]
    console = Console(turns)
    imp = Imp(console, engine, "s", root, LLM(), dict(runner.TOOL_MAP), lambda: None)
    assert imp.run().complete

    with get_session(engine) as db:
        row = db.get(Leaf, leaf)
        results = [m.content for m in db.exec(HistoryMessage.__table__.select()).all() if m.role == "tool"]
    assert row.status == LeafStatus.DONE and row.evidence_hash == evidence_hash(root, ["add"])
    assert any(r.startswith("Error: the new code doesn't match the evidence") and "new: 2.5" in r for r in results)
    assert any(r.startswith("Done: leaf") for r in results)


def test_changed_evidence_sends_a_passed_compare_leaf_back(project):
    engine, root = project
    leaf = _compare_leaf(engine, root)
    (root / "main.py").write_text(RIGHT_CALC)
    imp = Imp(Console([turn(call("mark_leaf_done", leaf_id=leaf, summary="ok"))]), engine, "s", root, LLM(),
              dict(runner.TOOL_MAP), lambda: None)
    assert imp.run().complete

    text = (root / "evidences" / "add.txt").read_text().replace(">>> -3 + 10\n7", ">>> -3 + 10\n8")
    save_edited_text(root, "add", text)
    imp._requeue_changed_evidence()
    with get_session(engine) as db:
        row = db.get(Leaf, leaf)
    assert row.status == LeafStatus.TODO and "evidence for add changed" in row.fix_note


def test_dev_editing_the_evidence_is_refused(project):
    engine, root = project
    leaf = _compare_leaf(engine, root)
    content = (root / "evidences" / "add.txt").read_text().replace(">>> 1 + 1\n2", ">>> 1 + 1\n2.5")
    turns = [turn(call("write_file", file_path="evidences/add.txt", content=content),
                  call("mark_leaf_done", leaf_id=leaf, summary="made it match"))]
    imp = Imp(Console(turns), engine, "s", root, LLM(), dict(runner.TOOL_MAP), lambda: None)
    imp.run()
    with get_session(engine) as db:
        results = [m.content for m in db.exec(HistoryMessage.__table__.select()).all() if m.role == "tool"]
    assert any(r.startswith("Error: evidences/ changed while you worked") for r in results)


# ------------------------------------------------------------------ review and EVIDENCE_REVIEW

def test_the_reviewer_cant_pass_a_build_that_doesnt_match(project):
    engine, root = project
    leaf = _compare_leaf(engine, root)
    reviewer = Reviewer(Console([]), engine, "s", root, LLM(), {})
    refusal, _ = reviewer._evidence_check()
    assert refusal.startswith("Error: not a pass -- the build doesn't match its ground truth")
    assert f"compare leaf {leaf}" in refusal
    (root / "main.py").write_text(RIGHT_CALC)
    refusal, note = reviewer._evidence_check()
    assert refusal is None and "All 1 ground-truth case(s) match" in note


def test_evidence_review_is_off_by_default_and_asks_once_when_on(project, monkeypatch):
    engine, root = project
    capture_evidence(engine, "s", root, "lead", "add", ["1 + 1"])
    ssm = SimpleNamespace(session_path=str(root / ".jfi"), db_engine=engine, session_id="s")
    asked = []

    class Asking(Console):
        def get_user_choice(self, label, options):
            asked.append(label)
            return "a"

    monkeypatch.delenv("EVIDENCE_REVIEW", raising=False)
    assert runner._evidence_review(Asking([]), ssm) and asked == []

    monkeypatch.setenv("EVIDENCE_REVIEW", "1")
    assert runner._evidence_review(Asking([]), ssm) and len(asked) == 1
    assert runner._evidence_review(Asking([]), ssm) and len(asked) == 1  # accepted, unchanged: not asked again
    with get_session(engine) as db:
        assert db.exec(PlanEvent.__table__.select().where(PlanEvent.type == "evidence_accepted")).first()
    capture_evidence(engine, "s", root, "lead", "add", ["1 + 1", "2 + 2"])
    assert runner._evidence_review(Asking([]), ssm) and len(asked) == 2


# ------------------------------------------------------------------ visual

def _chromium() -> bool:
    try:
        import JFI.tool.browser_tools  # noqa: F401
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            p.chromium.launch().close()
        return True
    except Exception:  # noqa: BLE001
        return False


@pytest.mark.skipif(not _chromium(), reason="Chromium for Playwright isn't installed")
def test_a_page_state_is_compared_as_screenshots_and_text(project):
    """Found while building this: a nav link missing from the new page
    changed 0.03% of the pixels and passed on pixels alone, so text counts
    too; and "click Watchlist" hit the nav label instead of the button, so a
    control with that name is clicked first."""
    engine, root = project
    page = ('<html><body style="margin:0;font-family:sans-serif"><nav>Watchlist · Holdings · News</nav>'
            '<div id="w" style="display:none">Watchlist table</div>'
            '<button onclick="document.getElementById(\'w\').style.display=\'block\'">Watchlist</button>'
            '</body></html>')
    (root / "old.html").write_text(page)
    (root / "new.html").write_text(page.replace(" · News", ""))
    saved = capture_evidence(engine, "s", root, "lead", "watch_tab", url="old.html", new_url="new.html",
                             steps=["click Watchlist"])
    assert saved.startswith("Saved evidences/watch_tab.png")
    assert "Watchlist table" in read_evidence(root, "watch_tab").spec["text"]

    result = compare_case(engine, "s", root, "watch_tab")
    assert not result.ok and "text missing from the new page: news" in result.report
    assert result.image_url.startswith("data:image/png;base64,")
    (root / "new.html").write_text(page)
    assert compare_case(engine, "s", root, "watch_tab").ok


def test_each_role_gets_its_evidence_tools(project):
    """The tools only work if their names match a core set and a schema:
    EpisodeTools silently drops a tool that has neither."""
    from JFI.episode.tools import EpisodeTools
    from JFI.planner.nodes import make_node_tools
    from JFI.tool.evidence_tools import make_evidence_tools

    engine, root = project
    impl = {**make_node_tools(engine, "s", "lead", None, root), **make_evidence_tools(engine, "s", root, "lead"),
            "execute_command": lambda command: ""}
    active = {role: EpisodeTools(role, impl).active for role in ("architect", "lead", "task", "dev", "reviewer")}
    assert "execute_command" in active["architect"]
    assert {"capture_evidence", "list_evidence"} <= set(active["lead"])
    assert "list_evidence" in active["task"] and "capture_evidence" not in active["task"]
    assert "compare_evidence" in active["dev"] and "compare_evidence" in active["reviewer"]
    [add] = [s for s in EpisodeTools("task", impl).schemas() if s["function"]["name"] == "add_node"]
    assert "cases" in add["function"]["parameters"]["properties"]
