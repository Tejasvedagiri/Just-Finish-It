"""run_phase forces a plan leaf to split itself up once it's stayed the
"current task" too long — see runner._stuck_task_directive and the
stuck_task_title/stuck_since/stuck_tokens tracking near the top of
run_phase's turn loop.

Motivating case observed in practice: a single Implementation leaf that
turned out to hide a diagnose-then-fix (not just a quick check) ran for
hours and 700k+ tokens of re-sent context without ever being ticked or
split into smaller sub-leaves, spawning a pile of throwaway scripts along
the way. Either threshold (TASK_STUCK_TIME_LIMIT_SECONDS,
TASK_STUCK_TOKEN_LIMIT) firing injects an AUTO-RECTIFY message telling the
model to split the leaf (3.1 -> 3.1.1, 3.1.2, 3.1.3, ...) instead of
continuing to grind on it.
"""
from pathlib import Path

from JFI.manager.abstract_manager import AbstractManager

_PLAN_ONE_STUCK_LEAF = """# Plan

## Context and Prerequisites
- n/a

## Implementation
- [ ] 3.1 A leaf that never gets ticked in these tests

## Testing
- [ ] 4.1 n/a
"""


class _RecordingConsole(AbstractManager):
    """Minimal AbstractManager fake — same shape as
    test_header_token_freshness.py's, extended with should_stop()'s
    optional call-count cap so a test can bound the turn loop."""

    def __init__(self, responses, max_turns=None):
        self._responses = list(responses)
        self.status_calls: list[dict] = []
        self.task_token_calls: list[tuple] = []
        self._max_turns = max_turns
        self._turns = 0

    def should_stop(self):
        if self._max_turns is not None and self._turns >= self._max_turns:
            return True
        return False

    def set_status(self, **kwargs):
        self.status_calls.append(kwargs)

    def record_task_tokens(self, task, tokens, started_at=None, ended_at=None, phase=None):
        self.task_token_calls.append((task, tokens))

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
        self._turns += 1
        item = self._responses.pop(0) if self._responses else None
        if item is None:
            # Ran out of canned turns (shouldn't happen if max_turns is
            # sized right) — a harmless plain-content turn so the loop can
            # still exit cleanly via should_stop() rather than hanging.
            return {"content": "still working", "tool_calls": None}
        # A callable can perform a side effect (e.g. ticking a plan
        # checkbox) before handing back its actual canned turn — this still
        # goes through this one method, so self._turns keeps incrementing
        # (a full override of print_agent_response would silently stop
        # counting turns and hang should_stop() forever).
        return item() if callable(item) else item


class _FakeLLM:
    def send_message(self, messages, tools=None):
        return object()


def _write_stuck_plan(ssm):
    Path(ssm.plan_path).write_text(_PLAN_ONE_STUCK_LEAF, encoding="utf-8")


def test_no_stuck_directive_when_under_both_thresholds(make_manager, monkeypatch):
    from JFI.runner import run_phase

    monkeypatch.setenv("TASK_STUCK_TIME_LIMIT_SECONDS", "300")
    monkeypatch.setenv("TASK_STUCK_TOKEN_LIMIT", "10000000")

    ssm = make_manager("plenty-of-room")
    _write_stuck_plan(ssm)
    console = _RecordingConsole([
        {"content": "still working on it", "tool_calls": None},
        {"content": "still working on it", "tool_calls": None},
        {"content": "IMP_COMPLETE", "tool_calls": None},
    ])

    run_phase(console, {"imp": _FakeLLM()}, ssm, "imp")

    assert not any(
        "AUTO-RECTIFY: the current task has stayed the same" in str(m.get("content", ""))
        for m in ssm.history
    )


def test_stuck_directive_fires_once_token_threshold_crossed(make_manager, monkeypatch):
    from JFI.runner import run_phase

    monkeypatch.setenv("TASK_STUCK_TIME_LIMIT_SECONDS", "300")  # time won't fire in a fast test
    monkeypatch.setenv("TASK_STUCK_TOKEN_LIMIT", "1")  # trips on the very first turn's tokens

    ssm = make_manager("stuck-on-tokens")
    _write_stuck_plan(ssm)
    console = _RecordingConsole(
        [{"content": "still working, not done", "tool_calls": None} for _ in range(3)],
        max_turns=3,
    )

    run_phase(console, {"imp": _FakeLLM()}, ssm, "imp")

    directives = [
        m for m in ssm.history
        if "AUTO-RECTIFY: the current task has stayed the same" in str(m.get("content", ""))
    ]
    assert directives, "expected a stuck-task directive once the token threshold was crossed"
    assert "3.1 A leaf that never gets ticked" in directives[0]["content"]
    assert "3.1.1" in directives[0]["content"] and "3.1.2" in directives[0]["content"]


def test_stuck_directive_resets_when_task_changes(make_manager, monkeypatch):
    """Ticking the leaf (a new current_task) must reset the window — no
    directive should fire just because the OLD leaf had accumulated tokens
    before it was completed."""
    from JFI.runner import run_phase

    monkeypatch.setenv("TASK_STUCK_TIME_LIMIT_SECONDS", "300")
    monkeypatch.setenv("TASK_STUCK_TOKEN_LIMIT", "1")

    ssm = make_manager("resets-on-progress")
    Path(ssm.plan_path).write_text(
        "# Plan\n\n## Implementation\n"
        "- [ ] 1.1 First leaf\n"
        "- [ ] 1.2 Second leaf\n\n## Testing\n- [ ] 2.1 n/a\n",
        encoding="utf-8",
    )

    def _tick_first_leaf():
        text = Path(ssm.plan_path).read_text(encoding="utf-8")
        Path(ssm.plan_path).write_text(text.replace("- [ ] 1.1", "- [x] 1.1"), encoding="utf-8")
        return {"content": "ticked 1.1", "tool_calls": None}

    console = _RecordingConsole(
        [_tick_first_leaf, {"content": "IMP_COMPLETE", "tool_calls": None}], max_turns=5
    )

    run_phase(console, {"imp": _FakeLLM()}, ssm, "imp")

    # Only one turn ever happened on leaf 1.1 (it was ticked immediately),
    # so even a token_limit of 1 must not have had a chance to fire yet
    # for leaf 1.2 (which becomes current only on the turn after).
    directives = [
        m for m in ssm.history
        if "AUTO-RECTIFY: the current task has stayed the same" in str(m.get("content", ""))
    ]
    assert not directives


