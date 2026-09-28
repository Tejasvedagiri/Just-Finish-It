"""Tiered planner: Arc -> Lead -> Dev -> Task Planner, walked depth-first
one top-level branch at a time (Arc is the one whole-tree pass; Lead/Dev/
Task Planner each run once PER top-level node, that node's entire sequence
resolved before the next node starts).

Covers get_system_message's planner_stage/planner_node_id branching
(simple_session_manager.py) and runner.run_phase's default per-branch
sequence for "planner", plus the PLANNER_SINGLE_PASS=1 escape hatch back to
today's one-pass planner — see the module docstring on get_system_message's
planner_stage param and runner.PLANNER_ARC_STAGE/PLANNER_NODE_STAGES for
the full design rationale (todo_v1.md §1-§3).
"""
from __future__ import annotations

from JFI.manager.abstract_manager import AbstractManager
from JFI.runner import PLANNER_ARC_STAGE, PLANNER_NODE_STAGES, PLANNER_PHASE_COMPLETE_MARKER, run_phase
from JFI.session.simple_session_manager import get_system_message
from JFI.tool.plan_db_tools import add_leaf


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


def _node_marker(base_keyword: str, node_id: int) -> str:
    """Mirrors runner._node_scoped_marker without importing a private
    helper across modules — same trivial f-string, kept in sync manually
    (see that function's own docstring)."""
    return f"{base_keyword}_NODE_{node_id}"


