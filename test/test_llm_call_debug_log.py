"""LOG_LLM_CALL_DEBUG=1 appends every LLM request/response pair this run
makes to JFI/<session>/llm_debug.jsonl -- see runner._log_llm_call_debug
and runner.log_llm_call, wired into run_phase's turn loop right after each
successful send_message/print_agent_response pair.

Unlike SHOW_STREAM_PROMPTS (console/TUI-only, meant for watching a run
live), this persists to disk so a run can be inspected afterward -- the
motivating case is diagnosing what a model actually saw/said on a run that
already finished, without having had the flag's console output scrolling
past at the time.
"""
import json
from pathlib import Path

from JFI.manager.abstract_manager import AbstractManager

_PLAN_TWO_TURNS = """# Plan

## Implementation
- [ ] 1.1 A leaf

## Testing
- [ ] 2.1 n/a
"""


class _RecordingConsole(AbstractManager):
    def __init__(self, responses):
        self._responses = list(responses)

    def should_stop(self):
        return False

    def set_status(self, **kwargs):
        pass

    def display_rule(self, label=""):
        pass

    def display_system(self, text):
        pass

    def display_error(self, text):
        pass

    def display_user(self, *a, **k):
        pass

    def display_assistant(self, *a, **k):
        pass

    def get_user_input(self, *a, **k):
        return ""

    def mark_phase_done(self, phase):
        pass

    def wait_while_paused(self):
        pass

    def print_agent_response(self, response, prompt_tokens_estimate=0):
        return self._responses.pop(0)


class _FakeLLM:
    def send_message(self, messages, tools=None):
        return object()


def _write_plan(ssm):
    Path(ssm.plan_path).write_text(_PLAN_TWO_TURNS, encoding="utf-8")


def _log_records(ssm):
    log_path = ssm.session_path / "llm_debug.jsonl"
    if not log_path.exists():
        return []
    return [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]


def test_no_log_file_when_flag_unset(make_manager, monkeypatch):
    from JFI.runner import run_phase

    monkeypatch.delenv("LOG_LLM_CALL_DEBUG", raising=False)
    ssm = make_manager("flag-off")
    _write_plan(ssm)
    console = _RecordingConsole([{"content": "IMP_COMPLETE", "tool_calls": None}])

    run_phase(console, {"imp": _FakeLLM()}, ssm, "imp")

    assert not (ssm.session_path / "llm_debug.jsonl").exists()


def test_logs_one_record_per_turn_when_flag_set(make_manager, monkeypatch):
    from JFI.runner import run_phase

    monkeypatch.setenv("LOG_LLM_CALL_DEBUG", "1")
    ssm = make_manager("flag-on")
    _write_plan(ssm)
    console = _RecordingConsole([
        {"content": "still working", "tool_calls": None},
        {"content": "IMP_COMPLETE", "tool_calls": None},
    ])

    run_phase(console, {"imp": _FakeLLM()}, ssm, "imp")

    records = _log_records(ssm)
    assert len(records) == 2
    for record in records:
        assert record["phase"] == "imp"
        assert "timestamp" in record
        assert isinstance(record["request"]["messages"], list)
        assert record["request"]["messages"][-1]["content"] or record["request"]["messages"]
        assert "content" in record["response"]
    assert records[0]["response"]["content"] == "still working"
    assert records[1]["response"]["content"] == "IMP_COMPLETE"


def test_accepts_truthy_variants(make_manager, monkeypatch):
    from JFI.runner import run_phase

    for value in ("true", "Yes", "ON"):
        monkeypatch.setenv("LOG_LLM_CALL_DEBUG", value)
        ssm = make_manager(f"flag-{value}")
        _write_plan(ssm)
        console = _RecordingConsole([{"content": "IMP_COMPLETE", "tool_calls": None}])

        run_phase(console, {"imp": _FakeLLM()}, ssm, "imp")

        assert len(_log_records(ssm)) == 1
