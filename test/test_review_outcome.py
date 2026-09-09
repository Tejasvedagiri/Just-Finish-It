"""
Tests for the post-review outcome logic in JFI.runner:

- PASS path: no review.md present -> collect_next_iteration returns (None, None)
  and the run ends normally.
- FAIL path: a generated review.md -> the runner schedules another full
  planner → imp → testing → reviewer iteration, embedding the report content
  in the feedback so the next planner can act on it even after the file is
  cleared away (which also prevents a stale file from re-triggering loops).
"""


class _FakeConsole:
    """Just enough surface of AbstractManager for collect_next_iteration."""

    def __init__(self, queued=None):
        self.queued = list(queued or [])
        self.rules = []
        self.users = []

    def display_rule(self, label=""):
        self.rules.append(label)

    def display_user(self, text):
        self.users.append(text)

    def set_status(self, **kwargs):
        pass

    def drain_queued_input(self):
        drained = self.queued
        self.queued = []
        return drained

    def wait_for_queued_input(self):
        return None


class TestReviewOutcome:
    def test_no_review_md_means_pass_and_end(self, tmp_path):
        from JFI.runner import review_outcome

        path = str(tmp_path / "review.md")  # never created
        assert review_outcome(path) is None

    def test_review_md_present_returns_feedback_with_report(self, tmp_path):
        from JFI.runner import review_outcome

        p = tmp_path / "review.md"
        p.write_text("1. bug in foo.py:42\n2. missing tests for bar", encoding="utf-8")
        feedback = review_outcome(str(p))
        assert feedback is not None
        # The full report travels with the feedback so the next planner sees it.
        assert "bug in foo.py:42" in feedback and "missing tests for bar" in feedback

    def test_fail_path_schedules_full_iteration(self, tmp_path):
        """With a review.md present the runner returns (feedback, review_path) —
        run_pipeline turns that into another planner → imp → testing → reviewer."""
        from JFI.runner import collect_next_iteration

        p = tmp_path / "review.md"
        p.write_text("issue A", encoding="utf-8")
        console = _FakeConsole()
        feedback, review_feedback = collect_next_iteration(console, review_path=str(p))

        assert feedback is not None and "REVIEW FAILED" in feedback
        assert review_feedback == str(p)
        # A failed review takes priority: a full iteration was announced.
        assert any("ANOTHER FULL ITERATION" in r for r in console.rules)


class TestNoStaleReTrigger:
    def test_stale_review_md_is_cleared_after_scheduling(self, tmp_path):
        """Once the fail path fires, the old report is removed so it cannot
        schedule yet another iteration on its own — only a freshly written
        review.md may loop again."""
        from JFI.runner import collect_next_iteration

        p = tmp_path / "review.md"
        p.write_text("issue A", encoding="utf-8")
        console = _FakeConsole()
        feedback, _ = collect_next_iteration(console, review_path=str(p))
        assert feedback is not None and p.exists() is False  # cleared away

        # Second pass with the same (now absent) path: PASS, run ends.
        feedback2, review_feedback2 = collect_next_iteration(_FakeConsole(), review_path=str(p))
        assert feedback2 is None and review_feedback2 is None


class TestPlannerTriggerForReIteration:
    def test_planner_trigger_mentions_review_report(self):
        """When a failed-review re-iteration starts, the planner trigger points
        at the report and forbids touching already-ticked lines."""
        from JFI.session.simple_session_manager import get_phase_trigger

        msg = get_phase_trigger(
            "planner", "goal", "JFI/demo/plan.md", iteration=2, review_path="JFI/demo/review.md"
        )
        assert "JFI/demo/review.md" in msg
        assert "- [x]" in msg  # do not touch ticked lines

    def test_planner_trigger_without_review_is_plain(self):
        from JFI.session.simple_session_manager import get_phase_trigger

        msg = get_phase_trigger("planner", "goal", "JFI/demo/plan.md", iteration=2)
        assert "review" not in msg.lower()


class TestLoopCap:
    def test_max_review_iterations_is_positive(self):
        from JFI.runner import MAX_REVIEW_ITERATIONS

        assert isinstance(MAX_REVIEW_ITERATIONS, int) and MAX_REVIEW_ITERATIONS > 0


class TestReviewAndQueueCombined:
    """A failed review and queued follow-ups used to be mutually exclusive:
    collect_next_iteration returned as soon as it saw review.md, without ever
    draining the queue — anything queued during that run sat untouched until
    some later iteration happened to pass review cleanly. Both now fold into
    the SAME next iteration when both are present."""

    def test_review_failure_and_queued_requests_both_included(self, tmp_path):
        from JFI.runner import collect_next_iteration

        p = tmp_path / "review.md"
        p.write_text("issue A: broken import in foo.py", encoding="utf-8")
        console = _FakeConsole(queued=["also add a login page", "and write tests for it"])

        feedback, review_feedback = collect_next_iteration(console, review_path=str(p))

        assert feedback is not None
        assert "issue A: broken import in foo.py" in feedback
        assert "also add a login page" in feedback
        assert "and write tests for it" in feedback
        # Still correctly identified as a review-triggered iteration (loop
        # guard, planner-trigger enrichment) even though the queue also fed in.
        assert review_feedback == str(p)

    def test_queue_is_drained_even_when_review_failed(self, tmp_path):
        """Regression: the queue used to never even be checked on the fail
        path, so queued items stayed queued indefinitely."""
        from JFI.runner import collect_next_iteration

        p = tmp_path / "review.md"
        p.write_text("issue A", encoding="utf-8")
        console = _FakeConsole(queued=["do this too"])

        collect_next_iteration(console, review_path=str(p))

        assert console.queued == []  # drained, not left sitting

    def test_review_failure_alone_unaffected_by_empty_queue(self, tmp_path):
        from JFI.runner import collect_next_iteration

        p = tmp_path / "review.md"
        p.write_text("issue A", encoding="utf-8")
        console = _FakeConsole(queued=[])

        feedback, review_feedback = collect_next_iteration(console, review_path=str(p))

        assert feedback is not None and "REVIEW FAILED" in feedback
        assert "ADDITIONALLY" not in feedback  # nothing queued, no second block
        assert review_feedback == str(p)

    def test_queue_alone_still_works_when_review_passed(self, tmp_path):
        """No review.md at all — pure queued-follow-up path, unaffected."""
        from JFI.runner import collect_next_iteration

        path = str(tmp_path / "review.md")  # never created
        console = _FakeConsole(queued=["one more thing"])

        feedback, review_feedback = collect_next_iteration(console, review_path=path)

        assert feedback == "- one more thing"
        assert review_feedback is None

    def test_review_failure_with_only_exit_words_queued_still_proceeds(self, tmp_path):
        """A genuine review failure isn't silently dropped just because the
        user's only queued item happened to be an exit word."""
        from JFI.runner import collect_next_iteration

        p = tmp_path / "review.md"
        p.write_text("issue A", encoding="utf-8")
        console = _FakeConsole(queued=["exit"])

        feedback, review_feedback = collect_next_iteration(console, review_path=str(p))

        assert feedback is not None and "REVIEW FAILED" in feedback
        assert review_feedback == str(p)
