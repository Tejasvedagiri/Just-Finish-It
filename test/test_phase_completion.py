"""Tests for phase completion detection (iteration 3, step 5.3).

Covers the module-level `phase_completed(content, phase)` marker detector and the
manager's `get_remaining_phases(all_phases)` slice-of-history behaviour.
"""

from __future__ import annotations

import pytest

from JFI.session.simple_session_manager import SimpleSessionManager, phase_completed


class FakeConsole:
    def print(self, *args, **kwargs):  # noqa: A003 - mirrors the real API
        pass


@pytest.fixture()
def manager(tmp_path, monkeypatch):
    monkeypatch.setenv("JFI_SESSIONS_DIR", str(tmp_path))
    return SimpleSessionManager(FakeConsole(), "phase-test")


# ---------------------------------------------------------------------------
# phase_completed(content, phase)
# ---------------------------------------------------------------------------

class TestPhaseCompleted:
    @pytest.mark.parametrize(
        ("content", "phase"),
        [
            ("PLANNER_COMPLETE", "planner"),
            ("\nIMP_COMPLETE\n", "imp"),
            ("TESTING_COMPLETE", "testing"),
            ("REVIEWER_COMPLETE!", "reviewer"),
        ],
    )
    def test_standalone_marker_is_detected(self, content: str, phase: str) -> None:
        assert phase_completed(content, phase) is True

    @pytest.mark.parametrize(
        ("content", "phase"),
        [
            ("The plan is in .JFI/session/plan.md.", "planner"),
            ("PLANNER_COMPLETE!", "reviewer"),  # right marker, wrong phase
            ("done\nTESTING_COMPLETE", "imp"),   # other phases' markers don't count
        ],
    )
    def test_no_marker_returns_false(self, content: str, phase: str) -> None:
        assert phase_completed(content, phase) is False

    def test_empty_content_is_false(self) -> None:
        assert phase_completed("", "imp") is False
        assert phase_completed(None, "imp") is False  # type: ignore[arg-type]

    @pytest.mark.parametrize(
        ("content", "phase"),
        [
            ("- [x] 3.4 Add tests ... then run it to green\nIMP_COMPLETE", "imp"),
            ("> IMP_COMPLETE", "imp"),              # blockquote prefix tolerated
            ("* `TESTING_COMPLETE` *", "testing"),  # markdown decoration tolerated
        ],
    )
    def test_marker_with_markdown_decorations_is_detected(self, content: str, phase: str) -> None:
        assert phase_completed(content, phase) is True

    @pytest.mark.parametrize(
        ("content", "phase"),
        [
            ("The marker IMP_COMPLETE appears here.", "imp"),  # prose on the line
            ("IMP_COMPLETE and done.", "imp"),                  # trailing text on the line
        ],
    )
    def test_marker_embedded_in_prose_is_not_detected(self, content: str, phase: str) -> None:
        assert phase_completed(content, phase) is False


# ---------------------------------------------------------------------------
# SimpleSessionManager.get_remaining_phases(all_phases)
# ---------------------------------------------------------------------------

class TestGetRemainingPhases:
    def _history_with(self, *assistant_texts: str):
        return [{"role": "assistant", "content": text} for text in assistant_texts]

    def test_no_history_returns_everything(self, manager) -> None:
        phases = ["planner", "reviewer", "imp", "testing"]
        assert manager.get_remaining_phases(phases) == phases

    def test_first_completed_phase_is_dropped(self, manager) -> None:
        manager.history = self._history_with("PLANNER_COMPLETE")
        remaining = manager.get_remaining_phases(["planner", "reviewer", "imp", "testing"])
        assert remaining == ["reviewer", "imp", "testing"]

    def test_completed_prefix_is_dropped(self, manager) -> None:
        manager.history = self._history_with("PLANNER_COMPLETE", "REVIEWER_COMPLETE")
        remaining = manager.get_remaining_phases(["planner", "reviewer", "imp", "testing"])
        assert remaining == ["imp", "testing"]

    def test_out_of_order_completions_slice_from_first_missing(self, manager) -> None:
        # 'testing' is done but 'reviewer' is not: the slice starts at reviewer and
        # keeps everything after it (including the already-done 'testing').
        manager.history = self._history_with("done\nTESTING_COMPLETE")
        remaining = manager.get_remaining_phases(["planner", "reviewer", "imp", "testing"])
        assert remaining == ["planner", "reviewer", "imp", "testing"]

    def test_completed_prefix_is_dropped_even_if_later_done(self, manager) -> None:
        # planner done + testing done: slice starts at reviewer, keeps imp/testing.
        manager.history = self._history_with(
            "PLANNER_COMPLETE", "TESTING_COMPLETE"
        )
        remaining = manager.get_remaining_phases(["planner", "reviewer", "imp", "testing"])
        assert remaining == ["reviewer", "imp", "testing"]

    def test_all_completed_returns_empty_list(self, manager) -> None:
        manager.history = self._history_with(
            "PLANNER_COMPLETE", "REVIEWER_COMPLETE", "IMP_COMPLETE", "TESTING_COMPLETE"
        )
        assert manager.get_remaining_phases(["planner", "reviewer", "imp", "testing"]) == []

    def test_non_assistant_messages_are_ignored(self, manager) -> None:
        # Markers in user/system messages must not count as completions.
        manager.history = [
            {"role": "user", "content": "PLANNER_COMPLETE"},
            {"role": "system", "content": "IMP_COMPLETE"},
            {"role": "assistant", "content": ""},
        ]
        phases = ["planner", "reviewer", "imp", "testing"]
        assert manager.get_remaining_phases(phases) == phases

    def test_marker_in_prose_does_not_end_phase(self, manager) -> None:
        # The marker must stand alone on its own line (see phase_completed docstring).
        manager.history = self._history_with(
            "The plan is written; PLANNER_COMPLETE will follow next."
        )
        phases = ["planner", "reviewer", "imp", "testing"]
        assert manager.get_remaining_phases(phases) == phases

    def test_unknown_phase_names_are_kept_in_order(self, manager) -> None:
        # Custom phase lists work as long as markers match <PHASE>_COMPLETE.
        manager.history = self._history_with("ANALYSIS_COMPLETE")
        assert manager.get_remaining_phases(["analysis", "build"]) == ["build"]

    def test_marker_split_across_messages(self, manager) -> None:
        # Each message is checked independently; one complete marker anywhere counts.
        manager.history = self._history_with(
            "Working on it...",
            "PLANNER_COMPLETE",
            "Now moving to review.",
        )
        remaining = manager.get_remaining_phases(["planner", "reviewer"])
        assert remaining == ["reviewer"]
