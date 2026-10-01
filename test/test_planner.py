"""Phase 5 of the v2 rewrite: the planning loop (laya_plan.md §2, §4.7, §5.2).
Real SQLite and real node/code/runbook/design tools; the LLM is scripted at
the console boundary exactly as in test_episode_engine.py, one list of turns
per episode, consumed in order."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from JFI import runner
from JFI.manager.abstract_manager import AbstractManager
from JFI.models import PlanEvent, PlannerVerdict, SessionRecord, get_engine, get_session
from sqlmodel import select
from JFI.planner.judge import BREAKDOWN, GOOD, REDO, FallbackJudge, Verdict
from JFI.planner.loop import Planner
from JFI.planner.nodes import load_nodes
from JFI.session.metadata_store import load_metadata_from_db
from JFI.session.pipeline import session_pipeline


class Console(AbstractManager):
    def __init__(self, turns):
        self.turns = list(turns)
        self.done_phases = []
        self.errors = []

    def should_stop(self):
        return False

    def wait_while_paused(self):
        pass

    def print_agent_response(self, response, prompt_tokens_estimate=0):
        return self.turns.pop(0) if self.turns else {"content": "(no more script)", "tool_calls": None}

    def mark_phase_done(self, phase):
        self.done_phases.append(phase)

    def display_error(self, text):
        self.errors.append(text)

    def display_tool_call(self, *a, **k):
        pass

    def display_tool_result(self, *a, **k):
        pass

    def display_system(self, *a, **k):
        pass

    def display_rule(self, *a, **k):
        pass

    def display_user(self, *a, **k):
        pass

    def display_assistant(self, *a, **k):
        pass

    def get_user_input(self, *a, **k):
        return ""

    def get_user_choice(self, *a, **k):
        return "s"

    def set_status(self, **k):
        pass


class LLM:
    def send_message(self, messages, tools=None):
        return object()


_ids = iter(range(10_000))


def call(tool, **args):
    return {"id": f"c{next(_ids)}", "type": "function", "function": {"name": tool, "arguments": json.dumps(args)}}


def turn(*calls):
    return {"content": None, "tool_calls": list(calls), "usage": None}


def finish(node_id):
    return turn(call("finish", node_id=node_id, summary="done"))


@pytest.fixture
def bare_engine(tmp_path):
    engine = get_engine(tmp_path / ".jfi")
    with get_session(engine) as db:
        db.add(SessionRecord(session_id="s", repo_path=str(tmp_path), pipeline_version="v2"))
        db.commit()
    return engine


@pytest.fixture
def engine(bare_engine, tmp_path):
    """A session whose base is already complete (stack + required runbook), so
    the Architect's finish gate only matters in the tests about it."""
    from JFI.planner.loop import REQUIRED_RUNBOOK
    from JFI.tool.design_tools import design_set
    from JFI.tool.runbook_tools import runbook_set
    design_set(bare_engine, "s", "stack", "stack", "python 3.12, uv, pytest")
    for name in REQUIRED_RUNBOOK:
        command = {"test_one": "uv run pytest {test_id}", "script": "uv run python {file}", "src_dir": "calc/",
                   "test_dir": "tests/", "test_naming": "calc/ops.py -> tests/test_ops.py",
                   "entry": "main.py"}.get(name, f"uv run {name}")
        runbook_set(bare_engine, "s", name, command, notes="e.g. tests/test_calc.py::test_add")
    # The app's entry point is already there: the Architect's finish only asks
    # for an entry node in the tests about it.
    (tmp_path / "main.py").write_text("from calc import ops\n")
    return bare_engine


def _planner(engine, root, console, judge=None, feedback=""):
    return Planner(console, engine, "s", "a calculator CLI", root, lambda role: LLM(),
                   judge or FallbackJudge(), feedback=feedback)


ARCHITECT = [turn(call("design_set", kind="stack", key="stack", text="python 3.12, uv, pytest"),
                  call("runbook_set", name="test", command="uv run pytest"),
                  call("add_node", description="build the calc component in calc/: add numbers",
                       done_when="calc.add works")),
             finish(0)]
LEAD = [turn(call("scaffold_file", path="calc/ops.py", purpose="arithmetic",
                  stubs=[{"signature": "def add(a: int, b: int) -> int:", "does": "sum a and b"}]),
             call("add_node", description="implement calc/ops.py stubs", kind="code",
                  files=["calc/ops.py", "tests/test_ops.py"], done_when="every stub is implemented and tested")),
        finish(1)]
