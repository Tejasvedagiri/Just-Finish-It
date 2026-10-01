"""Phase 7: the reviewer and cleanup as scoped episodes (JFI.review,
laya_plan.md §7, G8). Real SQLite, a real e2e command run by the reviewer's
own finish, the model scripted at the console boundary."""
from __future__ import annotations

import json
import sys
from types import SimpleNamespace

import pytest

from JFI import runner
from JFI.manager.abstract_manager import AbstractManager
from JFI.models import Leaf, PlanEvent, RunbookEntry, SessionRecord, get_engine, get_session
from JFI.models.enums import LeafStatus, Phase
from JFI.review import Reviewer, run_cleanup
from JFI.tool.note_tools import REVIEW_REPORT, get_note, write_review_report
from JFI.tool.runbook_tools import runbook_set

PASSING = f'"{sys.executable}" -c "pass"'
FAILING = f'"{sys.executable}" -c "raise SystemExit(3)"'


class Console(AbstractManager):
    def __init__(self, turns):
        self.turns = list(turns)
        self.errors = []

    def wait_while_paused(self):
        pass

    def print_agent_response(self, response, prompt_tokens_estimate=0):
        return self.turns.pop(0) if self.turns else {"content": "(no more script)", "tool_calls": None, "usage": None}

    def display_tool_call(self, *a, **k):
        pass

    def display_tool_result(self, *a, **k):
        pass

    def display_error(self, text):
        self.errors.append(text)

    def display_system(self, *a, **k):
        pass

    def display_user(self, *a, **k):
        pass

    def display_assistant(self, *a, **k):
        pass

    def get_user_input(self, *a, **k):
        return ""

    def get_user_choice(self, *a, **k):
        return "s"


class LLM:
    def send_message(self, messages, tools=None):
        return object()


_ids = iter(range(10_000))


def turn(name, **args):
    return {"content": None, "usage": None,
            "tool_calls": [{"id": f"c{next(_ids)}", "type": "function",
                            "function": {"name": name, "arguments": json.dumps(args)}}]}


@pytest.fixture
def engine(tmp_path):
    engine = get_engine(tmp_path / ".jfi")
    with get_session(engine) as db:
        db.add(SessionRecord(session_id="s", repo_path=str(tmp_path), pipeline_version="v2"))
        for i, desc in ((1, "implement add(a, b) in calc/ops.py"), (2, "implement parse(line) in calc/parse.py")):
            db.add(Leaf(id=i, session_id="s", phase=Phase.IMP, sort_key=i * 10, description=desc, level="task",
                        plan_status="GOOD", status=LeafStatus.DONE, files=[f"calc/{desc.split('/')[-1]}"]))
        db.commit()
    return engine


def _reviewer(engine, tmp_path, turns, notes=None):
    base = {"execute_command": lambda command: "ok", "get_plan": lambda: "1. add  2. parse",
            "get_reviewer_notes": lambda: "no notes",
            "write_review_report": lambda text: write_review_report(engine, "s", text)}
    return Reviewer(Console(turns), engine, "s", tmp_path, LLM(), base)


def _leaf(engine, leaf_id):
    with get_session(engine) as db:
        return db.get(Leaf, leaf_id)


def test_a_pass_is_confirmed_by_the_e2e_itself(engine, tmp_path):
    runbook_set(engine, "s", "e2e", PASSING)
    result = _reviewer(engine, tmp_path, [turn("finish", node_id=0, summary="PASS: ran the suite")]).run()

    assert result.verdict == "pass"
    assert {_leaf(engine, i).review_status for i in (1, 2)} == {"passed"}
    with get_session(engine) as db:
        [e2e] = db.exec(RunbookEntry.__table__.select()).all()
    assert e2e.verified


def test_a_huge_command_result_is_capped_and_a_cut_short_review_continues(engine, tmp_path, monkeypatch):
    """Observed on the calc run: the reviewer's `findstr "JFI:"` over a 4.7 MB
    log came back through execute_command uncapped (99,395 characters), the
    episode ended over budget after one turn, and the reviewer's "stopped"
    ended the whole session. Every result is capped now, and a budget or
    turn-cap end gets one fresh episode."""
    from JFI.models import Episode, HistoryMessage
    from JFI.tool.result_cap import tool_result_max_chars
    monkeypatch.setenv("MAX_EPISODE_TURNS", "1")
    runbook_set(engine, "s", "e2e", PASSING)
    reviewer = _reviewer(engine, tmp_path, [turn("execute_command", command="findstr JFI: big.log"),
                                            turn("finish", node_id=0, summary="PASS: ran the e2e")])
    reviewer.base_tools["execute_command"] = lambda command: "jfi-screen.log: JFI: stub\n" * 5000

    assert reviewer.run().verdict == "pass"
    with get_session(engine) as db:
        assert [e.end_reason for e in db.exec(Episode.__table__.select()).all()] == ["turn_cap", "finish"]
        results = [m.content for m in db.exec(HistoryMessage.__table__.select()).all() if m.name == "execute_command"]
    assert len(results) == 1 and len(results[0]) < tool_result_max_chars() + 300
    assert "truncated" in results[0]


