"""start_background_process / list_processes / stop_background_process
(src/JFI/tool/process_tools.py) -- structured process management scoped to
processes THIS module itself started, replacing the model freehanding
`ps`/`kill`/`pkill` through execute_command (see the module docstring for
the self-inflicted `pkill -f` failure this exists to prevent).

Each test gets a fresh registry (the module's dicts are process-global
state, same tradeoff execute_command's own lack of persistence has) via
the autouse fixture below.
"""

import time

import pytest

from JFI.models import get_engine
from JFI.tool import context_tools, process_tools


@pytest.fixture(autouse=True)
def _fresh_registry():
    process_tools._registry.clear()
    process_tools._metadata.clear()
    process_tools._next_id = 0
    yield
    # Best-effort cleanup so a failed assertion never leaks a live process
    # into the next test run.
    for handle in list(process_tools._registry):
        try:
            process_tools.stop_background_process(handle, timeout=1)
        except Exception:
            pass


class TestStartBackgroundProcess:
    def test_rejects_blank_command(self):
        assert "Error" in process_tools.start_background_process("   ")

    def test_starts_and_returns_a_handle_not_a_raw_pid(self):
        result = process_tools.start_background_process("sleep 5")
        assert result.startswith("Started 'sleep 5' as bg1")
        assert "pid" in result

    def test_handles_increment_across_calls(self):
        r1 = process_tools.start_background_process("sleep 5")
        r2 = process_tools.start_background_process("sleep 5")
        assert "bg1" in r1
        assert "bg2" in r2

    def test_output_captured_to_log_file_when_given(self, tmp_path):
        log = tmp_path / "out.log"
        process_tools.start_background_process("echo hello-world", log_file=str(log))
        time.sleep(0.3)
        assert "hello-world" in log.read_text()

    def test_host_and_port_recorded_when_given(self):
        process_tools.start_background_process("sleep 5", host="127.0.0.1", port="8000")
        entry = process_tools.snapshot_processes()[0]
        assert entry["host"] == "127.0.0.1"
        assert entry["port"] == "8000"

    def test_host_and_port_absent_when_not_given(self):
        process_tools.start_background_process("sleep 5")
        entry = process_tools.snapshot_processes()[0]
        assert entry["host"] is None
        assert entry["port"] is None


class TestSnapshotProcesses:
    def test_empty_when_nothing_started(self):
        assert process_tools.snapshot_processes() == []

    def test_includes_handle_command_pid_status(self):
        process_tools.start_background_process("sleep 5")
        [entry] = process_tools.snapshot_processes()
        assert entry["handle"] == "bg1"
        assert entry["command"] == "sleep 5"
        assert isinstance(entry["pid"], int)
        assert entry["status"] == "running"

    def test_reflects_exit_status(self):
        process_tools.start_background_process("true")
        time.sleep(0.3)
        [entry] = process_tools.snapshot_processes()
        assert entry["status"] == "exited (code 0)"


class TestListProcesses:
    def test_empty_registry_says_so(self):
        assert "No background processes" in process_tools.list_processes()

    def test_lists_a_running_process(self):
        process_tools.start_background_process("sleep 5")
        out = process_tools.list_processes()
        assert "bg1" in out
        assert "running" in out

    def test_lists_an_exited_process_with_its_code(self):
        process_tools.start_background_process("true")
        time.sleep(0.3)
        out = process_tools.list_processes()
        assert "exited (code 0)" in out

    def test_lists_multiple_processes_independently(self):
        process_tools.start_background_process("sleep 5")
        process_tools.start_background_process("sleep 5")
        out = process_tools.list_processes()
        assert "bg1" in out and "bg2" in out


class TestClearFinishedProcesses:
    def test_no_processes_says_so(self):
        assert process_tools.clear_finished_processes() == "No exited processes to clear."

    def test_clears_an_exited_process(self):
        process_tools.start_background_process("true")
        time.sleep(0.3)
        result = process_tools.clear_finished_processes()
        assert "Cleared 1 exited process" in result
        assert "bg1" in result
        assert process_tools.snapshot_processes() == []

    def test_never_clears_a_still_running_process(self):
        process_tools.start_background_process("sleep 5")
        result = process_tools.clear_finished_processes()
        assert result == "No exited processes to clear."
        [entry] = process_tools.snapshot_processes()
        assert entry["handle"] == "bg1"
        assert entry["status"] == "running"

    def test_clears_only_exited_ones_leaving_running_ones_intact(self):
        process_tools.start_background_process("true")       # bg1, will exit
        process_tools.start_background_process("sleep 5")    # bg2, stays running
        time.sleep(0.3)
        result = process_tools.clear_finished_processes()
        assert "bg1" in result
        assert "bg2" not in result
        [entry] = process_tools.snapshot_processes()
        assert entry["handle"] == "bg2"

    def test_sends_no_signal_to_a_running_process(self):
        """clear_finished_processes only ever prunes bookkeeping for
        processes poll() already reports as exited -- it must never touch,
        let alone kill, something still running."""
        process_tools.start_background_process("sleep 5")
        process_tools.clear_finished_processes()
        [entry] = process_tools.snapshot_processes()
        assert entry["status"] == "running"


