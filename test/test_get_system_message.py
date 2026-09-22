"""
Tests for session_manager.get_system_message: it must be present, instruct
the LLM to use the DB-backed plan tools (get_plan/add_leaf/...) rather than
writing a plan file, and no longer generate any markdown of its own.

`get_system_message` is a module-level function (phase, plan_path); the manager
threads its own resolved plan path through `_phase_system_message(phase)`.
`plan_path` is a legacy label used for review.md's location and cleanup's
own bookkeeping-folder references — the plan tree itself lives entirely in
the DB (see JFI.tool.plan_db_tools), never at that path.
"""


class TestGetSystemMessage:
    def test_function_is_present(self):
        from JFI.session import simple_session_manager as sm

        assert callable(sm.get_system_message)

    def test_returns_nonempty_string_with_plan_reference(self, manager):
        from JFI.session.simple_session_manager import get_system_message

        msg = get_system_message("planner", manager.plan_path)
        assert isinstance(msg, str) and msg.strip()
        # The LLM is told to use the DB-backed plan tools, not a file path
        # (the planner phase never sees plan.md/JFI -- the plan lives
        # entirely in the DB, see JFI.tool.plan_db_tools).
        assert "get_plan()" in msg and "add_leaf" in msg

    def test_phase_system_message_threads_plan_path(self, manager):
        """The per-phase system message must reference the resolved JFI plan path."""
        manager.plan_file.write_text("# Plan\n## Implementation\n- [ ] 1.1 x\n")
        msg = manager._phase_system_message("imp")
        assert "plan.md" in msg and ".jfi" in msg

    def test_plan_lives_inside_jfi_folder(self, manager):
        """The plan path must be inside the flat .jfi/ folder."""
        assert manager.plan_path.startswith(".jfi/")
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


class TestImplementationWholeProjectBuildGate:
    """The imp phase must not sign off (IMP_COMPLETE) until it has also run
    a whole-project build/typecheck once, when one exists -- catches two
    files drifting apart (a renamed/missing export still imported
    elsewhere) that neither file's own local check would ever notice on
    its own. See the real StockUI failure this was added for: renderers.js
    importing drawAreaLineChart/drawSparkline that charts.js never
    exported, only caught by an actual `npm run build`."""

    def test_imp_prompt_requires_a_whole_project_build_before_completion(self, manager):
        from JFI.session.simple_session_manager import get_system_message

        msg = get_system_message("imp", manager.plan_path).lower()
        assert "npm run build" in msg
        assert "whole project" in msg
        assert "imp_complete" in msg  # still the required completion marker

    def test_other_phases_do_not_carry_the_imp_specific_build_gate_wording(self, manager):
        from JFI.session.simple_session_manager import get_system_message

        for phase in ("planner", "testing", "reviewer", "cleanup"):
            assert "step 6" not in get_system_message(phase, manager.plan_path).lower()


class TestVerificationOrderingGuidance:
    """Shared VERIFICATION_RULES text (imp/testing/reviewer/cleanup all get
    it): a full build/compile check belongs at the FRONT of Testing, and a
    fragile check needing new system tooling (a headless browser, sudo, ...)
    must not block the whole run -- see the real failure this covers:
    T.2 blocked forever on `playwright install --with-deps` needing sudo,
    while T.3 (`npm run build`, which would have caught the actual bug)
    never got a chance to run."""

    def test_mentions_build_ordering(self, manager):
        from JFI.session.simple_session_manager import get_system_message

        msg = get_system_message("testing", manager.plan_path).lower()
        assert "cheapest" in msg
        assert "front" in msg

    def test_mentions_fragile_check_fallback(self, manager):
        from JFI.session.simple_session_manager import get_system_message

        msg = get_system_message("testing", manager.plan_path).lower()
        assert "not a blocker" in msg
        assert "sudo" in msg


class TestStructuredProcessToolsGuidance:
    """VERIFICATION_RULES points at start_background_process/
    stop_background_process (handle-based, never a raw pid/name pattern)
    as the default for anything long-running, instead of a shell `&` plus
    hand-tracked PID -- see process_tools.py's own module docstring for the
    self-inflicted `pkill -f` failure this replaces."""

    def test_mentions_structured_process_tools(self, manager):
        from JFI.session.simple_session_manager import get_system_message

        msg = get_system_message("testing", manager.plan_path)
        assert "start_background_process" in msg
        assert "stop_background_process" in msg

    def test_still_warns_about_name_based_search_as_fallback(self, manager):
        from JFI.session.simple_session_manager import get_system_message

        msg = get_system_message("testing", manager.plan_path)
        assert "pgrep" in msg or "pkill" in msg


