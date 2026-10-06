"""Ground-truth evidence (docs/old_new.md): every node under a ground truth --
the Architect's component, the Lead's file, Task's leaf -- captures its own
case into .jfi/evidence/<session>/, and Dev finishes a node only when the new
code's output matches it (bottom-up: leaves first, then their parents).
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

    assert result.startswith("Saved .jfi/evidence/s/add.txt")
    evidence = read_evidence(root, "s", "add")
    assert [(i.input, i.output, i.error) for i in evidence.items] == [
        ("1 + 1", "2", False), ("2.5 + 0.25", "2.75", False), ("-3 + 10", "7", False)]
    assert evidence.source.startswith("command ") and not evidence.unverified
    text = (root / ".jfi" / "evidence" / "s" / "add.txt").read_text()
    assert ">>> 1 + 1\n2\n" in text and "# how: " in text


def test_the_ground_truth_failing_on_an_input_is_evidence_too(project):
    """bc answers 1 / 0 with "Divide by zero" on stderr; that's what the new
    code must also treat as an error, in its own words."""
    engine, root = project
    capture_evidence(engine, "s", root, "lead", "divide_by_zero", ["1 / 0"])
    [item] = read_evidence(root, "s", "divide_by_zero").items
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
        assert read_evidence(root, "s", "add") is None
    runbook_set(engine, "s", "evidence_one", "{ echo scale=10; cat {input_file}; } | bc -l")
    capture_evidence(engine, "s", root, "lead", "add", ["1 + 1", "2.5 + 0.25"])
    assert [i.output for i in read_evidence(root, "s", "add").items] == ["2", "2.75"]


def test_a_command_that_isnt_there_is_refused(project):
    engine, root = project
    runbook_set(engine, "s", "evidence_one", "./no-such-oracle {input_file}")
    assert capture_evidence(engine, "s", root, "lead", "add", ["1 + 1"]).startswith("Error: evidence_one didn't run")
    assert read_evidence(root, "s", "add") is None


def test_answers_from_the_model_are_the_last_resort_and_flagged(project):
    engine, root = project
    with get_session(engine) as db:
        db.delete(db.exec(runner_select_runbook("evidence_one")).first())
        db.commit()
    refused = capture_evidence(engine, "s", root, "lead", "add", ["1 + 1"])
    assert refused.startswith("Error: there's no way to get the ground truth's answers")

    saved = capture_evidence(engine, "s", root, "lead", "add", ["1 + 1"], answers=["2"])
    assert "NOT VERIFIED" in saved
    assert read_evidence(root, "s", "add").source == LLM_SOURCE and read_evidence(root, "s", "add").unverified

    (root / "docs").mkdir()
    (root / "docs" / "api.md").write_text("1 + 1 is 2\n")
    capture_evidence(engine, "s", root, "lead", "add", ["1 + 1"], answers=["2"], from_file="docs/api.md L1-1")
    assert read_evidence(root, "s", "add").source == "file docs/api.md L1-1"


def runner_select_runbook(name):
    return select(RunbookEntry).where(RunbookEntry.session_id == "s", RunbookEntry.name == name)


# ------------------------------------------------------------------ compare

HOLDINGS_ROWS = (  # two rows of portfolio-dashboard.html's holdings table, as the user shared it
    '<tr><td><div class="sym-name">AAPL</div><div class="sym-full">Apple Inc.</div></td>\n'
    '  <td class="mono">84</td><td class="mono">$189.40</td><td class="mono">$219.55</td>\n'
    '<tr><td><div class="sym-name">MSFT</div><div class="sym-full">Microsoft Corp.</div></td>\n'
    '  <td class="mono">40</td><td class="mono">$402.10</td><td class="mono">$438.22</td>\n')


def test_data_taken_from_the_original_is_evidence_too(project):
    """Observed on the portfolio-dashboard run: the component extracting the
    page's data into CSV files (and its files) had no evidence at all. Rows
    copied from the original are checked against its lines -- a made-up value
    is refused -- even with an evidence_one in the runbook, and the CSV is
    compared with them through the case's own command."""
    engine, root = project
    (root / "page.html").write_text(HOLDINGS_ROWS)
    (root / "data").mkdir()
    (root / "data/holdings.csv").write_text("symbol,name,shares,avg_cost,price\n"
                                            "AAPL,Apple Inc.,84,189.40,219.55\nMSFT,Microsoft Corp.,40,402.10,438.22\n")
    made_up = capture_evidence(engine, "s", root, "task", "holdings_rows", inputs=["AAPL"],
                               answers=["AAPL Apple Inc. 85 189.40"], from_file="page.html L1-2")
    assert made_up.startswith("Error: 85 (from the answer 'AAPL Apple Inc. 85 189.40') isn't in page.html L1-2")
    saved = capture_evidence(engine, "s", root, "task", "holdings_rows", inputs=["AAPL", "MSFT"],
                             answers=["AAPL Apple Inc. 84 189.40 219.55", "MSFT Microsoft Corp. 40 402.10 438.22"],
                             from_file="page.html L1-4", match="contains",
                             new_command="grep {input} data/holdings.csv")
    assert saved.startswith("Saved") and "source: file page.html L1-4" in saved  # not the runbook's evidence_one
    assert compare_case(engine, "s", root, "holdings_rows").ok

    (root / "data/holdings.csv").write_text("symbol,name,shares,avg_cost,price\n"
                                            "AAPL,Apple Inc.,48,189.40,219.55\nMSFT,Microsoft Corp.,40,402.10,438.22\n")
    result = compare_case(engine, "s", root, "holdings_rows")
    assert not result.ok and "holdings_rows: 1 of 2 differ" in result.report


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
    the evidence folder, its result on the old database captured, and the same query
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

    assert (root / ".jfi" / "evidence" / "s" / "customers_by_status.sql").read_text().strip() == sql
    assert compare_case(engine, "s", root, "customers_by_status").ok
    with sqlite3.connect(root / "new.db") as db:
        db.execute("DELETE FROM customers WHERE id = 2")  # a row lost in the migration
    result = compare_case(engine, "s", root, "customers_by_status")
    assert not result.ok and "MISMATCH" in result.report


