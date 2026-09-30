"""Reviewer notes: a DB-backed (SessionNote, kind=REVIEWER_NOTES -- see
JFI.tool.note_tools) sidecar the Implementation agent writes to (via
add_reviewer_note) whenever a step hits a problem worth the Reviewer
knowing about -- an assumption it had to make, a check it couldn't fully
verify, a workaround. The Reviewer agent reads it (via get_reviewer_notes)
before deciding PASS/FAIL, and the harness clears it unconditionally
right after the reviewer phase completes (see runner._clear_reviewer_notes,
wired into run_pipeline's phase loop) so a stale note never resurfaces in
a later, unrelated review pass.
"""


class TestDevPrompt:
    def test_mentions_notes_tool(self):
        from JFI.imp.prompts import FINISH_RULE

        assert "add_reviewer_note" in FINISH_RULE


class TestReviewerPhasePrompt:
    def test_mentions_notes_tool(self):
        from JFI.review.prompts import REVIEWER

        assert "get_reviewer_notes" in REVIEWER

    def test_instructs_treating_notes_as_something_to_recheck(self):
        from JFI.review.prompts import REVIEWER

        assert "Re-check each one yourself" in REVIEWER


class TestCleanupPhaseProtectsNotes:
    def test_cleanup_protects_the_shared_db_notes_live_in(self):
        """Reviewer notes are DB-backed, inside the same shared .jfi/ database
        as everything else; that folder is what cleanup must never touch --
        it looks like an unexplained folder when cleanup scans the project."""
        from JFI.review.prompts import CLEANUP

        text = " ".join(CLEANUP.split())
        assert "NEVER delete, move or overwrite anything under .jfi/" in text
        assert "it is not stray" in text


class TestClearReviewerNotes:
    def test_removes_existing_notes(self, make_manager):
        from JFI.runner import _clear_reviewer_notes
        from JFI.tool.note_tools import get_note, REVIEWER_NOTES, set_note

        ssm = make_manager("has-notes")
        set_note(ssm.db_engine, ssm.session_id, REVIEWER_NOTES, "1.1: had to assume X")

        _clear_reviewer_notes(ssm)

        assert get_note(ssm.db_engine, ssm.session_id, REVIEWER_NOTES) is None

    def test_no_error_when_notes_absent(self, make_manager):
        """The common case -- an uneventful imp phase never left one at all."""
        from JFI.runner import _clear_reviewer_notes

        ssm = make_manager("no-notes")

        _clear_reviewer_notes(ssm)  # must not raise
