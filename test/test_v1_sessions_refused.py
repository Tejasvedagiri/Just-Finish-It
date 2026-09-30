"""The v1 pipeline (tiered planner, Program Manager, testing phase) was
removed. A session created by it can't be resumed any more -- its history
expects phases that no longer exist -- so the runner refuses it by name
instead of running v2 phases over a v1 plan. export-db still reads it.
"""

from JFI.manager.abstract_manager import AbstractManager
from JFI.models import SessionRecord, get_engine, get_session
from JFI.session.history_store import append_history_to_db


class _Console(AbstractManager):
    def __init__(self, answers):
        self.answers = list(answers)
        self.errors = []

    def safe_get_user_input(self, prompt_label="You", **kwargs):
        return self.answers.pop(0) if self.answers else ""

    def display_error(self, text, *a, **k):
        self.errors.append(text)

    def display_user(self, *a, **k): pass
    def display_assistant(self, *a, **k): pass
    def display_system(self, *a, **k): pass
    def get_user_input(self, *a, **k): return ""
    def print_agent_response(self, *a, **k): return {"content": None, "tool_calls": None}


class _NoLLM:
    def send_message(self, messages, tools=None):
        raise AssertionError("a v1 session must not reach an LLM call")


def test_resuming_a_v1_session_is_refused_before_any_phase_runs(tmp_path):
    from JFI.runner import PHASES, _run_session

    engine = get_engine(tmp_path)
    with get_session(engine) as db:
        db.add(SessionRecord(session_id="old", repo_path=str(tmp_path), pipeline_version="v1"))
        db.commit()
    append_history_to_db(engine, "old", [{"role": "user", "content": "My goal is: a calculator"},
                                         {"role": "assistant", "content": "PLANNER_COMPLETE"}])

    console = _Console(answers=["old"])
    _run_session(console, {phase: _NoLLM() for phase in PHASES})

    assert len(console.errors) == 1
    assert "old v1 pipeline" in console.errors[0]
    assert "export-db" in console.errors[0]