def test_reviewing_doesnt_count_as_a_change_but_editing_and_recapturing_do(project):
    engine, root = project
    capture_evidence(engine, "s", root, "lead", "add", ["1 + 1"])
    before = evidence_hash(root, "s", ["add"])
    mark_reviewed(root, "s", "add")
    assert evidence_hash(root, "s", ["add"]) == before and read_evidence(root, "s", "add").header["reviewed"]

    text = (root / ".jfi" / "evidence" / "s" / "add.txt").read_text().replace(">>> 1 + 1\n2", ">>> 1 + 1\n2\n>>> 2 + 2\n4")
    assert save_edited_text(root, "s", "add", text) == "Saved add.txt."
    edited = read_evidence(root, "s", "add")
    assert edited.source == "user" and edited.header["was"].startswith("command") and len(edited.items) == 2
    assert evidence_hash(root, "s", ["add"]) != before

    (root / ".jfi" / "evidence" / "s" / "add.txt").write_text(text)  # a hand edit that kept the command header
    assert recapture(engine, "s", root, "add").startswith("Re-captured add")
    assert [i.output for i in read_evidence(root, "s", "add").items] == ["2", "4"]


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
    # The component that rebuilds part of the ground truth needs its own case.
    [bare] = planner._reference_problems(load_nodes(engine, "s"), runbook)
    assert bare.startswith("a case on node(s) 1")
    update_node(engine, "s", "architect", 1, cases=["calc_view"])
    [missing] = planner._reference_problems(load_nodes(engine, "s"), runbook)
    assert missing.startswith("evidence for calc_view")
    capture_evidence(engine, "s", root, "architect", "calc_view", ["2 + 2"])
    assert planner._reference_problems(load_nodes(engine, "s"), runbook) == []
    assert any("compare_one" in p for p in planner._reference_problems(load_nodes(engine, "s"),
                                                                        {"evidence_one": "x {input}"}))

    plain = Planner(Console([]), engine, "s", "Build a FAQ page that looks like a real FAQ section", root,
                    lambda role: LLM(), FallbackJudge())
    assert ground_truth_hint(plain.goal, root) is None