# ---------------------------------------------------------------------------
# get_system_message's planner_stage/planner_node_id branching
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
        assert "LEAD_STAGE_COMPLETE" not in without_kwarg

    def test_architect_stage_framing_and_marker(self):
        # Arc is the one whole-tree pass -- never node-scoped.
        msg = get_system_message("planner", planner_stage="architect")
        assert "ARCHITECT_STAGE_COMPLETE" in msg
        assert "PLANNER_COMPLETE" not in msg  # not this stage's marker
        assert "LEAD_STAGE_COMPLETE" not in msg

    def test_team_lead_stage_is_scoped_to_one_node(self):
        msg = get_system_message("planner", planner_stage="team_lead", planner_node_id=5)
        assert "LEAD" in msg
        assert "[id=5]" in msg
        assert "LEAD_STAGE_COMPLETE_NODE_5" in msg
        assert "ARCHITECT_STAGE_COMPLETE" not in msg
        # Every OTHER branch is explicitly off-limits this turn.
        assert "CLOSED to you this turn" in msg

    def test_team_lead_forbidden_from_reading_source_files(self):
        """Observed in practice: Lead, given a narrow branch (e.g. "bootstrap
        the DB directory"), read half a dozen source files (app/main.py,
        app/db.py, app/config.py, constants/paths.py, constants/server.py)
        before creating a single child — many slow turns spent on
        file-level investigation that's Dev's job, not Lead's. Lead must
        decide feature-level children from the description/get_leaf alone."""
        msg = get_system_message("planner", planner_stage="team_lead", planner_node_id=5)
        assert "Do NOT read source files at this stage" in msg
        assert "no read_file" in msg
        assert "Dev's job" in msg

    def test_journeyman_dev_stage_framing_marker_and_worked_examples(self):
        msg = get_system_message("planner", planner_stage="journeyman", planner_node_id=5)
        assert "DEV" in msg
        assert "[id=5]" in msg
        assert msg.strip().endswith("DEV_STAGE_COMPLETE_NODE_5")
        assert "PLANNER_COMPLETE" not in msg  # not this stage's marker
        # The two real cases this pass exists to catch, named directly.
        assert "empty case" in msg
        assert "kill any already-running server" in msg
        # §2: name the actual file, not just "atomic" in the abstract.
        assert "app/routes/summary.py" in msg

    def test_function_breakdown_task_planner_stage_framing_and_marker(self):
        msg = get_system_message("planner", planner_stage="function_breakdown", planner_node_id=5)
        assert "Task Planner" in msg
        assert "[id=5]" in msg
        assert msg.strip().endswith("TICKETS_STAGE_COMPLETE_NODE_5")
        assert "DEV_STAGE_COMPLETE" not in msg
        assert "ARCHITECT_STAGE_COMPLETE" not in msg
        # Bare PLANNER_COMPLETE must NEVER appear in a per-node stage's own
        # prompt -- runner.py reserves it exclusively for the synthetic
        # whole-phase-done marker appended once every branch clears every
        # stage (see run_phase). If a per-node stage ever asked for the
        # bare marker, the model saying it would falsely end the ENTIRE
        # depth-first walk after just one branch.
        assert "PLANNER_COMPLETE" not in msg

    def test_function_breakdown_no_longer_excludes_non_code_leaves(self):
        """§3: the old "OUT OF SCOPE" exclusion for non-code leaves is gone
        -- every leaf, code-writing or not, gets reduced to one
        mechanically-executable ticket now."""
        msg = get_system_message("planner", planner_stage="function_breakdown", planner_node_id=5)
        assert "OUT OF SCOPE" not in msg
        assert "one child checkbox" in msg or "one new checkbox child" in msg or "one child per function" in msg
        # Worked examples of a correct NON-code ticket, not just code ones.
        assert "uv init" in msg
        assert "pyproject.toml" in msg

    def test_function_breakdown_wants_real_function_names(self):
        msg = get_system_message("planner", planner_stage="function_breakdown", planner_node_id=5)
        assert "collect_news" in msg  # the worked example naming a real signature

    def test_function_breakdown_uses_context_cache_instead_of_rereading_files(self):
        """Observed in practice: this pass was re-reading full source files for
        every leaf instead of checking/recording what it already learned --
        it must be told to context_lookup first and context_save what it finds."""
        msg = get_system_message("planner", planner_stage="function_breakdown", planner_node_id=5)
        assert "context_lookup" in msg
        assert "context_save" in msg
        assert "instead of re-reading" in msg or "instead of re-reading it from scratch" in msg

    def test_journeyman_forbids_single_child_parents(self):
        """Observed in practice: a parent split into exactly ONE child is
        pointless nesting, not a real decomposition — Dev must either
        produce 2+ children or checkbox the item itself as a leaf."""
        msg = get_system_message("planner", planner_stage="journeyman", planner_node_id=5)
        assert "AT LEAST 2" in msg
        assert "pointless nesting" in msg

    def test_team_lead_forbids_single_child_parents(self):
        msg = get_system_message("planner", planner_stage="team_lead", planner_node_id=5)
        assert "AT LEAST 2" in msg
        # Top-level items are never marked done directly (Arc's own
        # invariant, enforced by mark_leaf_done rejecting a leaf with
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
        msg = get_system_message("planner", planner_stage="journeyman", planner_node_id=5)
        assert "reorder_leaf" in msg
        assert "plan_renumber.py workflow entirely" in msg  # names the failure mode it replaces

    def test_journeyman_checks_dependency_ordering(self):
        """Observed in practice: a leaf verifying a package import was
        numbered before the leaf that installs that package."""
        msg = get_system_message("planner", planner_stage="journeyman", planner_node_id=5)
        assert "verify the ported" in msg
        assert "ordering bug" in msg

    def test_journeyman_and_function_breakdown_enforce_one_leaf_per_turn(self):
        """Observed live, multiple times in one real session (see /todo.md's
        Live validation section): reasoning about many leaves' split
        decisions in one turn repeatedly blew the reasoning-output cap and
        separately hit the model server's own context-size limit, needing
        manual recovery each time. Both stages that walk a branch (Dev,
        Task Planner) must tell the model to decide ONE leaf per turn."""
        dev_msg = get_system_message("planner", planner_stage="journeyman", planner_node_id=5)
        assert "ONE item at a time" in dev_msg
        assert "reasoning cap" in dev_msg

        tp_msg = get_system_message("planner", planner_stage="function_breakdown", planner_node_id=5)
        assert "ONE leaf at a time" in tp_msg
        assert "reasoning cap" in tp_msg

    def test_journeyman_exempts_investigation_leaves_from_the_one_tool_call_test(self):
        """Observed in a real run (StockUI's portfolio-api-wiring session):
        a single "investigate the SectorPie rendering bug" leaf was
        recursively split into 30+ leaves nested 7 levels deep by applying
        the normal "could this be one tool call" atomicity test to
        open-ended diagnostic work, which has no natural one-tool-call
        bottom. Dev must name this as its own failure mode and exempt
        investigation-shaped leaves from the normal test."""
        msg = " ".join(get_system_message("planner", planner_stage="journeyman", planner_node_id=5).split())
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

        msg = get_system_message("planner", planner_stage="journeyman", planner_node_id=5).lower()
        for keyword in INVESTIGATION_KEYWORDS:
            if keyword == "determine why":
                continue  # phrased as "determine why" verbatim below; skip exact dup check
            assert keyword in msg, f"{keyword!r} named in plan_db_tools.INVESTIGATION_KEYWORDS but missing from the Dev prompt"

    def test_journeyman_names_max_investigation_depth_as_a_rejection_source(self):
        """The model needs to recognize add_leaf/split_leaf's own rejection
        (see plan_db_tools._check_depth) as confirmation to stop, not a bug
        to retry around."""
        msg = get_system_message("planner", planner_stage="journeyman", planner_node_id=5)
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
# runner.run_phase: depth-first per-branch sequence (todo_v1.md §1)
# ---------------------------------------------------------------------------

