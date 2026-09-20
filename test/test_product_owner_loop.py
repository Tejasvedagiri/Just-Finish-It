"""The planner<->product_owner loop: a NEW phase sitting between planning
and implementation (see PHASES in runner.py), read-only like the Reviewer
role but running before any code exists instead of after. It checks the
plan against the REAL current repo state and, if it has concerns, writes
feedback_to_plan.md — which sends the plan back to the planner for one
more pass, then back to Product Owner again, entirely before "imp" ever
starts. This is a SEPARATE, tighter loop from the reviewer's own
planner->imp->testing->reviewer restart — see _run_product_owner_loop's
own docstring.
"""
from pathlib import Path

from JFI.manager.abstract_manager import AbstractManager
from JFI.runner import (
    MAX_PRODUCT_OWNER_ITERATIONS,
    _run_product_owner_loop,
    product_owner_feedback_outcome,
)
from JFI.session.simple_session_manager import get_phase_trigger, get_system_message


# ---------------------------------------------------------------------------
# product_owner_feedback_outcome -- mirrors review_outcome's own contract
# ---------------------------------------------------------------------------

class TestProductOwnerFeedbackOutcome:
    def test_no_feedback_file_means_approved(self, tmp_path):
        path = str(tmp_path / "feedback_to_plan.md")  # never created
        assert product_owner_feedback_outcome(path) is None

    def test_feedback_file_present_returns_feedback_with_report(self, tmp_path):
        p = tmp_path / "feedback_to_plan.md"
        p.write_text("1. leaf 2.3 assumes config.py doesn't exist, it does", encoding="utf-8")
        feedback = product_owner_feedback_outcome(str(p))
        assert feedback is not None
        assert "leaf 2.3 assumes config.py doesn't exist, it does" in feedback
        assert "PRODUCT OWNER FEEDBACK" in feedback


# ---------------------------------------------------------------------------
# get_system_message / get_phase_trigger for the new phase
# ---------------------------------------------------------------------------

class TestProductOwnerSystemMessage:
    def test_mentions_feedback_path_and_marker(self):
        msg = get_system_message("product_owner", "JFI/demo/plan.md")
        assert "JFI/demo/feedback_to_plan.md" in msg
        assert msg.strip().endswith("PRODUCT_OWNER_COMPLETE")

    def test_is_read_only_like_reviewer(self):
        msg = get_system_message("product_owner", "JFI/demo/plan.md")
        assert "do NOT write or edit any deliverable code" in msg

    def test_checks_against_real_repo_not_just_the_plan(self):
        msg = get_system_message("product_owner", "JFI/demo/plan.md")
        assert "ACTUALLY EXISTS" in msg or "real current state" in msg.lower()

    def test_other_phases_dont_mention_product_owner_feedback_file(self):
        msg = get_system_message("imp", "JFI/demo/plan.md")
        assert "feedback_to_plan.md" not in msg


class TestProductOwnerTrigger:
    def test_trigger_mentions_feedback_path(self):
        msg = get_phase_trigger("product_owner", "goal", "JFI/demo/plan.md")
        assert "JFI/demo/feedback_to_plan.md" in msg

    def test_planner_trigger_mentions_po_feedback_when_given(self):
        msg = get_phase_trigger(
            "planner", "goal", "JFI/demo/plan.md", po_feedback_path="JFI/demo/feedback_to_plan.md"
        )
        assert "Product Owner" in msg
        assert "JFI/demo/feedback_to_plan.md" in msg
        assert "- [x]" not in msg or "do NOT" in msg  # doesn't ask to touch ticked lines carelessly

    def test_planner_trigger_without_po_feedback_is_plain(self):
        msg = get_phase_trigger("planner", "goal", "JFI/demo/plan.md")
        assert "Product Owner" not in msg

    def test_po_feedback_alone_switches_planner_to_update_mode(self):
        """Even at iteration=1 (no outer review loop involved), a pending PO
        feedback file must switch the planner into "update the existing
        plan" wording, not "write a plan from scratch"."""
        msg = get_phase_trigger(
            "planner", "goal", "JFI/demo/plan.md", iteration=1,
            po_feedback_path="JFI/demo/feedback_to_plan.md",
        )
        assert "Update the plan" in msg
        assert "Write the step-by-step plan" not in msg


class TestLoopCapIsPositive:
    def test_max_product_owner_iterations_is_positive(self):
        assert isinstance(MAX_PRODUCT_OWNER_ITERATIONS, int) and MAX_PRODUCT_OWNER_ITERATIONS > 0


# ---------------------------------------------------------------------------
# _run_product_owner_loop -- integration, driving real run_phase() turns
# ---------------------------------------------------------------------------

class _RecordingConsole(AbstractManager):
    def __init__(self, responses, max_turns=None):
        self._responses = list(responses)
        self.rule_labels: list[str] = []
        self._max_turns = max_turns
        self._turns = 0

    def should_stop(self):
        if self._max_turns is not None and self._turns >= self._max_turns:
            return True
        return False

    def set_status(self, **kwargs):
        pass

    def display_rule(self, label=""):
        self.rule_labels.append(label)

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
            return {"content": "still working", "tool_calls": None}
        return item() if callable(item) else item


class _FakeLLM:
    def send_message(self, messages, tools=None):
        return object()