def test_every_node_under_a_ground_truth_gets_its_own_evidence(project):
    """The user, after the first real run: "you need to create evidence for
    all. There need not be a sub point to validate against evidence." Every
    file node and every leaf under a component that cites a reference has its
    own case; no compare leaves; a deletion needs none."""
    engine, root = project
    design_set(engine, "s", "reference", "bc", "behavioural: bc -l")
    comp = _good(engine, add_node(engine, "s", "architect", None, "evaluator in calc/", done_when="works",
                                  references=["reference:bc"]))
    planner = Planner(Console([]), engine, "s", "calc vs bc", root, lambda role: LLM(), FallbackJudge())
    component = next(n for n in load_nodes(engine, "s") if n.id == comp)
    lead_finish = planner._finish(ScopeAnchor("lead", comp, "evaluator", "finish"), "lead", "breakdown", component)

    file_node = _good(engine, add_node(engine, "s", "lead", comp, "calc/ops.py: + - * /", done_when="tested",
                                       files=["calc/ops.py"]))
    assert f"a case on node(s) {file_node}" in lead_finish(comp, "done")
    update_node(engine, "s", "lead", file_node, cases=["ops_table"])
    _good(engine, f"id={file_node}")
    assert "evidence for ops_table" in lead_finish(comp, "done")
    capture_evidence(engine, "s", root, "lead", "ops_table", ["1 + 1", "6 / 3"])
    assert not lead_finish(comp, "done").startswith("Error")

    # Evidence on no node would never be compared (load_holdings.txt on the real run).
    capture_evidence(engine, "s", root, "lead", "stray", ["2 * 2"])
    assert "a node for the evidence of stray" in lead_finish(comp, "done")
    update_node(engine, "s", "lead", file_node, cases=["ops_table", "stray"])
    _good(engine, f"id={file_node}")

    file_row = next(n for n in load_nodes(engine, "s") if n.id == file_node)
    task_finish = planner._finish(ScopeAnchor("task", file_node, "ops", "finish"), "task", "breakdown", file_row)
    impl = add_node(engine, "s", "task", file_node, "implement add() in calc/ops.py: a + b",
                    done_when="add(1, 1) == 2", files=["calc/ops.py"], kind="implement")
    impl_id = int(impl.split("id=")[1].split()[0].rstrip("."))
    add_node(engine, "s", "task", file_node, "delete old_add() in calc/ops.py", done_when="gone",
             files=["calc/ops.py"], kind="delete")
    assert task_finish(file_node, "done") == (f"Error: not finished yet. Still missing: a case on node(s) {impl_id} "
                                              "(cases=[...] with update_node, each captured with capture_evidence): "
                                              "every node is compared with its own evidence when it's done.")
    clash = update_node(engine, "s", "task", impl_id, cases=["ops_table"])
    assert clash.startswith(f"Error: case ops_table is already on node {file_node}")
    assert update_node(engine, "s", "task", impl_id, cases=["add"]).startswith("Updated")
    capture_evidence(engine, "s", root, "task", "add", ["1 + 1"])
    assert not task_finish(file_node, "done").startswith("Error")
    # A component that doesn't cite the reference needs its evidence too: on
    # the portfolio-dashboard run the CSV data component and its files had none.
    data = _good(engine, add_node(engine, "s", "architect", None, "extract the data into data/*.csv",
                                  done_when="csvs exist"))
    data_row = next(n for n in load_nodes(engine, "s") if n.id == data)
    data_finish = planner._finish(ScopeAnchor("lead", data, "data", "finish"), "lead", "breakdown", data_row)
    csv_file = _good(engine, add_node(engine, "s", "lead", data, "data/holdings.csv", done_when="rows",
                                      files=["data/holdings.csv"]))
    assert f"a case on node(s) {csv_file}" in data_finish(data, "done")
    [bare] = [p for p in planner._reference_problems(load_nodes(engine, "s"), {
        "evidence_one": "x {input}", "compare_one": "y {input}"}) if p.startswith("a case on")]
    assert bare.startswith(f"a case on node(s) {comp}, {data} ")  # every component, citing it or not

    # No new compare leaves: "compare" isn't a kind the planner can give any more.
    added = add_node(engine, "s", "task", file_node, "compare add with its evidence", done_when="matches",
                     files=["calc/ops.py"], kind="compare", cases=["add2"])
    assert next(n for n in load_nodes(engine, "s") if n.description.startswith("compare add")).kind is None, added


