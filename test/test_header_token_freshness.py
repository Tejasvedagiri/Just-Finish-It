"""
Regression: the header's ctx figure (console.set_status(tokens=...)) must
refresh every turn, not only turns that happen to produce tool calls.

Before this fix, run_phase only pushed tokens= to the console inside the
"if tool_calls:" branch or on phase completion — a plain-content turn (a
model thinking out loud, an auto-nudge reply, ...) left the header showing
whatever ctx figure was last pushed, even though ssm.get_messages() had
already moved ssm.token_usage() on via compress_history().
"""

from JFI.manager.abstract_manager import AbstractManager


class _RecordingConsole(AbstractManager):
    """Just enough of AbstractManager's surface for run_phase, recording
    every set_status(...) call so tests can inspect exactly what the header
    was told at each point."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.status_calls: list[dict] = []

    def should_stop(self):
        return False

    def set_status(self, **kwargs):
        self.status_calls.append(kwargs)

    def display_rule(self, label=""):
        pass

    def display_system(self, text):
        pass

    def display_user(self, *a, **k):
        pass

    def display_assistant(self, *a, **k):
        pass

    def get_user_input(self, *a, **k):
        return ""

    def mark_phase_done(self, phase):
        pass

    def print_agent_response(self, response, prompt_tokens_estimate=0):
        return self._responses.pop(0)


class _FakeLLM:
    """send_message's return value is never iterated: print_agent_response
    is faked to hand back canned parsed turns directly."""

    def send_message(self, messages, tools=None):
        return object()


def test_tokens_pushed_to_header_on_a_plain_content_turn(make_manager):
    from JFI.runner import run_phase

    ssm = make_manager("token_fresh")
    console = _RecordingConsole([
        # Turn 1: plain content, no tool calls, not the completion phrase —
        # this is exactly the turn shape that used to skip the tokens=
        # set_status call entirely.
        {"content": "Thinking out loud, not done yet.", "tool_calls": None},
        # Turn 2: signs off, so run_phase returns instead of looping forever.
        {"content": "IMP_COMPLETE", "tool_calls": None},
    ])

    result = run_phase(console, {"imp": _FakeLLM()}, ssm, "imp")

    assert result is True

    tokens_calls = [c for c in console.status_calls if "tokens" in c]
    # One push per turn (2 turns) plus the initial push at phase start.
    assert len(tokens_calls) >= 3, (
        f"expected a tokens= push every turn, only saw {len(tokens_calls)}: {tokens_calls}"
    )