TASK = [turn(call("add_node", description="implement add(a, b) in calc/ops.py: return a + b", kind="implement",
                  files=["calc/ops.py", "tests/test_ops.py"], done_when="add(2, 3) == 5")),
        finish(2)]


def _levels(engine):
    return {n.id: (n.level, n.plan_status, n.parent_id) for n in load_nodes(engine, "s")}


def test_goal_goes_architect_to_lead_to_task_with_stubs_on_disk(engine, tmp_path):
    """The phase-5 exit criterion: with the fallback judge every goal walks
    component -> file -> function leaf, all GOOD, and the Lead's stubs are
    really on disk."""
    result = _planner(engine, tmp_path, Console(ARCHITECT + LEAD + TASK)).run()

    assert result.complete and result.episodes == 3
    assert _levels(engine) == {1: ("architect", GOOD, None), 2: ("lead", GOOD, 1), 3: ("task", GOOD, 2)}
    assert "raise NotImplementedError" in (tmp_path / "calc/ops.py").read_text()
    with get_session(engine) as db:
        verdicts = db.exec(PlannerVerdict.__table__.select()).all()
    assert [(v.node_id, v.final_status) for v in verdicts] == [(1, BREAKDOWN), (2, BREAKDOWN), (3, GOOD)]


def test_nothing_reaches_the_lead_while_an_architect_node_is_unsettled(engine, tmp_path):
    """D15: every Architect node is judged (and any REDO fixed) before the
    first Lead breakdown starts."""
    seen = []

    class Judge:
        def judge(self, nodes):
            seen.append(sorted(n.node_id for n in nodes))
            return FallbackJudge().judge(nodes)

    architect = [turn(call("add_node", description="component a in a/", done_when="a works"),
                      call("add_node", description="component b in b/", done_when="b works")),
                 finish(0)]
    lead_a = [finish(1)]
    lead_b = [finish(2)]
    _planner(engine, tmp_path, Console(architect + lead_a + lead_b), Judge()).run()
    assert seen[0] == [1, 2]


def test_laya_reads_each_nodes_notes_and_its_p_yes_is_stored(engine, tmp_path):
    """The sizing question (docs/laya_poc.md) reads the task and its
    description -- the node's notes -- so the loop must hand Laya the notes,
    and the verdict row keeps P(yes) for the dashboard and later POC runs."""
    from JFI.planner.judge import LayaJudge

    class Router:
        sent = []

        def predict_batch(self, requests):
            self.sent.extend(requests)
            return [{"answers": {"solvable": {"noul": 0.2, "answer_confidence": 0.8}}} for _ in requests]

    router = Router()
    judge = LayaJudge("calc", router_factory=lambda: router, log=lambda _: None)
    architect = [turn(call("add_node", description="component a in a/", done_when="a works",
                           notes="Parses the input file into rows; a bad row is skipped with a warning.")),
                 finish(0)]
    _planner(engine, tmp_path, Console(architect + [finish(1)]), judge).run()
    assert router.sent[0]["state"]["input"] == ("Task: component a in a/\nDescription: Parses the input file into "
                                                "rows; a bad row is skipped with a warning.")
    with get_session(engine) as db:
        verdict = db.exec(select(PlannerVerdict).where(PlannerVerdict.node_id == 1)).first()
    assert (verdict.laya_model, verdict.laya_verdict, verdict.probabilities, verdict.decided_by) == (
        "english", BREAKDOWN, {"yes": 0.2}, "agree")


def test_redo_goes_back_to_the_creator_and_is_capped(engine, tmp_path, monkeypatch):
    """§4.7: a REDO node is re-done by the role that wrote it, only that node;
    once PLANNER_REDO_CAP is spent it's accepted and the reviewer is told."""
    monkeypatch.setenv("PLANNER_REDO_CAP", "1")

    class AlwaysRedo:
        def judge(self, nodes):
            return [Verdict(n.node_id, REDO if n.level == "architect" else GOOD, "llm",
                            rule_status=BREAKDOWN, laya_status=REDO, redo_reason="vague") for n in nodes]

    architect = [turn(call("add_node", description="stuff", done_when="?")), finish(0)]
    redo = [turn(call("update_node", node_id=1, description="build the parser in parse/: tokens to AST",
                      done_when="parse('1+2') returns an AST")), finish(1)]
    result = _planner(engine, tmp_path, Console(architect + redo), AlwaysRedo()).run()

    assert result.complete
    node = load_nodes(engine, "s")[0]
    assert node.description.startswith("build the parser")
    assert node.plan_status == GOOD and node.redo_count == 1
    with get_session(engine) as db:
        events = [e.type for e in db.exec(PlanEvent.__table__.select()).all()]
    assert events.count("redo") == 1 and "cap_reached" in events


