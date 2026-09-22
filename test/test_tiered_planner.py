"""Tiered planner: Architect -> Team Lead -> Journeyman -> Function Breakdown.

Covers get_system_message's planner_stage branching (simple_session_manager.py)
and runner.run_phase's default 4-stage sequence for "planner", plus the
PLANNER_SINGLE_PASS=1 escape hatch back to today's one-pass planner — see
the module docstring on get_system_message's planner_stage param and
runner.PLANNER_STAGES for the full design rationale.
"""
from __future__ import annotations

from JFI.manager.abstract_manager import AbstractManager
from JFI.runner import PLANNER_STAGES, run_phase
from JFI.session.simple_session_manager import get_system_message


class _RecordingConsole(AbstractManager):
    """Minimal AbstractManager fake — same shape as
    test_stuck_task_decomposition.py's, extended to record mark_phase_done
    and display_rule calls so a test can assert on the stage sequence."""

    def __init__(self, responses, max_turns=None):
        self._responses = list(responses)
        self.status_calls: list[dict] = []
        self.done_phases: list[str] = []
        self.rule_labels: list[str] = []
        self._max_turns = max_turns
        self._turns = 0

    def should_stop(self):
        if self._max_turns is not None and self._turns >= self._max_turns:
            return True
        return False

    def set_status(self, **kwargs):
        self.status_calls.append(kwargs)

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

    def display_tool_result(self, text):
        pass

    def get_user_input(self, *a, **k):
        return ""

    def get_user_choice(self, *a, **k):
        return "s"

    def mark_phase_done(self, phase):
        self.done_phases.append(phase)

    def wait_while_paused(self):
        pass

    def request_stop(self):
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


# ---------------------------------------------------------------------------
# get_system_message's planner_stage branching
# ---------------------------------------------------------------------------