class TestStopBackgroundProcess:
    def test_unknown_handle_is_an_error(self):
        result = process_tools.stop_background_process("bg999")
        assert "Error" in result
        assert "list_processes" in result

    def test_stops_a_running_process_cleanly(self):
        process_tools.start_background_process("sleep 30")
        result = process_tools.stop_background_process("bg1", timeout=3)
        assert "Stopped bg1" in result
        assert process_tools._registry["bg1"].poll() is not None

    def test_stopping_an_already_exited_process_says_so(self):
        process_tools.start_background_process("true")
        time.sleep(0.3)
        result = process_tools.stop_background_process("bg1")
        assert "already exited" in result

    def test_force_kills_a_process_that_ignores_sigterm(self):
        # A process that explicitly ignores SIGTERM so the SIGKILL fallback
        # path actually gets exercised, not just the clean-stop path. (A
        # shell `trap '' TERM` doesn't work for this: killpg signals every
        # process in the group, including a `sleep` child that has no trap
        # of its own and terminates normally regardless of its parent's.)
        process_tools.start_background_process(
            "python3 -c \"import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)\""
        )
        time.sleep(0.3)  # let the signal handler actually get installed before we send TERM
        result = process_tools.stop_background_process("bg1", timeout=1)
        assert "force-killed" in result.lower()
        assert process_tools._registry["bg1"].poll() is not None

    def test_never_touches_a_process_it_did_not_start(self):
        """The whole point: stop_background_process only ever takes a
        HANDLE this module itself issued -- there is no code path here
        that accepts a raw pid or a name pattern at all, so a name like
        another real process could never reach os.kill."""
        process_tools.start_background_process("sleep 5")
        result = process_tools.stop_background_process("some-unrelated-pattern")
        assert "Error" in result
        # The real bg1 process must be untouched by the bogus call.
        assert process_tools._registry["bg1"].poll() is None


class TestMakeProcessTools:
    """The context-cache-mirroring bindings -- so a process's command/pid/
    host/port/status survives history compression and a resumed session as
    an ordinary context-cache fact, not just live in this module's memory
    (see the module docstring)."""

    def test_start_writes_a_context_cache_entry(self, tmp_path):
        engine = get_engine(tmp_path)
        tools = process_tools.make_process_tools(engine, "demo")

        tools["start_background_process"]("sleep 5", host="127.0.0.1", port="8000")

        entry = context_tools.get_context_value(engine, "demo", "bg_process:bg1")
        assert entry is not None
        assert "127.0.0.1" in entry
        assert "8000" in entry
        assert "status=running" in entry

    def test_stop_updates_the_same_context_cache_entry(self, tmp_path):
        engine = get_engine(tmp_path)
        tools = process_tools.make_process_tools(engine, "demo")
        tools["start_background_process"]("sleep 30")

        tools["stop_background_process"]("bg1", timeout=3)

        entry = context_tools.get_context_value(engine, "demo", "bg_process:bg1")
        assert "exited" in entry or "status=exited" in entry

    def test_does_not_clobber_other_context_cache_keys(self, tmp_path):
        engine = get_engine(tmp_path)
        context_tools.context_save(engine, "demo", "unrelated_fact", "still here")
        tools = process_tools.make_process_tools(engine, "demo")

        tools["start_background_process"]("sleep 5")

        assert context_tools.get_context_value(engine, "demo", "unrelated_fact") == "still here"

    def test_failed_start_does_not_write_a_context_entry(self, tmp_path):
        engine = get_engine(tmp_path)
        tools = process_tools.make_process_tools(engine, "demo")

        tools["start_background_process"]("   ")

        assert context_tools.get_context_value(engine, "demo", "bg_process:bg1") is None
