"""run_pipeline wires the reviewer's read-only plan tools (get_plan, get_leaf)
to the session's own DB engine (see SimpleSessionManager.plan_db_tools()
and runner.py's _run_session), before any real LLM call happens -- same
fake-LLM-raises-immediately pattern as test_cmd_gate_session_e2e.py uses
for execute_command's gate wiring, so this is real end-to-end proof the
runner.py plumbing is correct without ever touching an actual LLM.
"""

import pytest

from JFI.manager.abstract_manager import AbstractManager


class _FakeConsole(AbstractManager):
    def __init__(self, answers):
        self.answers = list(answers)

    def safe_get_user_input(self, prompt_label="You", **kwargs):
        return self.answers.pop(0)

    def display_user(self, *a, **k): pass
    def display_assistant(self, *a, **k): pass
    def display_system(self, *a, **k): pass
    def get_user_input(self, *a, **k): return ""
    def print_agent_response(self, *a, **k): return {"content": None, "tool_calls": None}


class _FakeLLM:
    """send_message raises immediately -- run_phase bails out right after
    the TOOL_MAP rebind under test, before any real LLM work happens."""

    def send_message(self, messages, tools=None):
        raise RuntimeError("stop here — not retryable, not a real LLM call")


_PLAN_DB_TOOL_NAMES = ["get_plan", "get_leaf"]


@pytest.fixture(autouse=True)
def _restore_tool_map(monkeypatch):
    from JFI import runner
    # The planner builds its own per-role clients; make them fail the same way.
    monkeypatch.setattr(runner, "make_llm_stream", lambda prefixes: _FakeLLM())
    originals = {name: runner.TOOL_MAP[name] for name in _PLAN_DB_TOOL_NAMES}
    yield
    runner.TOOL_MAP.update(originals)


def test_run_pipeline_binds_plan_db_tools_to_the_sessions_own_engine():
    from JFI.runner import PHASES, TOOL_MAP, run_pipeline

    console = _FakeConsole(answers=["plan-db-session", "goal: do the thing", "s"])
    run_pipeline(console, {phase: _FakeLLM() for phase in PHASES})

    for name in _PLAN_DB_TOOL_NAMES:
        assert "not available yet" not in TOOL_MAP[name](**_dummy_args(name))


def test_the_wired_tools_read_this_sessions_real_db():
    """Not just "rebound to *something*": get_plan reads the SAME session's
    DB that run_pipeline opened."""
    from JFI.runner import PHASES, TOOL_MAP, run_pipeline

    console = _FakeConsole(answers=["plan-db-roundtrip", "goal: do the thing", "s"])
    run_pipeline(console, {phase: _FakeLLM() for phase in PHASES})

    assert "empty" in TOOL_MAP["get_plan"]().lower()
    assert TOOL_MAP["get_leaf"](leaf_id=999999).startswith("Error")


def _dummy_args(tool_name: str) -> dict:
    return {"get_plan": {}, "get_leaf": {"leaf_id": 999999}}[tool_name]