def test_run_phase_planner_walks_lead_dev_tickets_depth_first_per_node(make_manager):
    """Arc runs once, then each top-level node's ENTIRE Lead -> Dev ->
    Task Planner sequence resolves before the next node's Lead pass ever
    starts -- never Lead-for-every-node, then Dev-for-every-node."""
    ssm = make_manager("tiered-planner")
    # Simulate Arc having already produced two top-level nodes (ids 1, 2)
    # -- the fake console below never executes a real add_leaf tool call,
    # so this stands in for what Arc's own turn would have done.
    add_leaf(ssm.db_engine, ssm.session_id, "imp", "First top-level")
    add_leaf(ssm.db_engine, ssm.session_id, "imp", "Second top-level")

    calls_seen: list[tuple] = []
    original_set_stage = ssm.set_planner_stage
    original_set_node = ssm.set_planner_node

    def _track_stage(stage):
        return original_set_stage(stage)

    def _track_node(node_id):
        if node_id is not None:
            calls_seen.append((ssm._planner_stage, node_id))
        return original_set_node(node_id)

    ssm.set_planner_stage = _track_stage
    ssm.set_planner_node = _track_node

    console = _RecordingConsole([
        {"content": "ARCHITECT_STAGE_COMPLETE", "tool_calls": None},
        {"content": _node_marker("LEAD_STAGE_COMPLETE", 1), "tool_calls": None},
        {"content": _node_marker("DEV_STAGE_COMPLETE", 1), "tool_calls": None},
        {"content": _node_marker("TICKETS_STAGE_COMPLETE", 1), "tool_calls": None},
        {"content": _node_marker("LEAD_STAGE_COMPLETE", 2), "tool_calls": None},
        {"content": _node_marker("DEV_STAGE_COMPLETE", 2), "tool_calls": None},
        {"content": _node_marker("TICKETS_STAGE_COMPLETE", 2), "tool_calls": None},
    ])

    result = run_phase(console, {"planner": _FakeLLM()}, ssm, "planner")

    assert result is True
    # Node 1's entire branch (all 3 stages) fully resolved BEFORE node 2's
    # Lead pass ever starts -- the actual point of todo_v1.md §1.
    assert calls_seen == [
        ("team_lead", 1), ("journeyman", 1), ("function_breakdown", 1),
        ("team_lead", 2), ("journeyman", 2), ("function_breakdown", 2),
    ]
    # mark_phase_done fires exactly once, after EVERY node/stage -- not
    # once per node and not once per stage.
    assert console.done_phases == ["planner"]
    # A visible transition rule naming each node for every per-node stage.
    assert any("LEAD" in label and "node 1" in label for label in console.rule_labels)
    assert any("DEV" in label and "node 2" in label for label in console.rule_labels)
    # The synthetic phase-complete marker landed in history WITHOUT ever
    # being asked of the model (no per-node stage's own prompt ever says
    # bare PLANNER_COMPLETE -- see test_function_breakdown_task_planner_
    # stage_framing_and_marker above).
    assert any(
        m.get("role") == "assistant" and m.get("content") == PLANNER_PHASE_COMPLETE_MARKER
        for m in ssm.history
    )