def test_the_architect_cant_add_the_same_component_twice(project):
    """Observed on the portfolio-dashboard run: component 15 repeated 1 word
    for word, and 16 repeated 2 in other words."""
    engine, _ = project
    add_node(engine, "s", "architect", None, "Scaffold Next.js app with TypeScript, install Recharts and "
             "dependencies: working dev server at localhost:3000", done_when="dev server runs")
    add_node(engine, "s", "architect", None, "Extract all embedded JS data into CSV files under data/: holdings, "
             "dividends, allocation, transactions, news, watchlist, outlook projections", done_when="csvs exist")
    again = add_node(engine, "s", "architect", None, "Scaffold Next.js app with TypeScript, install Recharts and "
                     "dependencies: working dev server at localhost:3000", done_when="dev server runs")
    assert again.startswith("Error: component 1 already covers this")
    reworded = add_node(engine, "s", "architect", None, "Extract all dashboard data into CSV files under data/ "
                        "directory: holdings, dividends, transactions, news, watchlist, outlook projections, "
                        "ticker profiles", done_when="csvs exist")
    assert reworded.startswith("Error: component 2 already covers this")
    for page in ("Build Holdings page with sortable table of all positions and performance data",
                 "Build Watchlist page showing tracked stocks with price changes and alerts",
                 "Build Transactions page with searchable table of all buy/sell activity"):
        assert add_node(engine, "s", "architect", None, page, done_when="renders").startswith("Added")


# ------------------------------------------------------------------ Dev compares each node

def _case_leaf(engine, root):
    """calculator (1) > main.py (1.1) > implement + (1.1.1, case add)."""
    comp = _good(engine, add_node(engine, "s", "architect", None, "calculator", done_when="works",
                                  references=["reference:bc"]))
    file_node = _good(engine, add_node(engine, "s", "lead", comp, "main.py", done_when="tested", files=["main.py"]))
    leaf = _good(engine, add_node(engine, "s", "task", file_node, "implement + in main.py", done_when="1 + 1 -> 2",
                                  files=["main.py"], kind="implement", cases=["add"]))
    capture_evidence(engine, "s", root, "task", "add", ["1 + 1", "-3 + 10"])
    return comp, file_node, leaf


def test_a_node_is_done_only_when_the_code_matches_its_evidence(project):
    """The + leaf's code says 1 + 1 = 2.5; bc (here, the oracle) says 2.
    mark_leaf_done compares itself and refuses with both answers; after the
    fix it passes and remembers which evidence it passed against. Then the
    file and the component, above it, get their own turn."""
    engine, root = project
    comp, file_node, leaf = _case_leaf(engine, root)
    turns = [turn(call("compare_evidence", case="add")),
             turn(call("mark_leaf_done", leaf_id=leaf, summary="built +")),
             turn(call("write_file", file_path="main.py", content=RIGHT_CALC),
                  call("mark_leaf_done", leaf_id=leaf, summary="fixed +")),
             turn(call("mark_leaf_done", leaf_id=file_node, summary="file works", check="python3 -c pass")),
             turn(call("mark_leaf_done", leaf_id=comp, summary="component works", check="python3 -c pass"))]
    console = Console(turns)
    imp = Imp(console, engine, "s", root, LLM(), dict(runner.TOOL_MAP), lambda: None)
    assert imp.run().complete

    with get_session(engine) as db:
        rows = {n: db.get(Leaf, n) for n in (comp, file_node, leaf)}
        results = [m.content for m in db.exec(HistoryMessage.__table__.select()).all() if m.role == "tool"]
    assert all(r.status == LeafStatus.DONE for r in rows.values())
    assert rows[leaf].evidence_hash == evidence_hash(root, "s", ["add"])
    assert rows[leaf].ended_at <= rows[file_node].ended_at <= rows[comp].ended_at  # bottom-up
    assert any(r.startswith("Error: the new code doesn't match the evidence") and "new: 2.5" in r for r in results)
    assert any(r.startswith(f"Done: node {leaf} (compared; it matches the evidence for add)") for r in results)
    # Dev's comparison is kept as the leaf's own evidence, named by its number.
    assert (root / ".jfi" / "evidence" / "s" / "1.1.1_add.result.txt").is_file()


