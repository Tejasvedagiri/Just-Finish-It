"""Tests for persisting the plain-queued-input line across a resumed session.

Regression: the queue used to live only in PromptToolkitConsoleManager's
in-memory queue.Queue — closing the process (killed, crashed, or just quit)
before the pipeline reached "review landed, drain queue" silently lost
anything the user had queued. SimpleSessionManager.{load,save}_queued_requests
persist it via JFI.session.metadata_store (DB-backed);
PromptToolkitConsoleManager.set_queue_store wires the two together (seed on
start, report on every add/drain/promotion).
"""

from prompt_toolkit.buffer import Buffer


def _submit(console, text: str) -> None:
    buf = Buffer()
    buf.text = text
    console._on_accept(buf)


class TestSessionManagerQueuePersistence:
    def test_load_queued_requests_defaults_to_empty(self, manager):
        assert manager.load_queued_requests() == []

    def test_save_then_load_round_trips(self, manager):
        manager.save_queued_requests(["a", "b", "c"])
        assert manager.load_queued_requests() == ["a", "b", "c"]

    def test_save_persists_across_manager_instances(self, make_manager):
        """Metadata (including queued_requests) is DB-backed now (see
        JFI.session.metadata_store) -- a second manager for the same
        session_id must see it via the shared project-root .JFI.db."""
        first = make_manager("persist")
        first.save_queued_requests(["do the thing"])

        second = make_manager("persist")
        assert second.load_queued_requests() == ["do the thing"]

    def test_save_empty_list_clears_it(self, make_manager):
        ssm = make_manager("clear")
        ssm.save_queued_requests(["x"])
        ssm.save_queued_requests([])
        assert make_manager("clear").load_queued_requests() == []


class TestConsoleQueueStore:
    def test_set_queue_store_seeds_the_live_queue(self):
        from JFI.manager.pt_console_manager import PromptToolkitConsoleManager

        console = PromptToolkitConsoleManager()
        console.set_queue_store(["already queued from before"], lambda items: None)
        assert console.pending_input_count() == 1
        assert console.drain_queued_input() == ["already queued from before"]

    def test_typing_reports_full_queue_contents_via_callback(self):
        from JFI.manager.pt_console_manager import PromptToolkitConsoleManager

        console = PromptToolkitConsoleManager()
        seen = []
        console.set_queue_store([], seen.append)

        _submit(console, "first request")
        _submit(console, "second request")

        assert seen[-1] == ["first request", "second request"]

    def test_draining_reports_empty_queue(self):
        from JFI.manager.pt_console_manager import PromptToolkitConsoleManager

        console = PromptToolkitConsoleManager()
        seen = []
        console.set_queue_store([], seen.append)

        _submit(console, "one")
        console.drain_queued_input()

        assert seen[-1] == []

    def test_bare_force_promotion_reports_empty_queue(self):
        """A bare '!' promotes everything queued into the forced queue —
        the persisted queue must be cleared, not left stale."""
        from JFI.manager.pt_console_manager import PromptToolkitConsoleManager

        console = PromptToolkitConsoleManager()
        seen = []
        console.set_queue_store([], seen.append)

        _submit(console, "queued item")
        _submit(console, "!")  # bare force: promote everything queued

        assert seen[-1] == []
        assert console.forced_input_count() == 1

    def test_persistence_survives_close_and_reopen_end_to_end(self, make_manager):
        """The realistic scenario: queue something, 'close' (never drained),
        then a fresh console+session pair for the same session id must see
        it again — and draining it must clear the persisted copy too."""
        from JFI.manager.pt_console_manager import PromptToolkitConsoleManager

        console1 = PromptToolkitConsoleManager()
        ssm1 = make_manager("resume_queue")
        console1.set_queue_store(ssm1.load_queued_requests(), ssm1.save_queued_requests)
        _submit(console1, "add a login page")
        _submit(console1, "also add tests for it")
        assert ssm1.load_queued_requests() == ["add a login page", "also add tests for it"]

        # "close": nothing more drains the queue; a fresh pair resumes it.
        console2 = PromptToolkitConsoleManager()
        ssm2 = make_manager("resume_queue")
        console2.set_queue_store(ssm2.load_queued_requests(), ssm2.save_queued_requests)

        assert console2.pending_input_count() == 2
        drained = console2.drain_queued_input()
        assert drained == ["add a login page", "also add tests for it"]
        assert ssm2.load_queued_requests() == []

        # A third resume must see an empty queue (correctly cleared, not stale).
        console3 = PromptToolkitConsoleManager()
        ssm3 = make_manager("resume_queue")
        console3.set_queue_store(ssm3.load_queued_requests(), ssm3.save_queued_requests)
        assert console3.pending_input_count() == 0


