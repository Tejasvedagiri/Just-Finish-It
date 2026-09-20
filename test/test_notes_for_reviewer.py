"""NotesForReviewer.md: a plan.md/review.md-style sidecar the Implementation
agent writes to (via append_to_file) whenever a step hits a problem worth
the Reviewer knowing about -- an assumption it had to make, a check it
couldn't fully verify, a workaround. The Reviewer agent reads it (if
present) alongside plan.md before deciding PASS/FAIL, and the harness
clears it unconditionally right after the reviewer phase completes (see
runner._clear_reviewer_notes, wired into run_pipeline's phase loop) so a
stale note never resurfaces in a later, unrelated review pass.
"""
from pathlib import Path


class TestImpPhasePrompt:
    def test_mentions_notes_file_and_append_tool(self, manager):
        from JFI.session.simple_session_manager import get_system_message

        msg = get_system_message("imp", manager.plan_path)
        assert "NotesForReviewer.md" in msg
        assert "append_to_file" in msg

    def test_notes_file_lives_beside_plan(self, manager):
        """Same JFI/<session>/ folder as plan.md, not some other location."""
        from JFI.session.simple_session_manager import get_system_message

        msg = get_system_message("imp", manager.plan_path)
        expected_path = str(Path(manager.plan_path).with_name("NotesForReviewer.md"))
        assert expected_path in msg


class TestReviewerPhasePrompt:
    def test_mentions_notes_file(self, manager):
        from JFI.session.simple_session_manager import get_system_message

        msg = get_system_message("reviewer", manager.plan_path)
        expected_path = str(Path(manager.plan_path).with_name("NotesForReviewer.md"))
        assert expected_path in msg
        assert "read_file it too" in msg or "read_file" in msg

    def test_instructs_treating_notes_as_something_to_recheck(self, manager):
        from JFI.session.simple_session_manager import get_system_message

        msg = get_system_message("reviewer", manager.plan_path)
        assert "not something to accept" in msg or "re-check yourself" in msg


class TestCleanupPhaseProtectsNotesFile:
    def test_never_delete_list_includes_notes_file(self, manager):
        from JFI.session.simple_session_manager import get_system_message

        msg = get_system_message("cleanup", manager.plan_path)
        assert "NotesForReviewer.md" in msg


class TestClearReviewerNotes:
    def test_removes_existing_notes_file(self, make_manager):
        from JFI.runner import _clear_reviewer_notes

        ssm = make_manager("has-notes")
        ssm.session_path.mkdir(parents=True, exist_ok=True)
        notes_file = ssm.session_path / "NotesForReviewer.md"
        notes_file.write_text("# Notes for Reviewer\n\n1.1: had to assume X\n", encoding="utf-8")

        _clear_reviewer_notes(ssm)

        assert not notes_file.exists()

    def test_no_error_when_notes_file_absent(self, make_manager):
        """The common case -- an uneventful imp phase never wrote one at all."""
        from JFI.runner import _clear_reviewer_notes

        ssm = make_manager("no-notes")
        ssm.session_path.mkdir(parents=True, exist_ok=True)

        _clear_reviewer_notes(ssm)  # must not raise