def test_escalation_pauses_the_subtree_until_the_parent_is_redone(engine, tmp_path):
    """§4.7: a Lead that can't work within the design escalates; the
    Architect redoes its component, and the paused children are re-judged
    against the new version."""
    lead = [turn(call("add_node", description="implement x.py", kind="code", files=["x.py"], done_when="x"),
                 call("escalate", why="no contract for the database")),
            finish(1)]
    arch_redo = [turn(call("design_set", kind="contract", key="calc->db", text="save(row) -> id"),
                      call("update_node", node_id=1, description="build calc/ with a db contract",
                           done_when="calc saves rows")),
                 finish(1)]
    task = [turn(call("add_node", description="implement f() in x.py", kind="implement", files=["x.py"],
                      done_when="f() == 1")), finish(2)]
    result = _planner(engine, tmp_path, Console(ARCHITECT + lead + arch_redo + task)).run()

    assert result.complete
    nodes = {n.id: n for n in load_nodes(engine, "s")}
    assert nodes[1].escalation_count == 1 and nodes[1].description == "build calc/ with a db contract"
    assert not nodes[2].paused and nodes[2].plan_status == GOOD
    assert all(n.plan_status == GOOD for n in nodes.values())


def test_rerun_resumes_from_db_state(engine, tmp_path):
    """Resume is "run again": a finished architect stage is not re-run."""
    first = Console(ARCHITECT)
    _planner(engine, tmp_path, first).run()  # the Lead episode gets no script and ends without children
    assert load_nodes(engine, "s")[0].plan_status == GOOD

    second = _planner(engine, tmp_path, Console([])).run()
    assert second.complete and second.episodes == 0


def test_episode_budget_settles_the_plan(engine, tmp_path, monkeypatch):
    monkeypatch.setenv("MAX_PLANNER_EPISODES", "1")
    console = Console(ARCHITECT)
    result = _planner(engine, tmp_path, console).run()
    assert result.complete and result.reason == "episode budget reached"
    assert all(n.plan_status == GOOD for n in load_nodes(engine, "s"))
    assert console.errors


def test_every_new_session_is_v2_and_a_missing_record_reads_as_v1(tmp_path, monkeypatch):
    """v1 was removed: JFI_PIPELINE no longer exists, every new session is
    v2. A session with no record predates pipeline_version and was v1."""
    engine = get_engine(tmp_path / ".jfi")
    monkeypatch.setenv("JFI_PIPELINE", "v1")
    load_metadata_from_db(engine, "new", str(tmp_path))
    assert session_pipeline(engine, "new") == "v2"
    assert session_pipeline(engine, "missing") == "v1"


def test_run_phase_sends_v2_sessions_to_the_new_planner(engine, tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "make_llm_stream", lambda prefixes: LLM())
    added = []
    ssm = SimpleNamespace(
        db_engine=engine, session_id="s", session_path=Path(tmp_path) / ".jfi",
        history=[{"role": "user", "content": "My goal is: a calculator\n\nwith two lines\n\n"
                                              "Build the step-by-step plan now, using add_leaf/split_leaf."}],
        add_message=lambda role, content: added.append((role, content)))
    console = Console(ARCHITECT + LEAD + TASK)

    assert runner.run_phase(console, {}, ssm, "planner")
    assert added == [("assistant", runner.PLANNER_PHASE_COMPLETE_MARKER)]
    assert console.done_phases == ["planner"]
    assert (tmp_path / "calc/ops.py").exists()
    assert runner._session_goal(ssm.history) == "a calculator\n\nwith two lines"


