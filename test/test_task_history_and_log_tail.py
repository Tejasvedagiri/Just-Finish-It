"""record_task_tokens / get_status_snapshot's task_history + log_tail
fields -- both feed the fleet dashboard's tasks-vs-tokens heatmap and live
log view, kept in memory specifically because a remote viewer (the whole
point of jfi-master/socket_reporter.py) has no filesystem access to this
machine's run.log or any per-task bookkeeping at all.
"""

from JFI.manager.pt_console_manager import PromptToolkitConsoleManager, MAX_TASK_HISTORY


class TestRecordTaskTokens:
    def test_records_one_entry(self):
        manager = PromptToolkitConsoleManager()
        manager.record_task_tokens("3.1 Fix the thing", 4200)
        assert manager._task_history == [{
            "task": "3.1 Fix the thing", "tokens": 4200,
            "phase": None, "started_at": None, "ended_at": None,
        }]

    def test_records_phase_and_start_end_timestamps_when_given(self):
        manager = PromptToolkitConsoleManager()
        manager.record_task_tokens("3.1 Fix the thing", 4200, started_at=100.0, ended_at=142.5, phase="imp")
        [entry] = manager._task_history
        assert entry["phase"] == "imp"
        assert entry["started_at"] == 100.0
        assert entry["ended_at"] == 142.5

    def test_imp_and_testing_leaves_sharing_a_number_stay_distinguishable_by_phase(self):
        """'## Implementation' and '## Testing' are separately-numbered
        trees in plan.md, so the SAME leaf number can legitimately exist in
        both -- phase is what tells two such entries apart, not the number."""
        manager = PromptToolkitConsoleManager()
        manager.record_task_tokens("3.2 Add the handler", 500, phase="imp")
        manager.record_task_tokens("3.2 Exercise the handler", 300, phase="testing")
        phases = [e["phase"] for e in manager._task_history]
        assert phases == ["imp", "testing"]

    def test_ignores_blank_task_title(self):
        manager = PromptToolkitConsoleManager()
        manager.record_task_tokens("", 500)
        assert manager._task_history == []

    def test_ignores_zero_or_negative_tokens(self):
        manager = PromptToolkitConsoleManager()
        manager.record_task_tokens("3.1 Something", 0)
        manager.record_task_tokens("3.2 Something else", -5)
        assert manager._task_history == []

    def test_accumulates_multiple_entries_in_order(self):
        manager = PromptToolkitConsoleManager()
        manager.record_task_tokens("1.1 First", 100)
        manager.record_task_tokens("1.2 Second", 200)
        assert [e["task"] for e in manager._task_history] == ["1.1 First", "1.2 Second"]

    def test_capped_at_max_task_history_dropping_oldest_first(self):
        manager = PromptToolkitConsoleManager()
        for i in range(MAX_TASK_HISTORY + 10):
            manager.record_task_tokens(f"task-{i}", 1)
        assert len(manager._task_history) == MAX_TASK_HISTORY
        assert manager._task_history[0]["task"] == "task-10"  # oldest 10 dropped
        assert manager._task_history[-1]["task"] == f"task-{MAX_TASK_HISTORY + 9}"

    def test_appears_in_status_snapshot(self):
        manager = PromptToolkitConsoleManager()
        manager.record_task_tokens("2.1 Leaf", 900)
        snapshot = manager.get_status_snapshot()
        assert snapshot["task_history"] == [{
            "task": "2.1 Leaf", "tokens": 900,
            "phase": None, "started_at": None, "ended_at": None,
        }]

    def test_task_started_at_appears_in_status_snapshot(self):
        manager = PromptToolkitConsoleManager()
        manager.set_status(task="4.1 Leaf", task_started_at=12345.0)
        assert manager.get_status_snapshot()["task_started_at"] == 12345.0


class TestLogTail:
    def test_starts_empty(self):
        manager = PromptToolkitConsoleManager()
        assert manager.get_status_snapshot()["log_tail"] == []

    def test_log_lines_captured_without_a_file(self):
        """_log_tail fills even when start_session_log was never called --
        the ring buffer must not depend on run.log actually being open."""
        manager = PromptToolkitConsoleManager()
        manager._log("SYSTEM", "hello world")
        tail = manager.get_status_snapshot()["log_tail"]
        assert len(tail) == 1
        assert "SYSTEM: hello world" in tail[0]

    def test_multiline_text_becomes_multiple_entries(self):
        manager = PromptToolkitConsoleManager()
        manager._log("TOOL_RESULT", "line one\nline two")
        tail = manager.get_status_snapshot()["log_tail"]
        assert len(tail) == 2
        assert "line one" in tail[0]
        assert "line two" in tail[1]

    def test_capped_at_400_lines(self):
        manager = PromptToolkitConsoleManager()
        for i in range(450):
            manager._log("SYSTEM", f"line {i}")
        tail = manager.get_status_snapshot()["log_tail"]
        assert len(tail) == 400
        assert "line 449" in tail[-1]
        assert "line 50" in tail[0]  # first 50 dropped (450 - 400)
