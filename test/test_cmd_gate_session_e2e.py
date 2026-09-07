"""run_pipeline wires execute_command behind the approval gate, bound to the
session's own context.json — before any LLM call happens."""

import pytest

from JFI.manager.abstract_manager import AbstractManager
from JFI.tool.cmd_tools import execute_command


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
    """send_message raises immediately — run_phase bails out right after the
    TOOL_MAP rebind under test, before any real LLM work happens."""

    def send_message(self, messages, tools=None):
        raise RuntimeError("stop here — not retryable, not a real LLM call")


@pytest.fixture(autouse=True)
def _restore_tool_map():
    from JFI.runner import TOOL_MAP
    original = TOOL_MAP["execute_command"]
    yield
    TOOL_MAP["execute_command"] = original


def test_run_pipeline_binds_gated_execute_command_to_session_context_cache():
    from JFI.runner import TOOL_MAP, run_pipeline

    console = _FakeConsole(answers=["gate-session", "goal: do the thing", "n"])
    run_pipeline(console, _FakeLLM())

    gated = TOOL_MAP["execute_command"]
    assert gated is not execute_command  # replaced with the gated wrapper

    # The gate reads/writes THIS session's context.json, not the fallback;
    # answering "n" (the only answer left) proves it actually prompted.
    assert gated("echo ignored") == "Command not executed (user answered No): 'echo ignored'"