class TestSubmitExternalQueueItem:
    """submit_external_answer's counterpart for a NEW request, not an answer
    to something already pending -- the web dashboard's "queue a follow-up"
    box (see WebBridge._relay_answer's "queue" branch) goes through this."""

    def test_queues_text_like_a_typed_line(self):
        from JFI.manager.pt_console_manager import PromptToolkitConsoleManager

        console = PromptToolkitConsoleManager()
        console.submit_external_queue_item("do the thing")

        assert console.pending_input_count() == 1
        assert console.drain_queued_input() == ["do the thing"]

    def test_blank_text_is_a_no_op(self):
        from JFI.manager.pt_console_manager import PromptToolkitConsoleManager

        console = PromptToolkitConsoleManager()
        console.submit_external_queue_item("   ")

        assert console.pending_input_count() == 0

    def test_reports_via_queue_store_callback(self):
        from JFI.manager.pt_console_manager import PromptToolkitConsoleManager

        console = PromptToolkitConsoleManager()
        seen = []
        console.set_queue_store([], seen.append)

        console.submit_external_queue_item("from the web")

        assert seen[-1] == ["from the web"]

    def test_abstract_manager_default_is_a_no_op(self):
        """No-op default for managers with no such external channel (see
        AbstractManager.submit_external_queue_item) -- must not raise."""
        from JFI.manager.abstract_manager import AbstractManager

        class _Bare(AbstractManager):
            def display_system(self, *a, **k): pass
            def display_assistant(self, *a, **k): pass
            def display_user(self, *a, **k): pass
            def get_user_input(self, *a, **k): pass
            def print_agent_response(self, *a, **k): pass

        _Bare().submit_external_queue_item("anything")


class TestSubmitExternalPause:
    """The fleet dashboard's remote pause/resume button (see
    socket_reporter.py's _dispatch_control) goes through this -- same
    threading.Event Ctrl+P already toggles locally, just driven externally
    with an explicit target state instead of a toggle."""

    def test_pause_true_sets_is_paused(self):
        from JFI.manager.pt_console_manager import PromptToolkitConsoleManager

        console = PromptToolkitConsoleManager()
        assert console.is_paused() is False
        console.submit_external_pause(True)
        assert console.is_paused() is True

    def test_pause_false_clears_is_paused(self):
        from JFI.manager.pt_console_manager import PromptToolkitConsoleManager

        console = PromptToolkitConsoleManager()
        console.submit_external_pause(True)
        console.submit_external_pause(False)
        assert console.is_paused() is False

    def test_setting_the_same_state_twice_is_a_noop(self):
        """Two controllers (a local keypress + a remote button) landing on
        the same target state must never toggle past it."""
        from JFI.manager.pt_console_manager import PromptToolkitConsoleManager

        console = PromptToolkitConsoleManager()
        console.submit_external_pause(True)
        console.submit_external_pause(True)
        assert console.is_paused() is True

    def test_appears_in_status_snapshot(self):
        from JFI.manager.pt_console_manager import PromptToolkitConsoleManager

        console = PromptToolkitConsoleManager()
        assert console.get_status_snapshot()["is_paused"] is False
        console.submit_external_pause(True)
        assert console.get_status_snapshot()["is_paused"] is True

    def test_abstract_manager_default_is_a_no_op(self):
        from JFI.manager.abstract_manager import AbstractManager

        class _Bare(AbstractManager):
            def display_system(self, *a, **k): pass
            def display_assistant(self, *a, **k): pass
            def display_user(self, *a, **k): pass
            def get_user_input(self, *a, **k): pass
            def print_agent_response(self, *a, **k): pass

        _Bare().submit_external_pause(True)  # must not raise


class TestQueuedItemsInStatusSnapshot:
    """get_status_snapshot's queue_size only ever reported a count -- the
    fleet dashboard's queue-management UI needs the actual queued text too."""

    def test_starts_empty(self):
        from JFI.manager.pt_console_manager import PromptToolkitConsoleManager

        console = PromptToolkitConsoleManager()
        assert console.get_status_snapshot()["queued_items"] == []

    def test_reflects_queued_text_in_order(self):
        from JFI.manager.pt_console_manager import PromptToolkitConsoleManager

        console = PromptToolkitConsoleManager()
        console.submit_external_queue_item("first")
        console.submit_external_queue_item("second")
        snapshot = console.get_status_snapshot()
        assert snapshot["queued_items"] == ["first", "second"]
        assert snapshot["queue_size"] == 2