def test_replan_feedback_is_what_the_user_said_since_the_last_plan():
    """Only the loop's own feedback message counts: the phase triggers that
    also land in history after a plan are instructions to v1 phases."""
    history = [{"role": "user", "content": "My goal is: x"},
               {"role": "assistant", "content": "PLANNER_COMPLETE"},
               {"role": "user", "content": "Begin implementation. Work through the pending imp leaves"},
               {"role": "user", "content": runner.ITERATION_FEEDBACK_PREFIX + "the review failed:\n\nadd --help"
                                           + "\n\nProject state: 3 files" + runner.ITERATION_FEEDBACK_TAIL
                                           + " as it is, ..."}]
    assert runner._replan_feedback(history) == "the review failed:\n\nadd --help\n\nProject state: 3 files"
    assert runner._replan_feedback(history[:1]) == ""


def test_duplicate_leaf_for_the_same_function_is_refused(engine, tmp_path):
    """Observed on the first real v2 run (calc): the Task splitting parse()
    also added an evaluate() leaf, because evaluate's stub was in the same
    file, and the Task splitting evaluate() added it again."""
    from JFI.planner.nodes import add_node
    add_node(engine, "s", "architect", None, "calc component", files=["calculator.py"])
    add_node(engine, "s", "lead", 1, "calculator.py: parse and evaluate", files=["calculator.py"])
    files = ["calculator.py", "tests/test_calculator.py"]
    assert add_node(engine, "s", "task", 2, "implement parse(line) in calculator.py: split", files=files) \
        .startswith("Added")
    assert add_node(engine, "s", "task", 2, "implement evaluate(a, op, b) in calculator.py: math", files=files) \
        .startswith("Added")
    again = add_node(engine, "s", "task", 2, "implement evaluate(a, op, b) in calculator.py: the maths",
                     files=files)
    assert again.startswith("Error: node 4 already covers evaluate()")
    # splitting a leaf restates its own function: that's the normal split, not a duplicate
    assert add_node(engine, "s", "task", 4, "implement evaluate(a, op, b) in calculator.py: core", files=files) \
        .startswith("Added")


def test_words_before_a_bracket_are_not_functions(engine):
    """Observed on the stui runs: "copied verbatim (L1376-1402)" and
    "exactly (…)" were read as functions verbatim() and exactly(), and valid
    nodes were refused as duplicates of each other."""
    from JFI.planner.nodes import add_node
    add_node(engine, "s", "architect", None, "data component", files=["src/data.js"])
    add_node(engine, "s", "lead", 1, "src/data.js: copy the data", files=["src/data.js"])
    files = ["src/data.js", "tests/data.test.js"]
    for what in ("holdings", "dividends"):
        added = add_node(engine, "s", "task", 2, f"copy the {what} array verbatim (L10-20) into src/data.js",
                         files=files)
        assert added.startswith("Added"), added


def test_depends_on_errors_list_the_real_ids(engine):
    """Observed on the stui runs: ~29 depends_on errors were positions ([1],
    [1, 2]) instead of ids, and the error didn't say which ids exist."""
    from JFI.planner.nodes import add_node
    add_node(engine, "s", "architect", None, "calc component", files=["calc/ops.py"])
    add_node(engine, "s", "lead", 1, "calc/ops.py: add and sub", files=["calc/ops.py"])
    add_node(engine, "s", "task", 2, "implement add(a, b) in calc/ops.py: sum", files=["calc/ops.py"])
    answer = add_node(engine, "s", "task", 2, "implement sub(a, b) in calc/ops.py: difference",
                      files=["calc/ops.py"], depends_on=[99])
    assert "not positions" in answer and "Ids here: 3 (implement add(a, b)" in answer


def test_a_test_file_must_follow_the_runbooks_test_dir(engine):
    """Observed on the QA machine: chart-bar.test.js landed beside its source
    while test_one ran __tests__/{test_id}.test.js, so it could never run."""
    from JFI.planner.nodes import add_node
    from JFI.tool.runbook_tools import runbook_set
    runbook_set(engine, "s", "test_dir", "__tests__/")
    add_node(engine, "s", "architect", None, "charts component", files=["src/components/chart-bar.js"])
    wrong = add_node(engine, "s", "lead", 1, "src/components/chart-bar.js: the bar chart",
                     files=["src/components/chart-bar.js", "src/components/chart-bar.test.js"])
    assert wrong.startswith("Error: the runbook puts tests under __tests__/")
    right = add_node(engine, "s", "lead", 1, "src/components/chart-bar.js: the bar chart",
                     files=["src/components/chart-bar.js", "__tests__/chart-bar.test.js"])
    assert right.startswith("Added")
    runbook_set(engine, "s", "test_dir", "beside the source")
    assert add_node(engine, "s", "lead", 1, "src/views/news.js: the news view",
                    files=["src/views/news.js", "__tests__/news.test.js"]).startswith("Error")