def test_a_claimed_pass_the_e2e_refutes_is_refused(engine, tmp_path):
    """"The pipeline reported success" is not evidence: the model saying PASS
    while the e2e fails must not end the review as a pass."""
    runbook_set(engine, "s", "e2e", FAILING)
    reviewer = _reviewer(engine, tmp_path, [turn("finish", node_id=0, summary="PASS"),
                                            turn("reopen_leaf", leaf_id=2, fix_note="parse('1 + 2') returns None"),
                                            turn("finish", node_id=0, summary="FIX: parse")])
    result = reviewer.run()

    assert result.verdict == "fix" and result.reopened == [2]
    leaf = _leaf(engine, 2)
    assert (leaf.status, leaf.review_status, leaf.fix_note, leaf.reopened_count, leaf.attempt_count) == (
        LeafStatus.TODO, "failed", "parse('1 + 2') returns None", 1, 0)
    assert _leaf(engine, 1).status == LeafStatus.DONE
    with get_session(engine) as db:
        assert [e.type for e in db.exec(PlanEvent.__table__.select()).all()] == ["reopen"]


def test_missing_work_becomes_a_review_report(engine, tmp_path):
    runbook_set(engine, "s", "e2e", PASSING)
    result = _reviewer(engine, tmp_path, [turn("write_review_report", text="the README was never written"),
                                          turn("finish", node_id=0, summary="MISSING: README")]).run()
    assert result.verdict == "missing"
    assert "README" in get_note(engine, "s", REVIEW_REPORT)


def test_reopen_needs_a_fix_note_and_a_real_leaf(engine, tmp_path):
    runbook_set(engine, "s", "e2e", PASSING)
    reviewer = _reviewer(engine, tmp_path, [])
    reopen = reviewer._reopen_leaf([])
    assert reopen(2, "  ").startswith("Error")
    assert reopen(99, "broken").startswith("Error")


def test_the_reviewer_loop_fixes_reopened_leaves_then_reviews_again(engine, tmp_path, monkeypatch):
    """G8: a bug in one function doesn't go through the Architect -- Dev fixes
    the re-opened leaf and the reviewer runs again."""
    verdicts = iter([SimpleNamespace(verdict="fix", reopened=[2]), SimpleNamespace(verdict="pass", reopened=[])])
    imp_runs = []
    monkeypatch.setattr("JFI.review.Reviewer.run", lambda self: next(verdicts))
    monkeypatch.setattr(runner, "_imp", lambda console, ssm, llm_for_role: imp_runs.append(1) or
                        SimpleNamespace(complete=True))
    monkeypatch.setattr(runner, "make_llm_stream", lambda prefixes: LLM())
    added, done = [], []
    ssm = SimpleNamespace(db_engine=engine, session_id="s", session_path=tmp_path / ".jfi",
                          add_message=lambda role, content: added.append(content))
    console = Console([])
    console.mark_phase_done = done.append

    assert runner._run_reviewer(console, {}, ssm)
    assert imp_runs == [1] and added == ["REVIEWER_COMPLETE"] and done == ["reviewer"]


def test_every_episode_can_load_the_optional_tools(monkeypatch):
    """load_tool is the escape hatch for tools a role rarely needs (a browser
    check, a screenshot, ask_llm). No episode used to be given them, so the
    pool was always empty and load_tool offered nothing."""
    from JFI.episode.roles import OPTIONAL_POOL
    from JFI.episode.tools import EpisodeTools

    pool = runner._pool_tools(lambda role: LLM())("dev")
    assert set(OPTIONAL_POOL) <= set(pool)
    tools = EpisodeTools("dev", {**pool, "mark_leaf_done": lambda leaf_id, summary="": "ok"})
    load = next(s for s in tools.schemas() if s["function"]["name"] == "load_tool")
    assert {"browse_webpage", "view_image", "ask_llm"} <= set(load["function"]["parameters"]["properties"]["name"]["enum"])


def test_cleanup_is_one_episode(engine, tmp_path):
    console = Console([turn("list_dir"), turn("finish", node_id=0, summary="already tidy")])
    assert run_cleanup(console, engine, "s", tmp_path, LLM(), {"execute_command": lambda command: "ok"})


def test_reopen_with_revert_rebuilds_the_leaf_from_before_it(engine, tmp_path):
    """A leaf whose approach is wrong is reopened with its files
    put back, so Dev starts from the stub instead of patching the attempt."""
    pytest.importorskip("shutil").which("git") or pytest.skip("needs git")
    from JFI.tool.checkpoint_tools import checkpoint, ensure_baseline

    (tmp_path / "calc").mkdir()
    (tmp_path / "calc" / "parse.py").write_text("def parse(line):\n    raise NotImplementedError\n")
    ensure_baseline(tmp_path, "s")
    (tmp_path / "calc" / "parse.py").write_text("def parse(line):\n    return eval(line)\n")
    with get_session(engine) as db:
        db.get(Leaf, 2).checkpoint = checkpoint(tmp_path, "s", "leaf 2: parse")
        db.commit()

    reopen = _reviewer(engine, tmp_path, [])._reopen_leaf([])
    assert reopen(1, "wrong", revert=True).startswith("Error: leaf 1 has no checkpoint")
    assert _leaf(engine, 1).status == LeafStatus.DONE, "a refused revert reopens nothing"
    done = reopen(2, "parse() uses eval: '__import__(\"os\")' runs code", revert=True)
    assert done.endswith("Reverted 1 file(s) to before the leaf: calc/parse.py.")
    assert "raise NotImplementedError" in (tmp_path / "calc" / "parse.py").read_text()
    assert _leaf(engine, 2).status == LeafStatus.TODO
