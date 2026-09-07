"""
Tests for session_manager.get_system_message: it must be present, expose the
plan file location (inside the .JFI folder) to the LLM, and no longer generate
any markdown of its own.

`get_system_message` is a module-level function (phase, plan_path); the manager
threads its own resolved plan path through `_phase_system_message(phase)`.
"""


class TestGetSystemMessage:
    def test_function_is_present(self):
        from JFI.session import simple_session_manager as sm

        assert callable(sm.get_system_message)

    def test_returns_nonempty_string_with_plan_reference(self, manager):
        from JFI.session.simple_session_manager import get_system_message

        msg = get_system_message("planner", manager.plan_path)
        assert isinstance(msg, str) and msg.strip()
        # The LLM is told where the plan lives.
        assert "plan.md" in msg and ".JFI" in msg

    def test_phase_system_message_threads_plan_path(self, manager):
        """The per-phase system message must reference the resolved .JFI plan path."""
        manager.plan_file.write_text("# Plan\n## Implementation\n- [ ] 1.1 x\n")
        msg = manager._phase_system_message("imp")
        assert "plan.md" in msg and ".JFI" in msg

    def test_plan_lives_inside_jfi_folder(self, manager):
        """The plan path must be inside the .JFI session folder."""
        assert manager.plan_path.startswith(".JFI/")
        assert manager.plan_file.name == "plan.md"

    def test_does_not_write_or_generate_markdown(self, manager):
        """get_system_message must not create/mark a markdown document."""
        from JFI.session.simple_session_manager import get_system_message

        before = set(p.name for p in manager.session_path.iterdir()) if manager.session_path.exists() else set()
        msg = get_system_message("planner", manager.plan_path)
        after = set(p.name for p in manager.session_path.iterdir()) if manager.session_path.exists() else set()

        assert isinstance(msg, str) and msg.strip()
        assert before == after, f"get_system_message created files: {after - before}"


class TestGenerateMarkdownRemoved:
    def test_generate_markdown_function_gone_from_session_manager(self):
        import JFI.session.simple_session_manager as sm

        names = [n for n in dir(sm) if "markdown" in n.lower()]
        assert not names, f"generate-markdown function still present: {names}"


class TestLLMUsesTools:
    def test_system_message_instructs_tool_usage(self, manager):
        from JFI.session.simple_session_manager import get_system_message

        msg = get_system_message("planner", manager.plan_path).lower()
        # The plan is produced via file tools, not by the session manager
        # emitting markdown: the planner prompt names them explicitly.
        assert "write_file" in msg and "plan.md" in msg


class TestPendingItems:
    def test_pending_items_returns_unchecked_under_section(self, manager):
        manager.plan_file.write_text(
            "# Plan\n"
            "## Implementation\n- [x] 1.1 done\n- [ ] 1.2 pending\n"
            "## Testing\n- [ ] 2.1 t\n"
        )
        assert manager._pending_items("Implementation") == ["- [ ] 1.2 pending"]

    def test_pending_items_excludes_other_sections(self, manager):
        manager.plan_file.write_text(
            "# Plan\n## Implementation\n- [ ] 1.1 a\n## Testing\n- [ ] 2.1 b\n"
        )
        assert manager._pending_items("Testing") == ["- [ ] 2.1 b"]

    def test_pending_items_empty_when_missing(self, manager):
        assert manager._pending_items("Implementation") == []


class TestContextCache:
    """A small JSON scratchpad the model can read/write for facts that would
    otherwise be lost once older turns are compressed out of context — see
    CONTEXT_CACHE_RULES. It must exist before the model ever asks for it, and
    every phase's system message must tell the model where to find it."""

    def test_context_cache_file_pre_created_empty(self, manager):
        assert manager.context_cache_file.exists()
        assert manager.context_cache_file.read_text().strip() == "{}"

    def test_context_cache_path_lives_inside_jfi_folder(self, manager):
        assert manager.context_cache_path.startswith(".JFI/")
        assert manager.context_cache_file.name == "context.json"

    def test_get_system_message_references_the_cache_path(self, manager):
        from JFI.session.simple_session_manager import get_system_message

        for phase in ("planner", "imp", "testing", "reviewer"):
            msg = get_system_message(phase, manager.plan_path, manager.context_cache_path)
            assert manager.context_cache_path in msg
            assert "CONTEXT CACHE" in msg

    def test_phase_system_message_threads_context_cache_path(self, manager):
        for phase in ("planner", "imp", "testing", "reviewer"):
            msg = manager._phase_system_message(phase)
            assert manager.context_cache_path in msg

    def test_default_get_system_message_call_still_works(self):
        """context_cache_path is optional (defaults to DEFAULT_CONTEXT_CACHE_PATH),
        so existing callers that only pass plan_path keep working."""
        from JFI.session.simple_session_manager import get_system_message

        msg = get_system_message("imp", ".JFI/demo/plan.md")
        assert isinstance(msg, str) and "context.json" in msg


class TestReviewerSystemMessage:
    """The reviewer phase must carry BOTH branches of the conditional review.md
    instruction: do not generate it when the work is good; generate it (via
    write_file) into .JFI/<session>/review.md when issues exist."""

    def test_good_branch_instructs_not_to_generate_review_md(self, manager):
        from JFI.session.simple_session_manager import get_system_message

        msg = get_system_message("reviewer", manager.plan_path).lower()
        assert "do not write or touch .jfi/demo/review.md" in msg
        assert "pass" in msg  # the short 'Review: PASS' summary branch

    def test_issues_branch_instructs_to_generate_review_md(self, manager):
        from JFI.session.simple_session_manager import get_system_message

        msg = get_system_message("reviewer", manager.plan_path).lower()
        assert "write_file to create .jfi/demo/review.md" in msg
        # The report must be concrete and actionable.
        assert "line(s)" in msg or "file/line" in msg

    def test_review_md_lives_next_to_plan(self, manager):
        """The reviewer's review.md target is derived from the plan path — same folder."""
        from pathlib import Path
        from JFI.session.simple_session_manager import get_system_message

        expected = str(Path(manager.plan_path).with_name("review.md"))
        assert expected == ".JFI/demo/review.md"
        msg = get_system_message("reviewer", manager.plan_path)
        assert expected in msg  # the exact path, not a placeholder

    def test_other_phases_do_not_mention_review_md(self, manager):
        """Only the reviewer decides about review.md."""
        from JFI.session.simple_session_manager import get_system_message

        for phase in ("planner", "imp", "testing"):
            assert "review.md" not in get_system_message(phase, manager.plan_path)