def test_tests_at_the_repo_root_are_accepted(engine):
    """Observed on the react_counter run: test_dir "." refused every path for
    test_index.py (it became the folder "/") and the Task planner escalated."""
    from JFI.planner.nodes import add_node
    from JFI.tool.runbook_tools import runbook_set
    runbook_set(engine, "s", "test_dir", ".")
    add_node(engine, "s", "architect", None, "counter page", files=["index.html"])
    for page, path in (("index.html", "test_index.py"), ("about.html", "./test_about.py")):
        assert add_node(engine, "s", "lead", 1, f"{page} page, tested by {path}",
                        files=[page, path]).startswith("Added"), path
    refused = add_node(engine, "s", "lead", 1, "help.html page", files=["help.html", "tests/test_help.py"])
    assert refused.startswith("Error: the runbook puts tests at the repo root")


def test_notes_and_references_reach_the_next_layers_brief(engine, tmp_path):
    """The user: "add a new column ... of what should be implemented and what
    docs can be referenced for context". A reference naming a design entry
    is shown with its text, so the next layer needn't design_get it."""
    from JFI.planner.nodes import add_node
    from JFI.tool.design_tools import design_set, references_text
    design_set(engine, "s", "contract", "main->calc", "calc.evaluate(line) -> float, raises CalcError")
    add_node(engine, "s", "architect", None, "calc component", files=["calc/ops.py"],
             notes="keep evaluate pure: no printing", references=["contract:main->calc", "spec.md L10-30"])
    node = load_nodes(engine, "s")[0]
    assert node.notes == "keep evaluate pure: no printing"
    assert references_text(engine, "s", node.references) == [
        "contract:main->calc -- calc.evaluate(line) -> float, raises CalcError", "spec.md L10-30"]
    from JFI.episode.brief import ScopeAnchor
    brief = ScopeAnchor(role="lead", node_id=node.id, node=node.description, finish="finish(1, summary)",
                        notes=node.notes, references=references_text(engine, "s", node.references)).render()
    assert "notes:     keep evaluate pure" in brief and "- contract:main->calc -- calc.evaluate" in brief


def test_notes_say_what_to_do_first_in_plain_text(engine, tmp_path):
    """Observed on the react_counter run: a Task leaf's notes read "Replace the
    JFI comment at index.html L3. &lt;head&gt; holds only &lt;title&gt;..." --
    where to edit first, the purpose never, and HTML entities for <head>."""
    from JFI.planner.nodes import add_node, update_node
    refused = add_node(engine, "s", "architect", None, "page head in index.html", files=["index.html"],
                       notes="Replace the JFI comment at index.html L3. <head> holds only <title>.")
    assert refused.startswith("Error: notes must start with WHAT this item must do") and "references" in refused
    assert add_node(engine, "s", "architect", None, "Fill &lt;head&gt; in index.html: title and style",
                    files=["index.html"], done_when="&lt;title&gt; is Counter",
                    notes="The page's &lt;head&gt;: title it 'Counter'.").startswith("Added")
    node = load_nodes(engine, "s")[0]
    assert (node.description, node.done_when, node.notes) == (
        "Fill <head> in index.html: title and style", "<title> is Counter", "The page's <head>: title it 'Counter'.")
    assert update_node(engine, "s", "architect", node.id,
                       notes="Fill the JFI line at L5 with the scripts").startswith("Error: notes must start")
    from JFI.planner.prompts import RULES
    assert "Notes explain the work" in RULES and "never a node" in RULES


def test_fallback_accepts_a_node_that_already_names_one_function():
    """Observed on the first real v2 run: "Implement parse(line) in
    calculator.py" went down two more layers and came back as the same
    sentence each time."""
    from JFI.planner.judge import fallback_status
    assert fallback_status("lead", "Implement parse(line) in calculator.py: return (a, op, b)",
                           ["calculator.py", "test_calculator.py"]) == GOOD
    assert fallback_status("architect", "Implement parse(line) and evaluate(a,op,b) in calculator.py",
                           ["calculator.py"]) == BREAKDOWN
    assert fallback_status("lead", "Implement the REPL in main.py", ["main.py"]) == BREAKDOWN
    # Observed on the stui run: an Architect component that merely mentions one
    # function is still a component.
    assert fallback_status("architect", "Create src/data/dashboardData.js exporting all static data verbatim "
                           "from original L1062-1466: sectors, getTickerData(sym)",
                           ["src/data/dashboardData.js"]) == BREAKDOWN