def _write_feedback(ssm):
    """Returns a zero-arg callable _RecordingConsole can invoke directly
    (print_agent_response calls canned callables with no arguments), closing
    over `ssm` since the console itself never holds one."""
    def _do():
        (ssm.session_path / "feedback_to_plan.md").write_text(
            "1. leaf 1.1 assumes a table that already exists", encoding="utf-8"
        )
        # Product Owner always signs off with its own completion marker,
        # whether or not it had feedback -- the loop tells the two cases
        # apart by checking feedback_to_plan.md separately (see
        # product_owner_feedback_outcome).
        return {"content": "wrote feedback_to_plan.md\nPRODUCT_OWNER_COMPLETE", "tool_calls": None}
    return _do


class TestRunProductOwnerLoop:
    def test_approved_on_first_pass_runs_product_owner_once(self, make_manager):
        ssm = make_manager("po-approved")
        Path(ssm.plan_path).write_text("# Plan\n\n## Implementation\n- [ ] 1.1 x\n", encoding="utf-8")
        console = _RecordingConsole([{"content": "Product Owner: APPROVED\nPRODUCT_OWNER_COMPLETE", "tool_calls": None}], max_turns=3)

        ok = _run_product_owner_loop(console, {"product_owner": _FakeLLM(), "planner": _FakeLLM()}, ssm, "goal")

        assert ok is True
        assert any("APPROVED" in r for r in console.rule_labels)
        assert not (ssm.session_path / "feedback_to_plan.md").exists()

    def test_feedback_sends_it_back_to_planner_then_reviews_again(self, make_manager, monkeypatch):
        monkeypatch.setenv("PLANNER_SINGLE_PASS", "1")  # one PLANNER_COMPLETE marker, not 3 tiered stages
        ssm = make_manager("po-one-round")
        Path(ssm.plan_path).write_text("# Plan\n\n## Implementation\n- [ ] 1.1 x\n", encoding="utf-8")
        console = _RecordingConsole([
            _write_feedback(ssm),  # product_owner round 1: writes feedback
            {"content": "PLANNER_COMPLETE", "tool_calls": None},  # planner incorporates it
            {"content": "Product Owner: APPROVED\nPRODUCT_OWNER_COMPLETE", "tool_calls": None},  # product_owner round 2: approves
        ], max_turns=5)

        ok = _run_product_owner_loop(
            console, {"product_owner": _FakeLLM(), "planner": _FakeLLM()}, ssm, "goal",
        )

        assert ok is True
        assert any("REQUESTED CHANGES #1" in r for r in console.rule_labels)
        assert any("APPROVED" in r for r in console.rule_labels)
        # The feedback content must have reached the planner as a real message.
        assert any(
            "leaf 1.1 assumes a table that already exists" in str(m.get("content", ""))
            for m in ssm.history
        )
        assert not (ssm.session_path / "feedback_to_plan.md").exists()

    def test_feedback_file_cleared_immediately_each_round_not_batched(self, make_manager, monkeypatch):
        """Explicit requirement: the file's presence/absence must be acted
        on right away each round, never left for a later pass to notice."""
        monkeypatch.setenv("PLANNER_SINGLE_PASS", "1")  # one PLANNER_COMPLETE marker, not 3 tiered stages
        ssm = make_manager("po-clears-immediately")
        Path(ssm.plan_path).write_text("# Plan\n\n## Implementation\n- [ ] 1.1 x\n", encoding="utf-8")

        def _check_cleared_before_planner_runs():
            # By the time this (the planner's canned turn) executes, the
            # harness must have ALREADY unlinked the file from the
            # product_owner round that just ran — not deferred to later.
            assert not (ssm.session_path / "feedback_to_plan.md").exists()
            return {"content": "PLANNER_COMPLETE", "tool_calls": None}

        console = _RecordingConsole([
            _write_feedback(ssm),
            _check_cleared_before_planner_runs,
            {"content": "Product Owner: APPROVED\nPRODUCT_OWNER_COMPLETE", "tool_calls": None},
        ], max_turns=5)

        ok = _run_product_owner_loop(
            console, {"product_owner": _FakeLLM(), "planner": _FakeLLM()}, ssm, "goal"
        )
        assert ok is True

    def test_cap_reached_stops_the_run(self, make_manager, monkeypatch):
        monkeypatch.setenv("PLANNER_SINGLE_PASS", "1")  # one PLANNER_COMPLETE marker, not 3 tiered stages
        ssm = make_manager("po-cap-reached")
        Path(ssm.plan_path).write_text("# Plan\n\n## Implementation\n- [ ] 1.1 x\n", encoding="utf-8")
        # Product Owner never approves -- every round writes fresh feedback.
        responses = []
        for _ in range(MAX_PRODUCT_OWNER_ITERATIONS):
            responses.append(_write_feedback(ssm))
            responses.append({"content": "PLANNER_COMPLETE", "tool_calls": None})

        console = _RecordingConsole(responses, max_turns=len(responses) + 2)

        ok = _run_product_owner_loop(
            console, {"product_owner": _FakeLLM(), "planner": _FakeLLM()}, ssm, "goal"
        )

        assert ok is False
        assert any("LOOP CAP REACHED" in r for r in console.rule_labels)
