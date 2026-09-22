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


class TestImpPhasePrompt:
    def test_mentions_notes_tool(self, manager):
        from JFI.session.simple_session_manager import get_system_message

        msg = get_system_message("imp", manager.plan_path)
        assert "add_reviewer_note" in msg


class TestReviewerPhasePrompt:
    def test_mentions_notes_tool(self, manager):
        from JFI.session.simple_session_manager import get_system_message

        msg = get_system_message("reviewer", manager.plan_path)
        assert "get_reviewer_notes" in msg

    def test_instructs_treating_notes_as_something_to_recheck(self, manager):
        from JFI.session.simple_session_manager import get_system_message

        msg = get_system_message("reviewer", manager.plan_path)
        assert "not something to accept" in msg or "re-check yourself" in msg


class TestCleanupPhaseProtectsNotes:
    def test_cleanup_protects_the_shared_db_notes_live_in(self, manager):
        """Reviewer notes are DB-backed now, inside the same shared .jfi/
        database as everything else -- there is no separate
        NotesForReviewer.md file left to name; the DB itself is the
        thing cleanup must protect (see the .jfi/ protection block)."""
        from JFI.session.simple_session_manager import get_system_message

        msg = " ".join(get_system_message("cleanup", manager.plan_path).lower().split())
        assert ".jfi" in msg
        assert "shared sqlite database" in msg


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