def test_every_brief_says_which_os_shell_and_python_to_use():
    """Observed on the first real v2 run (Windows): with no platform in the
    brief, every runbook command used `python3` -- the Store alias there --
    and POSIX shell syntax that cmd.exe can't run."""
    from JFI.episode.brief import ScopeAnchor, build_system_message
    text = build_system_message(ScopeAnchor(role="architect", node_id=None, node="goal", finish="finish(0)"), "x")
    assert "Environment:" in text and "Python command:" in text


def test_the_architect_sees_the_whole_goal_and_every_review_issue(engine, tmp_path):
    """Observed on the calc benchmark: the goal and the review feedback were
    cut at 600 characters, so the goal's last requirement ("place them under
    tests/") and the second of two review issues never reached the Architect
    and were never planned."""
    from JFI.models import HistoryMessage
    goal = "Build a calculator. " * 50 + "Place the unit tests under tests/."
    feedback = "1. README missing. " * 40 + "2. Tests are not under tests/."
    add = call("add_node", description="calc component in calc/", done_when="works")
    Planner(Console([turn(add), finish(0)]), engine, "s", goal, tmp_path, lambda role: LLM(), FallbackJudge()).run()
    Planner(Console([finish(0)]), engine, "s", goal, tmp_path, lambda role: LLM(), FallbackJudge(),
            feedback=feedback).run()
    with get_session(engine) as db:
        systems = [m.content for m in db.exec(HistoryMessage.__table__.select()).all() if m.role == "system"]
    assert any("Place the unit tests under tests/." in s for s in systems)
    assert any("2. Tests are not under tests/." in s for s in systems)


def test_an_architect_that_plans_nothing_fails_planning(engine, tmp_path, monkeypatch):
    """Observed on the stui run: the Architect ran out of budget with no
    nodes, and the empty plan was reported complete -- imp then "finished"
    nothing and the run went straight to review."""
    monkeypatch.setenv("MAX_EPISODE_TURNS", "2")
    result = _planner(engine, tmp_path, Console([turn(call("list_dir"))] * 2)).run()
    assert not result.complete and "produced no plan" in result.reason and "turn_cap" in result.reason


def test_an_architect_that_runs_out_of_turns_is_continued(engine, tmp_path, monkeypatch):
    """Observed on the stui run: the Architect spent all its turns on design
    notes and added only the scaffold node before the turn cap, so most of
    the app was never planned. A continuation conversation picks up from
    what it saved instead of stopping there."""
    from JFI.models import Episode
    monkeypatch.setenv("MAX_EPISODE_TURNS", "2")
    first = [turn(call("design_set", kind="stack", key="stack", text="vite")),
             turn(call("add_node", description="scaffold the vite project at root", done_when="npm run build"))]
    second = [turn(call("add_node", description="build the holdings view in src/views/holdings.js",
                        done_when="holdings renders")), finish(0)]
    lead = [finish(1), finish(2)]
    _planner(engine, tmp_path, Console(first + second + lead)).run()
    with get_session(engine) as db:
        modes = [(e.role, e.mode, e.end_reason) for e in db.exec(Episode.__table__.select()).all()][:2]
    assert modes == [("architect", "create", "turn_cap"), ("architect", "continue", "finish")]
    assert [n.description for n in load_nodes(engine, "s") if n.parent_id is None] == [
        "scaffold the vite project at root", "build the holdings view in src/views/holdings.js"]