def test_a_difference_left_for_a_later_task_is_accepted_and_noted(project):
    from JFI.models import SessionNote

    engine, root = project
    _, _, leaf = _case_leaf(engine, root)
    imp = Imp(Console([turn(call("mark_leaf_done", leaf_id=leaf, summary="built",
                                 accept_difference="the + rounding is task 1.1.2's"))]),
              engine, "s", root, LLM(), dict(runner.TOOL_MAP), lambda: None)
    imp._work(next(n for n in load_nodes(engine, "s") if n.id == leaf))
    with get_session(engine) as db:
        assert db.get(Leaf, leaf).status == LeafStatus.DONE
        notes = " ".join(n.text for n in db.exec(select(SessionNote)))
    assert "accepted a difference from its evidence (add): the + rounding is task 1.1.2's" in notes


def test_changed_evidence_sends_a_finished_node_and_its_parents_back(project):
    engine, root = project
    comp, file_node, leaf = _case_leaf(engine, root)
    (root / "main.py").write_text(RIGHT_CALC)
    turns = [turn(call("mark_leaf_done", leaf_id=leaf, summary="ok")),
             turn(call("mark_leaf_done", leaf_id=file_node, summary="ok", check="python3 -c pass")),
             turn(call("mark_leaf_done", leaf_id=comp, summary="ok", check="python3 -c pass"))]
    imp = Imp(Console(turns), engine, "s", root, LLM(), dict(runner.TOOL_MAP), lambda: None)
    assert imp.run().complete

    text = read_evidence(root, "s", "add").path.read_text().replace(">>> -3 + 10\n7", ">>> -3 + 10\n8")
    save_edited_text(root, "s", "add", text)
    imp._requeue_changed_evidence()
    with get_session(engine) as db:
        row = db.get(Leaf, leaf)
        parents = [db.get(Leaf, n).status for n in (file_node, comp)]
    assert row.status == LeafStatus.TODO and "evidence for add changed" in row.fix_note
    assert parents == [LeafStatus.TODO, LeafStatus.TODO]


def test_dev_editing_the_evidence_is_refused(project):
    engine, root = project
    _, _, leaf = _case_leaf(engine, root)
    add_file = read_evidence(root, "s", "add").path
    content = add_file.read_text().replace(">>> 1 + 1\n2", ">>> 1 + 1\n2.5")
    turns = [turn(call("write_file", file_path=f".jfi/evidence/s/{add_file.name}", content=content),
                  call("mark_leaf_done", leaf_id=leaf, summary="made it match"))]
    imp = Imp(Console(turns), engine, "s", root, LLM(), dict(runner.TOOL_MAP), lambda: None)
    imp.run()
    with get_session(engine) as db:
        results = [m.content for m in db.exec(HistoryMessage.__table__.select()).all() if m.role == "tool"]
    # The file tool refuses outright; the gate's own check (the evidence
    # unchanged since the episode began) is the second line, for
    # execute_command and the like.
    assert any("is the ground truth and can't be edited with a file tool" in r for r in results)
    assert read_evidence(root, "s", "add").items[0].output == "2"


# ------------------------------------------------------------------ review and EVIDENCE_REVIEW

def test_the_reviewer_cant_pass_a_build_that_doesnt_match(project):
    engine, root = project
    _, _, leaf = _case_leaf(engine, root)
    reviewer = Reviewer(Console([]), engine, "s", root, LLM(), {})
    refusal, _ = reviewer._evidence_check()
    assert refusal.startswith("Error: not a pass -- the build doesn't match its ground truth")
    assert f"add (node {leaf})" in refusal
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
    assert saved.startswith("Saved .jfi/evidence/s/watch_tab.png")
    assert "Watchlist table" in read_evidence(root, "s", "watch_tab").spec["text"]

    result = compare_case(engine, "s", root, "watch_tab")
    assert not result.ok and "text missing from the new page: news" in result.report
    assert result.image_url.startswith("data:image/png;base64,")
    (root / "new.html").write_text(page)
    assert compare_case(engine, "s", root, "watch_tab").ok