def test_task_transition_records_the_finished_leafs_token_cost(make_manager, monkeypatch):
    """Feeds the fleet dashboard's tasks-vs-tokens heatmap: when a leaf's
    box gets ticked (current_task changes), the JUST-FINISHED leaf's
    accumulated token cost must be handed to
    console.record_task_tokens BEFORE the counter resets for the next
    leaf — see runner._drive_turn_loop."""
    from JFI.runner import run_phase

    ssm = make_manager("records-task-tokens")
    Path(ssm.plan_path).write_text(
        "# Plan\n\n## Implementation\n"
        "- [ ] 1.1 First leaf\n"
        "- [ ] 1.2 Second leaf\n\n## Testing\n- [ ] 2.1 n/a\n",
        encoding="utf-8",
    )

    def _tick_first_leaf():
        text = Path(ssm.plan_path).read_text(encoding="utf-8")
        Path(ssm.plan_path).write_text(text.replace("- [ ] 1.1", "- [x] 1.1"), encoding="utf-8")
        return {"content": "ticked 1.1", "tool_calls": None}

    def _tick_second_leaf():
        text = Path(ssm.plan_path).read_text(encoding="utf-8")
        Path(ssm.plan_path).write_text(text.replace("- [ ] 1.2", "- [x] 1.2"), encoding="utf-8")
        return {"content": "ticked 1.2", "tool_calls": None}

    console = _RecordingConsole(
        [_tick_first_leaf, _tick_second_leaf, {"content": "IMP_COMPLETE", "tool_calls": None}],
        max_turns=6,
    )

    run_phase(console, {"imp": _FakeLLM()}, ssm, "imp")

    # First call is always the empty starting title (no leaf finished yet) —
    # ignore it and check the two real leaf transitions that followed.
    real_calls = [c for c in console.task_token_calls if c[0]]
    assert len(real_calls) == 2
    assert real_calls[0][0] == "1.1 First leaf"
    assert real_calls[1][0] == "1.2 Second leaf"
    # Leaf 1.1 is the very first turn of the whole phase: token_usage()
    # reflects only prior conversation HISTORY (never the system prompt,
    # added separately per get_messages), and there IS no prior history
    # yet on turn one — so its recorded cost is legitimately 0, not a bug.
    # By leaf 1.2's turn, 1.1's own response has been appended to history,
    # so its cost must be real and positive.
    assert real_calls[0][1] == 0
    assert real_calls[1][1] > 0


def test_stuck_directive_fires_for_adaptive_session_manager_too(make_adaptive_manager, monkeypatch):
    """run_phase's stuck-task tracking only calls SessionManager-interface
    methods (current_task_title, add_message, get_messages, ...) —
    AdaptiveSessionManager doesn't override any of them (only
    _plan_format_rules), but this is the manager actually driving real
    sessions (SESSION_MANAGER=adaptive), so it earns its own direct check
    rather than trusting that inheritance alone."""
    from JFI.runner import run_phase

    monkeypatch.setenv("TASK_STUCK_TIME_LIMIT_SECONDS", "300")
    monkeypatch.setenv("TASK_STUCK_TOKEN_LIMIT", "1")

    ssm = make_adaptive_manager("stuck-adaptive")
    _write_stuck_plan(ssm)
    console = _RecordingConsole(
        [{"content": "still working, not done", "tool_calls": None} for _ in range(3)],
        max_turns=3,
    )

    run_phase(console, {"imp": _FakeLLM()}, ssm, "imp")

    assert any(
        "AUTO-RECTIFY: the current task has stayed the same" in str(m.get("content", ""))
        for m in ssm.history
    )


def test_stuck_task_tracking_ignores_phases_without_a_task_queue(make_manager, monkeypatch):
    """planner/reviewer have no per-leaf checklist (current_task_title
    returns None/"") — the stuck-task check must be a no-op there, never
    dividing by a task title that doesn't exist."""
    from JFI.runner import run_phase

    monkeypatch.setenv("TASK_STUCK_TIME_LIMIT_SECONDS", "300")
    monkeypatch.setenv("TASK_STUCK_TOKEN_LIMIT", "1")
    # This test is about stuck-task tracking, not planner staging — pin the
    # single-pass planner (see test_tiered_planner.py for stage coverage) so
    # the 3 canned turns below map onto one PLANNER_COMPLETE marker, not the
    # tiered default's 3 separate stage markers.
    monkeypatch.setenv("PLANNER_SINGLE_PASS", "1")

    ssm = make_manager("planner-phase")
    console = _RecordingConsole([
        {"content": "still planning", "tool_calls": None},
        {"content": "still planning", "tool_calls": None},
        {"content": "PLANNER_COMPLETE", "tool_calls": None},
    ])

    run_phase(console, {"planner": _FakeLLM()}, ssm, "planner")

    assert not any(
        "AUTO-RECTIFY: the current task has stayed the same" in str(m.get("content", ""))
        for m in ssm.history
    )
