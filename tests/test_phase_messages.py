"""
Tests for iteration-2 item 4.2: each phase is handed ONLY its own pending items.

The Implementation system message must embed the unchecked "- [ ]"
Implementation lines (and nothing from Testing), and the Testing system message
must embed only the unchecked Testing lines. Completed "- [x]" lines are
excluded, as are items from the other section.
"""


PLAN = """# Plan
## Context and Prerequisites
- something to note

## Implementation
- [ ] 1.1 first implementation step
- [x] 1.2 second implementation step (done)
- [ ] 1.3 third implementation step

## Testing
- [ ] 2.1 first test
- [x] 2.2 second test (passed)
"""


def _system_message(manager, phase):
    """The system prompt that get_messages(phase) would send for this phase."""
    messages = manager.get_messages(phase)
    assert messages[0]["role"] == "system"
    return messages[0]["content"]


class TestImpPhaseGetsOnlyImplementationPending:
    def test_embeds_only_unchecked_implementation_items(self, manager):
        manager.plan_file.write_text(PLAN)
        msg = _system_message(manager, "imp")

        assert "- [ ] 1.1 first implementation step" in msg
        assert "- [ ] 1.3 third implementation step" in msg

    def test_excludes_completed_implementation_items(self, manager):
        manager.plan_file.write_text(PLAN)
        msg = _system_message(manager, "imp")

        assert "second implementation step (done)" not in msg

    def test_excludes_testing_items(self, manager):
        manager.plan_file.write_text(PLAN)
        msg = _system_message(manager, "imp")

        assert "first test" not in msg
        assert "second test (passed)" not in msg


class TestTestingPhaseGetsOnlyTestingPending:
    def test_embeds_only_unchecked_testing_items(self, manager):
        manager.plan_file.write_text(PLAN)
        msg = _system_message(manager, "testing")

        assert "- [ ] 2.1 first test" in msg

    def test_excludes_completed_testing_items(self, manager):
        manager.plan_file.write_text(PLAN)
        msg = _system_message(manager, "testing")

        assert "second test (passed)" not in msg

    def test_excludes_implementation_items(self, manager):
        manager.plan_file.write_text(PLAN)
        msg = _system_message(manager, "testing")

        assert "implementation step" not in msg


class TestPhaseTriggers:
    def test_imp_trigger_points_at_pending_implementation(self, manager):
        from session.simple_session_manager import get_phase_trigger

        trigger = get_phase_trigger("imp", plan_path=manager.plan_path)
        assert "Implementation" in trigger and manager.plan_path in trigger

    def test_testing_trigger_points_at_pending_testing(self, manager):
        from session.simple_session_manager import get_phase_trigger

        trigger = get_phase_trigger("testing", plan_path=manager.plan_path)
        assert "Testing" in trigger and manager.plan_path in trigger


class TestEmptyPlan:
    def test_imp_queue_noted_when_no_items(self, manager):
        msg = _system_message(manager, "imp")
        assert "(no unchecked Implementation items found)" in msg

    def test_testing_queue_noted_when_no_items(self, manager):
        msg = _system_message(manager, "testing")
        assert "(no unchecked Testing items found)" in msg


class TestIteration2Sections:
    """Items 3.2/3.3 wording: agents use the embedded queue first."""

    def test_imp_prompt_tells_agent_to_use_embedded_queue(self, manager):
        manager.plan_file.write_text(PLAN)
        msg = _system_message(manager, "imp")
        assert "Your work queue" in msg
        # The agent is told to take the FIRST item from the embedded list.
        assert "work queue (the unchecked Implementation items)" in msg

    def test_testing_prompt_still_points_at_plan_file_for_context(self, manager):
        manager.plan_file.write_text(PLAN)
        msg = _system_message(manager, "testing")
        # Testing prompt still names the plan file it should read for context.
        assert manager.plan_path in msg
        assert "Your work queue" in msg


class TestWordingUsesEmbeddedListFirst:
    """Item 3.3: both phases tell the agent to take items from the embedded
    list instead of re-scanning the plan file."""

    def test_imp_prompt_no_longer_instructs_re_scanning(self, manager):
        manager.plan_file.write_text(PLAN)
        msg = _system_message(manager, "imp")
        # Old wording ("read_file {plan_path} and take the FIRST unchecked") is gone.
        assert f"read_file {manager.plan_path} and take" not in msg
        # The loop step 1 now points at the embedded queue.
        assert "take the FIRST item from it" in msg

    def test_testing_prompt_no_longer_instructs_re_scanning(self, manager):
        manager.plan_file.write_text(PLAN)
        msg = _system_message(manager, "testing")
        # Old wording ("read_file {plan_path} and take the FIRST unchecked") is gone.
        assert f"read_file {manager.plan_path} and take" not in msg
        assert "take the FIRST item from it" in msg

    def test_one_box_per_step_rule_intact(self, manager):
        manager.plan_file.write_text(PLAN)
        for phase in ("imp", "testing"):
            msg = _system_message(manager, phase)
            # imp: "Tick exactly one box per step"; testing: "One box per step"
            assert "box per step" in msg
