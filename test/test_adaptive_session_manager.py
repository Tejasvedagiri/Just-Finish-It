"""
Tests for AdaptiveSessionManager: it must detect the session's task type
from the goal already in history and swap in the matching planning rules,
while leaving everything else (storage, plan parsing, message shape)
identical to SimpleSessionManager -- see adaptive_session_manager.py.
"""
from JFI.session.simple_session_manager import PLAN_FORMAT_RULES
from JFI.session.task_rules import CORE_PLAN_RULES, JAVASCRIPT_ADDENDUM, PYTHON_ADDENDUM, STORY_ADDENDUM


def _seed_goal(manager, goal_text):
    """Mirrors what _run_session actually writes: get_phase_trigger's
    planner-branch trigger message, as the first user turn."""
    manager.add_message(
        "user",
        f"My goal is: {goal_text}\n\nWrite the step-by-step plan to {manager.plan_path} now, "
        f"using the mandated '- [ ]' format.",
    )


def test_detects_python_task_type_from_goal(make_adaptive_manager):
    mgr = make_adaptive_manager()
    _seed_goal(mgr, "Build a command-line calculator application in Python.")
    assert mgr.task_type() == "python"


def test_detects_javascript_task_type_from_goal(make_adaptive_manager):
    mgr = make_adaptive_manager()
    _seed_goal(mgr, "Build a Vite + vanilla JavaScript portfolio dashboard.")
    assert mgr.task_type() == "javascript"


def test_detects_story_task_type_from_goal(make_adaptive_manager):
    mgr = make_adaptive_manager()
    _seed_goal(mgr, "Write a short story about a lighthouse keeper.")
    assert mgr.task_type() == "story"


def test_no_goal_yet_is_generic(make_adaptive_manager):
    mgr = make_adaptive_manager()
    assert mgr.task_type() == "generic"


def test_task_type_is_cached_not_recomputed(make_adaptive_manager):
    mgr = make_adaptive_manager()
    _seed_goal(mgr, "Build a command-line calculator application in Python.")
    assert mgr.task_type() == "python"
    # Mutating history after the fact must not change the cached answer --
    # detection happens once, not on every call.
    mgr.history.append({"role": "user", "content": "My goal is: Write a short story.\n\n"})
    assert mgr.task_type() == "python"


def test_plan_format_rules_python(make_adaptive_manager):
    mgr = make_adaptive_manager()
    _seed_goal(mgr, "Build a command-line calculator application in Python.")
    rules = mgr._plan_format_rules()
    assert rules == CORE_PLAN_RULES + PYTHON_ADDENDUM


def test_plan_format_rules_javascript(make_adaptive_manager):
    mgr = make_adaptive_manager()
    _seed_goal(mgr, "Build a Vite + vanilla JavaScript portfolio dashboard.")
    rules = mgr._plan_format_rules()
    assert rules == CORE_PLAN_RULES + JAVASCRIPT_ADDENDUM


def test_plan_format_rules_story(make_adaptive_manager):
    mgr = make_adaptive_manager()
    _seed_goal(mgr, "Write a short story about a lighthouse keeper.")
    assert mgr._plan_format_rules() == CORE_PLAN_RULES + STORY_ADDENDUM


def test_generic_goal_falls_back_to_existing_rules_unchanged(make_adaptive_manager):
    mgr = make_adaptive_manager()
    _seed_goal(mgr, "Summarize the attached PDF into three bullet points.")
    assert mgr._plan_format_rules() == PLAN_FORMAT_RULES


def test_phase_system_message_uses_adaptive_rules(make_adaptive_manager):
    mgr = make_adaptive_manager()
    _seed_goal(mgr, "Build a command-line calculator application in Python.")
    msg = mgr._phase_system_message("planner")
    assert "PYTHON-SPECIFIC DECOMPOSITION" in msg
    assert "JAVASCRIPT/FRONTEND-SPECIFIC" not in msg


def test_generic_goal_system_message_matches_plain_manager(console, make_manager, make_adaptive_manager):
    """No regression for an unclassifiable goal: the adaptive manager's
    system message must be byte-identical to SimpleSessionManager's own."""
    plain = make_manager("demo")
    adaptive = make_adaptive_manager("demo2")
    _seed_goal(adaptive, "Summarize the attached PDF into three bullet points.")

    plain_msg = plain._phase_system_message("planner")
    adaptive_msg = adaptive._phase_system_message("planner")
    # Both resolve to their own session's plan_path, so normalize that out
    # rather than asserting raw equality. (Context is DB-backed and pulled
    # via context_save/context_lookup tool calls now -- no per-session path
    # threaded into the message to normalize.)
    def _normalize(msg, mgr):
        return msg.replace(mgr.plan_path, "PLAN")

    assert _normalize(plain_msg, plain) == _normalize(adaptive_msg, adaptive)