def test_the_architect_cannot_finish_without_the_base_later_roles_need(bare_engine, tmp_path):
    """Observed on the stui run: the plan ended with a runbook of only
    setup/dev/build/stop -- no test, test_one or e2e, which Dev's gate and the
    reviewer run -- and a stack that ruled unit tests out."""
    from JFI.models import HistoryMessage
    turns = [turn(call("add_node", description="build the calc component in calc/", done_when="works"),
                  call("design_set", kind="stack", key="stack", text="python, no unit tests")),
             finish(0),
             turn(call("design_set", kind="stack", key="stack", text="python 3.12, pytest"),
                  call("add_node", description="wire calc into main.py: read lines and start the REPL",
                       done_when="python main.py runs", files=["main.py"]),
                  *[call("runbook_set", name=n, notes="e.g. tests/test_calc.py::test_add",
                         command={"test_one": "pytest {test_id}", "script": "python {file}",
                                  "entry": "main.py"}.get(n, f"npm run {n}"))
                    for n in ("setup", "run", "test", "test_one", "build", "e2e", "script", "entry", "src_dir",
                              "test_dir", "test_naming")]),
             finish(0)] + [finish(1)] * 3
    _planner(bare_engine, tmp_path, Console(turns)).run()
    with get_session(bare_engine) as db:
        results = [m.content for m in db.exec(HistoryMessage.__table__.select()).all()
                   if m.role == "tool" and "not finished yet" in (m.content or "")]
    assert len(results) == 1
    assert ("runbook entries setup, run, test, test_one, build, e2e, script, entry, src_dir, test_dir, "
            "test_naming" in results[0])
    assert "a test framework in the stack" in results[0]


def test_the_app_entry_point_must_be_planned(engine, tmp_path):
    """Observed on the QA machine's stui run: index.html imported /src/main.js
    but no node owned it, so nothing wired the shell and views together and
    Vite failed mid-imp. The runbook's entry must be built by a plan node, or
    already be in the project."""
    from JFI.planner.loop import Planner
    from JFI.planner.nodes import add_node
    from JFI.tool.design_tools import design_set
    from JFI.tool.runbook_tools import runbook_set
    runbook_set(engine, "s", "entry", "src/main.js")
    design_set(engine, "s", "contract", "main->shell", "main.js calls initShell(root) from shell.js")
    add_node(engine, "s", "architect", None, "scaffold Vite: index.html loads /src/main.js", files=["index.html"])
    add_node(engine, "s", "architect", None, "app shell in src/components/shell.js",
             files=["src/components/shell.js"])
    planner = Planner(Console([]), engine, "s", "a dashboard", tmp_path, lambda role: LLM(), FallbackJudge())
    from JFI.episode.brief import ScopeAnchor
    finish = planner._architect_finish(ScopeAnchor(role="architect", node_id=None, node="the goal",
                                                   finish="finish(0, summary)"))

    refused = finish(0, "done")
    assert "a plan node that builds the entry point src/main.js" in refused and "depends_on" in refused
    add_node(engine, "s", "architect", None, "wire the shell and views together in src/main.js and start the app",
             files=["src/main.js"], depends_on=[2])
    assert not finish(0, "done").startswith("Error")

    runbook_set(engine, "s", "entry", "app/cli.py")
    assert "entry point app/cli.py" in finish(0, "done")
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "cli.py").write_text("print('existing app')\n")
    assert not finish(0, "done").startswith("Error"), "an entry point the project already has needs no node"
    runbook_set(engine, "s", "entry", "start the app")
    assert "the entry file in entry's command" in finish(0, "done")


def test_a_lead_that_hits_the_turn_cap_is_continued_not_sent_back(engine, tmp_path, monkeypatch):
    """Observed on the stui run: Lead hit the turn cap on a tiny view before
    adding its node, and the view went back to the Architect as "too big"."""
    architect = [turn(call("add_node", description="build the performance view in src/views/perf.js",
                           done_when="renders")), finish(0)]
    lead_first = [turn(call("list_dir"))]
    lead_again = [turn(call("add_node", description="implement src/views/perf.js", kind="code",
                            files=["src/views/perf.js"], done_when="stubs done"))]
    monkeypatch.setenv("MAX_EPISODE_TURNS", "2")
    _planner(engine, tmp_path, Console(architect + lead_first + [turn(call("list_dir"))] + lead_again
                                       + [finish(1), finish(2)] * 3)).run()
    nodes = load_nodes(engine, "s")
    assert nodes[0].redo_count == 0 and any(n.parent_id == nodes[0].id for n in nodes)


