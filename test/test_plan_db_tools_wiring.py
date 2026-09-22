"""run_pipeline wires get_plan/add_leaf/start_leaf/mark_leaf_done/split_leaf
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


_PLAN_DB_TOOL_NAMES = ["get_plan", "add_leaf", "start_leaf", "mark_leaf_done", "split_leaf"]


@pytest.fixture(autouse=True)
def _restore_tool_map():
    from JFI.runner import TOOL_MAP
    originals = {name: TOOL_MAP[name] for name in _PLAN_DB_TOOL_NAMES}
    yield
    TOOL_MAP.update(originals)


def test_run_pipeline_binds_plan_db_tools_to_the_sessions_own_engine():
    from JFI.runner import PHASES, TOOL_MAP, run_pipeline

    console = _FakeConsole(answers=["plan-db-session", "goal: do the thing", "s"])
    run_pipeline(console, {phase: _FakeLLM() for phase in PHASES})

    for name in _PLAN_DB_TOOL_NAMES:
        assert "not available yet" not in TOOL_MAP[name](**_dummy_args(name))


def test_the_wired_tools_actually_write_to_this_sessions_real_db():
    """Not just "rebound to *something*" -- exercises the full round trip
    through TOOL_MAP dispatch: add a leaf, start it, finish it, confirm
    get_plan reflects it, all via the SAME callables run_pipeline wired in."""
    from JFI.runner import PHASES, TOOL_MAP, run_pipeline

    console = _FakeConsole(answers=["plan-db-roundtrip", "goal: do the thing", "s"])
    run_pipeline(console, {phase: _FakeLLM() for phase in PHASES})

    assert "empty" in TOOL_MAP["get_plan"]().lower()

    add_result = TOOL_MAP["add_leaf"](phase="imp", description="Core arithmetic")
    assert "Added leaf id=" in add_result
    leaf_id = int(add_result.split("id=")[1].split(" ")[0])

    TOOL_MAP["start_leaf"](leaf_id=leaf_id)
    assert "Core arithmetic" in TOOL_MAP["get_plan"]()

    done_result = TOOL_MAP["mark_leaf_done"](leaf_id=leaf_id)
    assert f"Marked leaf id={leaf_id} done" in done_result

    plan_text = TOOL_MAP["get_plan"]()
    assert "[x]" in plan_text


def _dummy_args(tool_name: str) -> dict:
    return {
        "get_plan": {},
        "add_leaf": {"phase": "imp", "description": "placeholder"},
        "start_leaf": {"leaf_id": 999999},
        "mark_leaf_done": {"leaf_id": 999999},
        "split_leaf": {"leaf_id": 999999, "into": ["a", "b"]},
    }[tool_name]