def test_run_phase_planner_stops_early_if_a_stage_never_completes(make_manager):
    # Only the Arc marker is ever sent; should_stop() trips via max_turns
    # before Lead is ever reached -- run_phase must return False (stop
    # early), never hang, and never reach a per-node stage.
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


def test_run_phase_planner_resumes_after_the_last_completed_node_stage(make_manager):
    """A resumed session whose history already has Arc's marker plus
    node 1's ENTIRE sequence (Lead/Dev/Task Planner all done for node 1
    specifically) must pick up at node 1's... no, node 2's Lead pass, not
    re-run Arc or re-touch node 1 -- self._planner_stage/_planner_node_id
    are in-memory only, so this has to come from scanning history the same
    way get_remaining_phases does, just per (node, stage) now."""
    ssm = make_manager("resumed-tiered-planner")
    add_leaf(ssm.db_engine, ssm.session_id, "imp", "First top-level")
    add_leaf(ssm.db_engine, ssm.session_id, "imp", "Second top-level")
    ssm.history.extend([
        {"role": "user", "content": "start"},
        {"role": "assistant", "content": "ARCHITECT_STAGE_COMPLETE", "tool_calls": None},
        {"role": "assistant", "content": _node_marker("LEAD_STAGE_COMPLETE", 1), "tool_calls": None},
        {"role": "assistant", "content": _node_marker("DEV_STAGE_COMPLETE", 1), "tool_calls": None},
        {"role": "assistant", "content": _node_marker("TICKETS_STAGE_COMPLETE", 1), "tool_calls": None},
    ])

    calls_seen: list[tuple] = []
    original_set_node = ssm.set_planner_node

    def _track_node(node_id):
        if node_id is not None:
            calls_seen.append((ssm._planner_stage, node_id))
        return original_set_node(node_id)

    ssm.set_planner_node = _track_node

    console = _RecordingConsole([
        {"content": _node_marker("LEAD_STAGE_COMPLETE", 2), "tool_calls": None},
        {"content": _node_marker("DEV_STAGE_COMPLETE", 2), "tool_calls": None},
        {"content": _node_marker("TICKETS_STAGE_COMPLETE", 2), "tool_calls": None},
    ])

    result = run_phase(console, {"planner": _FakeLLM()}, ssm, "planner")

    assert result is True
    # Node 1 never re-entered at all -- only node 2's 3 stages ran.
    assert calls_seen == [("team_lead", 2), ("journeyman", 2), ("function_breakdown", 2)]
    assert console.done_phases == ["planner"]
    # No transition rule mentions node 1 this run -- it was never
    # (re-)touched.
    assert not any("node 1" in label for label in console.rule_labels)
    assert any("node 2" in label for label in console.rule_labels)


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
    # PLANNER_SINGLE_PASS_STAGE_TAG), not one of the tiered Arc/Lead/Dev/
    # Tickets tags, and it's cleared once the phase completes like the
    # tiered path.
    stage_updates = [c["stage"] for c in console.status_calls if "stage" in c]
    assert stage_updates == ["", "Task", ""]


def test_planner_arc_and_node_stage_constants_are_consistent():
    """PLANNER_ARC_STAGE/PLANNER_NODE_STAGES are the single source of truth
    both runner.py's own loop and this test file's _node_marker helper
    have to agree with -- a regression guard against the two silently
    drifting apart."""
    arc_stage, arc_keyword, _label, arc_tag = PLANNER_ARC_STAGE
    assert arc_stage == "architect"
    assert arc_keyword == "ARCHITECT_STAGE_COMPLETE"
    assert arc_tag == "Arc"

    stage_names = [stage for stage, _kw, _label, _tag in PLANNER_NODE_STAGES]
    assert stage_names == ["team_lead", "journeyman", "function_breakdown"]
    tags = [tag for _stage, _kw, _label, tag in PLANNER_NODE_STAGES]
    assert tags == ["Lead", "Dev", "Tickets"]