def test_a_breakdown_cut_off_after_some_nodes_is_continued_for_the_rest(engine, tmp_path, monkeypatch):
    """Observed on the stui runs: Task episodes hit the turn cap after adding
    SOME of a file's functions and were accepted as complete -- the nav,
    dividends and news files lost leaves. The conversation is continued once,
    told what it already added, so it adds only the rest."""
    from JFI.models import HistoryMessage
    monkeypatch.setenv("MAX_EPISODE_TURNS", "2")
    architect = [turn(call("add_node", description="build the calc component in calc/", done_when="works")),
                 finish(0)]
    lead_cut_off = [turn(call("add_node", description="implement calc/ops.py stubs", kind="code",
                              files=["calc/ops.py"], done_when="stubs done")),
                    turn(call("list_dir"))]
    lead_continued = [turn(call("add_node", description="implement calc/parse.py stubs", kind="code",
                                files=["calc/parse.py"], done_when="stubs done")), finish(1)]
    _planner(engine, tmp_path, Console(architect + lead_cut_off + lead_continued + [finish(2), finish(3)] * 3)).run()

    lead_nodes = [n.description for n in load_nodes(engine, "s") if n.parent_id == 1]
    assert lead_nodes == ["implement calc/ops.py stubs", "implement calc/parse.py stubs"]
    with get_session(engine) as db:
        systems = [m.content for m in db.exec(HistoryMessage.__table__.select()).all() if m.role == "system"]
    assert any("You had added these nodes under it:\n- 2: implement calc/ops.py stubs" in s for s in systems)


class _FailingLLM:
    def send_message(self, messages, tools=None):
        raise RuntimeError('Engine protocol predict stream returned an error: {"code":500,"message":"failed to decode"}')


def test_a_lead_cut_off_by_llm_errors_never_marks_its_node_good(engine, tmp_path, monkeypatch):
    """Observed on stui run 12: LM Studio returned "failed to decode" in the
    middle of the Lead episodes for three views; each ended on "error" with no
    nodes added, was accepted as GOOD ("added nothing"), and would have gone to
    Dev whole. An error says nothing about the node's size: it's retried once,
    then the node stays BREAKDOWN and planning stops."""
    from JFI.llm import retry
    from JFI.models import Episode
    monkeypatch.setattr(retry, "LLM_RETRY_DELAY_SECONDS", 0)
    architect = [turn(call("add_node", description="build the overview view in src/views/overview.js",
                           done_when="renders")), finish(0)]
    planner = Planner(Console(architect), engine, "s", "a dashboard", tmp_path,
                      lambda role: _FailingLLM() if role == "lead" else LLM(), FallbackJudge())

    result = planner.run()

    [node] = load_nodes(engine, "s")
    assert node.plan_status == BREAKDOWN
    assert not result.complete and "ended on error" in result.reason
    with get_session(engine) as db:
        leads = [e.end_reason for e in db.exec(Episode.__table__.select()).all() if e.role == "lead"]
    assert leads == ["error", "error"]


def test_a_file_belongs_to_one_lead_node(engine):
    """Observed on the stui run: the entry component's Lead added a second
    index.html node although the scaffold component's Lead had one."""
    from JFI.planner.nodes import add_node
    add_node(engine, "s", "architect", None, "scaffold the project", done_when="builds")
    add_node(engine, "s", "architect", None, "entry module", done_when="runs")
    assert add_node(engine, "s", "lead", 1, "fill index.html", files=["index.html"]).startswith("Added")
    refused = add_node(engine, "s", "lead", 2, "wire index.html to main.js", files=["src/main.js", "index.html"])
    assert refused.startswith("Error: node 3") and "already owns index.html" in refused
    # sharing only a test file is fine
    assert add_node(engine, "s", "lead", 2, "implement src/main.js", files=["src/main.js", "tests/app.test.js"]) \
        .startswith("Added")
    assert add_node(engine, "s", "lead", 1, "implement src/nav.js", files=["src/nav.js", "tests/app.test.js"]) \
        .startswith("Added")


def test_what_a_non_coding_model_sends_for_parent_and_kind_is_understood(engine):
    """Observed on the stui run with gemma: the Architect passed parent_id=0
    for top-level nodes (copying finish(0, ...)) and got "no node 0" seven
    times, and sent kind="code/artifact/section" -- the schema's own list."""
    from JFI.planner.nodes import add_node
    assert add_node(engine, "s", "architect", None, "news view", parent_id=0, kind="code/artifact/section") \
        .endswith("(top level).")
    node = load_nodes(engine, "s")[0]
    assert node.parent_id is None and node.kind == "code"
    add_node(engine, "s", "architect", None, "settings view", kind="Component")
    assert load_nodes(engine, "s")[1].kind == "component"