class TestGetSystemMessagePlannerStages:
    def test_default_matches_original_single_pass_prompt(self):
        # No planner_stage arg at all (the pre-tiering call shape) must
        # produce EXACTLY today's original combined instructions —
        # regression guard for PLANNER_SINGLE_PASS and any other caller.
        without_kwarg = get_system_message("planner")
        with_none = get_system_message("planner", planner_stage=None)
        assert without_kwarg == with_none
        assert "PLANNER_COMPLETE" in without_kwarg
        assert "ARCHITECT_STAGE_COMPLETE" not in without_kwarg
        assert "TEAM_LEAD_STAGE_COMPLETE" not in without_kwarg

    def test_architect_stage_framing_and_marker(self):
        msg = get_system_message("planner", planner_stage="architect")
        assert "ARCHITECT" in msg
        assert "ARCHITECT_STAGE_COMPLETE" in msg
        assert "PLANNER_COMPLETE" not in msg  # not this stage's marker
        assert "TEAM_LEAD_STAGE_COMPLETE" not in msg

    def test_team_lead_stage_framing_and_marker(self):
        msg = get_system_message("planner", planner_stage="team_lead")
        assert "TEAM LEAD" in msg
        assert "TEAM_LEAD_STAGE_COMPLETE" in msg
        assert "ARCHITECT_STAGE_COMPLETE" not in msg

    def test_journeyman_stage_framing_marker_and_worked_examples(self):
        msg = get_system_message("planner", planner_stage="journeyman")
        assert "JOURNEYMAN" in msg
        # Journeyman is no longer the last stage -- Function Breakdown
        # follows it, so it gets its own intermediate marker now (see
        # PLANNER_STAGES' own docstring).
        assert msg.strip().endswith("JOURNEYMAN_STAGE_COMPLETE")
        assert "PLANNER_COMPLETE" not in msg  # not this stage's marker
        # The two real cases this pass exists to catch, named directly.
        assert "empty case" in msg
        assert "kill any already-running server" in msg

    def test_function_breakdown_stage_framing_and_marker(self):
        msg = get_system_message("planner", planner_stage="function_breakdown")
        assert "FUNCTION" in msg.upper()
        # Reuses the ORIGINAL marker — the last stage always does, so
        # nothing downstream needs to know intermediate stages exist (see
        # PLANNER_STAGES' own docstring).
        assert msg.strip().endswith("PLANNER_COMPLETE")
        assert "JOURNEYMAN_STAGE_COMPLETE" not in msg
        assert "ARCHITECT_STAGE_COMPLETE" not in msg

    def test_function_breakdown_targets_code_leaves_only(self):
        msg = get_system_message("planner", planner_stage="function_breakdown")
        assert "one child checkbox" in msg or "one new checkbox child" in msg
        # Non-code leaves (verification/research/docs) are explicitly out of
        # scope, not silently ignored -- the model needs to be told this or
        # it tends to "helpfully" touch everything.
        assert "OUT OF SCOPE" in msg
        assert "Testing" in msg

    def test_function_breakdown_wants_real_function_names(self):
        msg = get_system_message("planner", planner_stage="function_breakdown")
        assert "collect_news" in msg  # the worked example naming a real signature

    def test_function_breakdown_uses_context_cache_instead_of_rereading_files(self):
        """Observed in practice: this pass was re-reading full source files for
        every leaf instead of checking/recording what it already learned --
        it must be told to context_lookup first and context_save what it finds."""
        msg = get_system_message("planner", planner_stage="function_breakdown")
        assert "context_lookup" in msg
        assert "context_save" in msg
        assert "instead of re-reading" in msg or "instead of re-reading it from scratch" in msg

    def test_journeyman_forbids_single_child_parents(self):
        """Observed in practice: a parent split into exactly ONE child is
        pointless nesting, not a real decomposition — Journeyman must
        either produce 2+ children or checkbox the item itself as a leaf."""
        msg = get_system_message("planner", planner_stage="journeyman")
        assert "AT LEAST 2" in msg
        assert "pointless nesting" in msg

    def test_team_lead_forbids_single_child_parents(self):
        msg = get_system_message("planner", planner_stage="team_lead")
        assert "AT LEAST 2" in msg
        # Top-level items are never marked done directly (Architect's own
        # invariant, now enforced by mark_leaf_done rejecting a leaf with
        # children rather than a "checkbox" convention) -- the
        # single-real-subtask edge case must not contradict that.
        assert "never becomes a leaf" in msg

    def test_single_pass_planner_forbids_single_child_parents(self):
        msg = get_system_message("planner")
        assert "AT LEAST 2" in msg
        assert "pointless nesting" in msg

    def test_journeyman_points_to_the_reorder_leaf_tool(self):
        """DB-backed replacement for the old plan_renumber.py workflow --
        see JFI.tool.plan_db_tools.reorder_leaf's own docstring."""
        msg = get_system_message("planner", planner_stage="journeyman")
        assert "reorder_leaf" in msg
        assert "plan_renumber.py workflow entirely" in msg  # names the failure mode it replaces

    def test_journeyman_checks_dependency_ordering(self):
        """Observed in practice: a leaf verifying a package import was
        numbered before the leaf that installs that package."""
        msg = get_system_message("planner", planner_stage="journeyman")
        assert "verify the ported" in msg
        assert "ordering bug" in msg

    def test_journeyman_and_function_breakdown_enforce_one_leaf_per_turn(self):
        """Observed live, multiple times in one real session (see /todo.md's
        Live validation section): reasoning about many leaves' split
        decisions in one turn repeatedly blew the reasoning-output cap and
        separately hit the model server's own context-size limit, needing
        manual recovery each time. Both stages that walk the whole tree
        (Journeyman, Function Breakdown) must tell the model to decide ONE
        leaf per turn."""
        journeyman_msg = get_system_message("planner", planner_stage="journeyman")
        assert "ONE item at a time" in journeyman_msg
        assert "reasoning cap" in journeyman_msg

        fb_msg = get_system_message("planner", planner_stage="function_breakdown")
        assert "ONE leaf at a time" in fb_msg
        assert "reasoning cap" in fb_msg

    def test_journeyman_exempts_investigation_leaves_from_the_one_tool_call_test(self):
        """Observed in a real run (StockUI's portfolio-api-wiring session):
        a single "investigate the SectorPie rendering bug" leaf was
        recursively split into 30+ leaves nested 7 levels deep by applying
        the normal "could this be one tool call" atomicity test to
        open-ended diagnostic work, which has no natural one-tool-call
        bottom. Journeyman must name this as its own failure mode and
        exempt investigation-shaped leaves from the normal test."""
        msg = " ".join(get_system_message("planner", planner_stage="journeyman").split())
        assert "FIFTH failure" in msg
        assert "SectorPie rendering bug" in msg
        assert "30+ leaves nested 7 levels deep" in msg
        assert "exempt from rule 2" in msg.lower() or "are exempt from" in msg

    def test_journeyman_names_the_same_investigation_keywords_the_tool_layer_enforces(self):
        """The prompt-level guidance and the mechanical depth cap in
        JFI.tool.plan_db_tools (MAX_INVESTIGATION_DEPTH) must agree on what
        counts as investigation/diagnostic work, or the model will keep
        hitting a tool error it was never told to expect."""
        from JFI.tool.plan_db_tools import INVESTIGATION_KEYWORDS

        msg = get_system_message("planner", planner_stage="journeyman").lower()
        for keyword in INVESTIGATION_KEYWORDS:
            if keyword == "determine why":
                continue  # phrased as "determine why" verbatim below; skip exact dup check
            assert keyword in msg, f"{keyword!r} named in plan_db_tools.INVESTIGATION_KEYWORDS but missing from the Journeyman prompt"

    def test_journeyman_names_max_investigation_depth_as_a_rejection_source(self):
        """The model needs to recognize add_leaf/split_leaf's own rejection
        (see plan_db_tools._check_depth) as confirmation to stop, not a bug
        to retry around."""
        msg = get_system_message("planner", planner_stage="journeyman")
        assert "MAX_INVESTIGATION_DEPTH" in msg
        assert "REJECTED with an error" in msg

    def test_imp_phase_forbids_add_leaf_as_an_investigation_notebook(self):
        """The other half of the same real-run failure: imp itself kept
        calling add_leaf for each new diagnostic sub-step it thought up
        while investigating, instead of just doing the work and
        context_save-ing findings -- add_leaf is for genuinely new
        deliverable work discovered, never the next step of an ongoing
        investigation."""
        msg = " ".join(get_system_message("imp").split())
        assert "do NOT call add_leaf for each new thing you think of trying" in msg
        assert "context_save each real finding" in msg
        assert "genuinely NEW deliverable work" in msg

    def test_planner_stage_ignored_for_other_phases(self):
        # planner_stage only means something for phase == "planner".
        imp_msg = get_system_message("imp", planner_stage="architect")
        assert "ARCHITECT" not in imp_msg
        assert "IMP_COMPLETE" in imp_msg