class TestReadFilePreferredOverCat:
    def test_imp_prompt_prefers_read_file_over_cat(self, manager):
        from JFI.session.simple_session_manager import get_system_message

        msg = get_system_message("imp", manager.plan_path)
        assert "prefer read_file" in msg
        assert "`cat`" in msg

    def test_testing_prompt_prefers_read_file_over_cat(self, manager):
        from JFI.session.simple_session_manager import get_system_message

        msg = get_system_message("testing", manager.plan_path)
        assert "prefer read_file over" in msg


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
    """A small DB-backed fact store (JFI.models.ContextEntry) the model can
    read/write via the context_save/context_lookup tools for facts that
    would otherwise be lost once older turns are compressed out of context
    — see CONTEXT_CACHE_RULES. There is no file backing it, and nothing is
    auto-loaded into the prompt: the model pulls what it needs via
    context_lookup as an ordinary tool call, so every phase's system
    message only needs to describe those two tools, never a path."""

    def test_get_system_message_describes_context_save_and_lookup(self, manager):
        from JFI.session.simple_session_manager import get_system_message

        for phase in ("planner", "imp", "testing", "reviewer", "cleanup"):
            msg = get_system_message(phase, manager.plan_path)
            assert "context_save" in msg and "context_lookup" in msg
            assert "CONTEXT CACHE" in msg

    def test_phase_system_message_describes_context_tools(self, manager):
        for phase in ("planner", "imp", "testing", "reviewer", "cleanup"):
            msg = manager._phase_system_message(phase)
            assert "context_save" in msg and "context_lookup" in msg

    def test_default_get_system_message_call_still_works(self):
        """Only plan_path is required; every other parameter has a default."""
        from JFI.session.simple_session_manager import get_system_message

        msg = get_system_message("imp", "JFI/demo/plan.md")
        assert isinstance(msg, str) and "context_save" in msg


class TestRunCommandsPersistence:
    """Observed failure this guidance exists to prevent: a session discovers
    the correct interpreter/command the hard way (plain `python3` fails with
    ModuleNotFoundError, THEN `.venv/bin/python`/`uv run` is tried), and then
    re-discovers it the same way again later once the turn that figured it
    out ages out of context. The fix is the SAME DB-backed context cache
    other durable facts already use via context_save (see
    CONTEXT_CACHE_RULES) — a stable "run_commands" key, not a new file or
    tool."""

    def test_context_cache_rules_instruct_saving_run_commands(self, manager):
        from JFI.session.simple_session_manager import get_system_message

        for phase in ("planner", "imp", "testing", "reviewer", "cleanup"):
            msg = get_system_message(phase, manager.plan_path)
            assert "run_commands" in msg
            assert "context_save" in msg

    def test_names_the_python3_then_uv_failure_pattern(self, manager):
        from JFI.session.simple_session_manager import get_system_message

        msg = get_system_message("imp", manager.plan_path)
        assert "ModuleNotFoundError" in msg
        assert "uv run" in msg


class TestReviewerSystemMessage:
    """The reviewer phase must carry BOTH branches of the conditional
    review-report instruction: do not call write_review_report when the
    work is good; call it (DB-backed, see JFI.tool.note_tools) when issues
    exist."""

    def test_good_branch_instructs_not_to_call_write_review_report(self, manager):
        from JFI.session.simple_session_manager import get_system_message

        msg = get_system_message("reviewer", manager.plan_path).lower()
        assert "do not call write_review_report" in msg
        assert "pass" in msg  # the short 'Review: PASS' summary branch

    def test_issues_branch_instructs_to_call_write_review_report(self, manager):
        from JFI.session.simple_session_manager import get_system_message

        msg = get_system_message("reviewer", manager.plan_path).lower()
        assert "call write_review_report with concrete" in msg
        # The report must be concrete and actionable.
        assert "line(s)" in msg or "file/line" in msg

    def test_reviewer_reads_notes_via_get_reviewer_notes(self, manager):
        from JFI.session.simple_session_manager import get_system_message

        msg = get_system_message("reviewer", manager.plan_path)
        assert "get_reviewer_notes()" in msg


class TestCleanupSystemMessage:
    """The cleanup phase's sole job: sweep the working directory for stray,
    non-deliverable files and relocate anything worth keeping into the
    flat `.jfi/` folder for reference, deleting the rest -- while never
    touching the bookkeeping files (or the shared DB) already inside it."""

    def test_names_the_session_folder_as_the_move_target(self, manager):
        from JFI.session.simple_session_manager import get_system_message

        msg = get_system_message("cleanup", manager.plan_path)
        assert ".jfi" in msg
        assert "mv" in msg  # instructed via execute_command's mv

    def test_protects_bookkeeping_files_from_deletion(self, manager):
        from JFI.session.simple_session_manager import get_system_message

        msg = " ".join(get_system_message("cleanup", manager.plan_path).lower().split())
        assert "never delete, move, or overwrite" in msg
        assert "llm_debug.jsonl" in msg

    def test_protects_the_shared_db_from_deletion(self, manager):
        """The shared SQLite database sits inside the flat `.jfi/` folder
        at the project ROOT -- it will look unrecognized/stray when
        cleanup scans the working directory unless the prompt explicitly
        calls it out (see todo.md's independent-review findings)."""
        from JFI.session.simple_session_manager import get_system_message

        msg = " ".join(get_system_message("cleanup", manager.plan_path).lower().split())
        assert ".jfi" in msg and "shared sqlite database" in msg
        assert "never" in msg and "not stray" in msg

    def test_completion_marker_present(self, manager):
        from JFI.session.simple_session_manager import get_system_message

        msg = get_system_message("cleanup", manager.plan_path)
        assert "CLEANUP_COMPLETE" in msg

    def test_other_phases_do_not_carry_cleanup_instructions(self, manager):
        from JFI.session.simple_session_manager import get_system_message

        for phase in ("planner", "imp", "testing", "reviewer"):
            assert "CLEANUP_COMPLETE" not in get_system_message(phase, manager.plan_path)