# A side bar like portfolio-dashboard.html's: plain divs with onclick (no
# button or link role), a KPI card that says "Holdings" before them in the
# page, and one view shown at a time.
SIDEBAR_PAGE = ("<html><body style='margin:0;font-family:sans-serif;display:flex'>"
                "<main style='flex:1'><div class='kpi'>Holdings</div>"
                "<section id='home'>Home: total value $120,000</section>"
                "<section id='holdings' style='display:none'><table><tr><td>AAPL 10</td></tr></table></section></main>"
                "<aside style='width:200px;order:-1'>"
                "<div class='nav-item' data-view='home' style='cursor:pointer' onclick='show(\"home\")'>"
                "<span>&#8962;</span><span>Home</span></div>"
                "<div class='nav-item' data-view='holdings' style='cursor:pointer' onclick='show(\"holdings\")'>"
                "<span>&#9783;</span><span>My Holdings</span></div></aside>"
                "<script>function show(v){for(const s of document.querySelectorAll('section'))"
                "s.style.display=s.id===v?'block':'none'}</script></body></html>")


@pytest.mark.skipif(not _chromium(), reason="Chromium for Playwright isn't installed")
def test_a_side_bar_tab_is_never_saved_as_the_home_page(project):
    """Observed on the first real run (portfolio-dashboard.html): every
    side-bar tab's evidence -- holdings table, dividend chart, watchlist,
    news, settings -- was the home page. A click that changes nothing is
    refused with what can be clicked, and an image identical to another
    case's is refused, instead of being saved as evidence."""
    engine, root = project
    (root / "old.html").write_text(SIDEBAR_PAGE)
    home = capture_evidence(engine, "s", root, "lead", "home_view", url="old.html", new_url="/")
    assert home.startswith("Saved")

    # "Holdings" matches the KPI label first, which does nothing when clicked.
    missed = capture_evidence(engine, "s", root, "lead", "holdings_tab", url="old.html", new_url="/holdings",
                              steps=["click Holdings"])
    assert missed.startswith("Error: the steps ['click Holdings'] changed nothing")
    assert 'div[data-view="holdings"]' in missed and "My Holdings" in missed  # what to click instead
    no_steps = capture_evidence(engine, "s", root, "lead", "holdings_tab", url="old.html", new_url="/holdings")
    assert no_steps.startswith("Error: this is the same screen as case home_view's evidence")
    hidden = capture_evidence(engine, "s", root, "lead", "holdings_tab", url="old.html", new_url="/holdings",
                              selector="#holdings table")
    assert "hidden on this screen" in hidden
    assert read_evidence(root, "s", "holdings_tab") is None  # nothing wrong was kept

    saved = capture_evidence(engine, "s", root, "lead", "holdings_tab", url="old.html", new_url="/holdings",
                             steps=['click [data-view="holdings"]'])
    assert saved.startswith("Saved") and "AAPL 10" in read_evidence(root, "s", "holdings_tab").spec["text"]


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
    assert {"capture_evidence", "list_evidence"} <= set(active["task"])  # every leaf captures its own
    assert "compare_evidence" in active["dev"] and "compare_evidence" in active["reviewer"]
    [add] = [s for s in EpisodeTools("task", impl).schemas() if s["function"]["name"] == "add_node"]
    assert "cases" in add["function"]["parameters"]["properties"]


