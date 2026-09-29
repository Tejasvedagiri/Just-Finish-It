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
def engine(tmp_path):
    engine = get_engine(tmp_path / ".jfi")
    with get_session(engine) as db:
        db.add(SessionRecord(session_id="s", repo_path=str(tmp_path), pipeline_version="v2"))
        db.commit()
    return engine


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


def test_redo_goes_back_to_the_creator_and_is_capped(engine, tmp_path, monkeypatch):
    """§4.7: a REDO node is re-done by the role that wrote it, only that node;
    once PLANNER_REDO_CAP is spent it's accepted and the reviewer is told."""
    monkeypatch.setenv("PLANNER_REDO_CAP", "1")

    class AlwaysRedo:
        def judge(self, nodes):
            return [Verdict(n.node_id, REDO if n.level == "architect" else GOOD, "laya", redo_reason="vague")
                    for n in nodes]

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


def test_new_sessions_record_the_pipeline_from_env(tmp_path, monkeypatch):
    engine = get_engine(tmp_path / ".jfi")
    monkeypatch.setenv("JFI_PIPELINE", "v2")
    load_metadata_from_db(engine, "new", str(tmp_path))
    monkeypatch.delenv("JFI_PIPELINE")
    load_metadata_from_db(engine, "old", str(tmp_path))
    assert session_pipeline(engine, "new") == "v2"
    assert session_pipeline(engine, "old") == "v1"
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