# ---------------------------------------------------------------------------
# runner.run_phase: default tiered sequence
# ---------------------------------------------------------------------------

def test_run_phase_planner_walks_all_four_stages_in_order(make_manager):
    ssm = make_manager("tiered-planner")
    stages_seen: list[str | None] = []
    original_set_stage = ssm.set_planner_stage

    def _tracking(stage):
        stages_seen.append(stage)
        return original_set_stage(stage)

    ssm.set_planner_stage = _tracking

    console = _RecordingConsole([
        {"content": "ARCHITECT_STAGE_COMPLETE", "tool_calls": None},
        {"content": "TEAM_LEAD_STAGE_COMPLETE", "tool_calls": None},
        {"content": "JOURNEYMAN_STAGE_COMPLETE", "tool_calls": None},
        {"content": "PLANNER_COMPLETE", "tool_calls": None},
    ])

    result = run_phase(console, {"planner": _FakeLLM()}, ssm, "planner")

    assert result is True
    assert stages_seen == [stage for stage, _keyword, _label, _tag in PLANNER_STAGES]
    # mark_phase_done fires exactly once, after the FINAL stage — not once
    # per stage.
    assert console.done_phases == ["planner"]
    # A visible transition rule for each stage AFTER the first (the first
    # stage's own prompt already frames itself as "the first of four").
    assert any("TEAM LEAD" in label for label in console.rule_labels)
    assert any("JOURNEYMAN" in label for label in console.rule_labels)
    assert any("FUNCTION BREAKDOWN" in label for label in console.rule_labels)
    # The header/dashboard "stage" tag (see AbstractManager.set_status's
    # `stage` param) starts cleared, walks Arc -> Lead -> Journy -> Func in
    # the same order as the stages themselves, then clears back to "" once
    # the phase is done — never left showing a stale tag once run_phase
    # moves on.
    stage_updates = [c["stage"] for c in console.status_calls if "stage" in c]
    assert stage_updates == [""] + [tag for _stage, _keyword, _label, tag in PLANNER_STAGES] + [""]


def test_run_phase_planner_stops_early_if_a_stage_never_completes(make_manager):
    # Only the Architect marker is ever sent; should_stop() trips via
    # max_turns before Team Lead is ever reached — run_phase must return
    # False (stop early), never hang, and never reach later stages.
    ssm = make_manager("tiered-planner-stuck")
    stages_seen: list[str | None] = []
    original_set_stage = ssm.set_planner_stage

    def _tracking(stage):
        stages_seen.append(stage)
        return original_set_stage(stage)

    ssm.set_planner_stage = _tracking

    console = _RecordingConsole(
        [{"content": "still thinking", "tool_calls": None}],
        max_turns=3,
    )

    result = run_phase(console, {"planner": _FakeLLM()}, ssm, "planner")

    assert result is False
    assert stages_seen == ["architect"]
    assert console.done_phases == []


# ---------------------------------------------------------------------------
# PLANNER_SINGLE_PASS=1 escape hatch
# ---------------------------------------------------------------------------

def test_planner_single_pass_env_flag_skips_staging(make_manager, monkeypatch):
    monkeypatch.setenv("PLANNER_SINGLE_PASS", "1")
    ssm = make_manager("single-pass-planner")
    stages_seen: list[str | None] = []
    original_set_stage = ssm.set_planner_stage

    def _tracking(stage):
        stages_seen.append(stage)
        return original_set_stage(stage)

    ssm.set_planner_stage = _tracking

    console = _RecordingConsole([
        {"content": "PLANNER_COMPLETE", "tool_calls": None},
    ])

    result = run_phase(console, {"planner": _FakeLLM()}, ssm, "planner")

    assert result is True
    # Only ever asked for the single-pass prompt (None), never a real stage.
    assert stages_seen == [None]
    assert console.done_phases == ["planner"]
    assert not any("PLANNER STAGE" in label for label in console.rule_labels)
    # Single-pass mode gets its own stage tag ("Task" -- see
    # PLANNER_SINGLE_PASS_STAGE_TAG), not one of the tiered Arc/Lead/Journy/
    # Func tags, and it's cleared once the phase completes like the tiered path.
    stage_updates = [c["stage"] for c in console.status_calls if "stage" in c]
    assert stage_updates == ["", "Task", ""]