@pytest.mark.skipif(not _chromium(), reason="Chromium for Playwright isn't installed")
def test_one_part_of_a_page_is_shot_with_a_selector_not_as_an_image(project):
    """Observed on the first real run (a dashboard rebuilt in Next.js): to
    capture one chart, the Lead passed image="<the dashboard>.html", and the
    page's first <svg> -- its logo -- became the evidence for a dozen cases
    (allocation_donut, change_pill_colors, build_config, ...). A page given as
    an image is refused; selector= shoots the part asked for, on both sides."""
    engine, root = project
    page = ('<html><body style="margin:0"><header><svg id="logo" width="40" height="40"><circle cx="20" cy="20" '
            'r="18" fill="#36f"/></svg> Portfolio</header>'
            '<div id="donut" style="width:300px;height:200px;background:conic-gradient(#e33 0 40%, #3a3 0)">'
            'Allocation 40% stocks</div></body></html>')
    (root / "dashboard.html").write_text(page)
    (root / "new.html").write_text(page.replace("#e33 0 40%", "#e33 0 70%"))

    refused = capture_evidence(engine, "s", root, "lead", "allocation_donut", image="dashboard.html",
                               new_url="new.html")
    assert refused.startswith("Error: image= takes an image file") and "selector=" in refused
    assert read_evidence(root, "s", "allocation_donut") is None

    capture_evidence(engine, "s", root, "lead", "allocation_donut", url="dashboard.html", new_url="new.html",
                     selector="#donut")
    evidence = read_evidence(root, "s", "allocation_donut")
    assert evidence.spec["selector"] == "#donut" and evidence.spec["text"] == "Allocation 40% stocks"
    assert evidence.path.read_bytes()[16:24] == (300).to_bytes(4, "big") + (200).to_bytes(4, "big")  # PNG size

    result = compare_case(engine, "s", root, "allocation_donut")
    assert not result.ok and "differing pixels" in result.report  # 40% vs 70% red: a real visual difference
    missing = capture_evidence(engine, "s", root, "lead", "pills", url="dashboard.html", new_url="new.html",
                               selector="#pills")
    assert missing.startswith("Error: nothing on") and "#pills" in missing


def test_evidence_files_are_named_by_the_task_that_owns_them(project):
    """The user: "the png and json that are created must follow the task id
    like 1.1.1 or 1.2.1 ... Arch, lead, dev all of them must give their
    evidence." The Architect's case is named by its component (1), a Lead's
    by its file node (1.1), a Task's by its leaf (1.1.1), Dev's comparison by
    the same node it checked; and when the plan is renumbered the names
    follow."""
    from JFI.tool.evidence_tools import sync_evidence_names

    engine, root = project
    comp = _good(engine, add_node(engine, "s", "architect", None, "calculator", done_when="works",
                                  references=["reference:bc"], cases=["calc_overview"]))
    capture_evidence(engine, "s", root, "architect", "calc_overview", ["2 + 2"])
    file_node = _good(engine, add_node(engine, "s", "lead", comp, "main.py", done_when="tested", files=["main.py"],
                                       cases=["main_ops"]))
    capture_evidence(engine, "s", root, "lead", "main_ops", ["3 - 1"])
    _good(engine, add_node(engine, "s", "task", file_node, "implement + in main.py", done_when="1 + 1 -> 2",
                           files=["main.py"], kind="implement", cases=["add"]))
    capture_evidence(engine, "s", root, "task", "add", ["1 + 1"])
    compare_case(engine, "s", root, "add")
    names = sorted(p.name for p in (root / ".jfi" / "evidence" / "s").iterdir())
    assert names == ["1.1.1_add.result.txt", "1.1.1_add.txt", "1.1_main_ops.txt", "1_calc_overview.txt"]
    # The Architect's case is compared too now, not kept only to look at.
    assert "not compared" not in compare_case(engine, "s", root, "calc_overview").report

    # A later iteration puts a component before this one: everything is now 2.x.
    with get_session(engine) as db:
        db.get(Leaf, comp).sort_key = 20
        db.commit()
    _good(engine, add_node(engine, "s", "architect", None, "a new first component", done_when="works"))
    with get_session(engine) as db:
        first = db.exec(select(Leaf).where(Leaf.description == "a new first component")).one()
        first.sort_key = 10
        db.add(first)
        db.commit()
    sync_evidence_names(engine, "s", root)
    assert sorted(p.name for p in (root / ".jfi" / "evidence" / "s").iterdir()) == [
        "2.1.1_add.result.txt", "2.1.1_add.txt", "2.1_main_ops.txt", "2_calc_overview.result.txt",
        "2_calc_overview.txt"]
    assert compare_case(engine, "s", root, "add").ok is False  # still found under its new name (main.py adds 0.5)
