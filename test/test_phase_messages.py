"""
The goal is recorded once in history as "My goal is: <goal>"
(runner.GOAL_PREFIX), and runner._session_goal reads it back on resume and
for the planner. The per-phase trigger messages the old one-conversation
phases needed are gone: every phase now builds its own episode prompt.
"""

from JFI.runner import GOAL_PREFIX, _session_goal


def test_the_goal_message_is_read_back():
    history = [{"role": "user", "content": f"{GOAL_PREFIX} Build a CLI calculator."}]
    assert _session_goal(history) == "Build a CLI calculator."


def test_goal_is_still_read_from_sessions_that_recorded_v1s_planner_instruction():
    """Sessions created while v1 still existed recorded the goal with v1's
    planner instruction after it."""
    old = "My goal is: Build a CLI calculator.\n\nBuild the step-by-step plan now, using add_leaf/split_leaf as described."
    assert _session_goal([{"role": "user", "content": old}]) == "Build a CLI calculator."


def test_no_goal_message_means_no_goal():
    assert _session_goal([{"role": "user", "content": "USER FEEDBACK FOR ITERATION:\nadd dark mode"}]) == ""
