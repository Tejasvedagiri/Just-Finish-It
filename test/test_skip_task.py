"""Ctrl+K / skip-current-task: runner.handle_skip_request wires
console.drain_skip_request() into SimpleSessionManager.skip_current_task,
and PromptToolkitConsoleManager's Ctrl+K binding sets the flag that
drain_skip_request reports and clears.
"""

from JFI.manager.abstract_manager import AbstractManager


class _StubConsole(AbstractManager):
    def __init__(self, skip_pending: bool = False):
        self._skip_pending = skip_pending
        self.systems: list = []

    def drain_skip_request(self) -> bool:
        pending, self._skip_pending = self._skip_pending, False
        return pending

    def display_system(self, text):
        self.systems.append(text)

    def display_assistant(self, *a, **k): pass
    def display_user(self, *a, **k): pass
    def get_user_input(self, *a, **k): pass
    def print_agent_response(self, *a, **k): pass


class TestHandleSkipRequest:
    def test_no_request_pending_is_a_noop(self, manager):
        from JFI.runner import handle_skip_request

        manager.plan_file.parent.mkdir(parents=True, exist_ok=True)
        manager.plan_file.write_text("## Implementation\n- [ ] 1.1 a\n", encoding="utf-8")
        console = _StubConsole(skip_pending=False)

        handle_skip_request(console, manager, "imp")

        assert console.systems == []
        assert "- [ ] 1.1 a" in manager.plan_file.read_text(encoding="utf-8")

    def test_marks_item_skipped_and_injects_user_message(self, manager):
        from JFI.runner import handle_skip_request

        manager.plan_file.parent.mkdir(parents=True, exist_ok=True)
        manager.plan_file.write_text("## Implementation\n- [ ] 1.1 a\n", encoding="utf-8")
        console = _StubConsole(skip_pending=True)

        handle_skip_request(console, manager, "imp")

        assert "- [○] 1.1 a" in manager.plan_file.read_text(encoding="utf-8")
        assert any("Skipped: 1.1 a" in s for s in console.systems)
        last_message = manager.history[-1]
        assert last_message["role"] == "user"
        assert "skipped" in last_message["content"].lower()
        assert "next unchecked item" in last_message["content"]

    def test_nothing_pending_reports_and_does_not_add_a_message(self, manager):
        from JFI.runner import handle_skip_request

        manager.plan_file.parent.mkdir(parents=True, exist_ok=True)
        manager.plan_file.write_text("## Implementation\n- [x] 1.1 done\n", encoding="utf-8")
        console = _StubConsole(skip_pending=True)
        history_before = len(manager.history)

        handle_skip_request(console, manager, "imp")

        assert any("Nothing to skip" in s for s in console.systems)
        assert len(manager.history) == history_before

    def test_planner_phase_is_a_noop_even_with_pending_items_elsewhere(self, manager):
        from JFI.runner import handle_skip_request

        manager.plan_file.parent.mkdir(parents=True, exist_ok=True)
        manager.plan_file.write_text("## Implementation\n- [ ] 1.1 a\n", encoding="utf-8")
        console = _StubConsole(skip_pending=True)

        handle_skip_request(console, manager, "planner")

        assert any("Nothing to skip" in s for s in console.systems)
        assert "- [ ] 1.1 a" in manager.plan_file.read_text(encoding="utf-8")


class TestPromptToolkitConsoleManagerSkipFlag:
    def _new_manager(self):
        from JFI.manager.pt_console_manager import PromptToolkitConsoleManager
        return PromptToolkitConsoleManager(title="skip-flag")

    def test_drain_skip_request_reports_and_clears(self):
        console = self._new_manager()
        console._skip_requested.set()

        assert console.drain_skip_request() is True
        assert console.drain_skip_request() is False  # cleared after first read

    def test_ctrl_k_is_bound_and_sets_the_flag(self):
        """Finds the real registered c-k binding (not a simulated stand-in)
        and invokes its handler directly — headless, no event loop needed,
        same pattern as test_choice_menu.py."""
        from prompt_toolkit.keys import Keys

        console = self._new_manager()
        bindings = console._kb.get_bindings_for_keys((Keys.ControlK,))
        assert bindings, "no key binding registered for c-k"

        assert console.drain_skip_request() is False
        bindings[0].handler(None)  # the handler ignores its `event` arg
        assert console.drain_skip_request() is True
