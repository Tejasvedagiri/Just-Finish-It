"""Tests for AbstractManager.safe_get_user_input (event-loop redraw protection)."""

import unittest.mock as mock

from JFI.manager.abstract_manager import AbstractManager


class _Stub(AbstractManager):
    def __init__(self, results=None, excs=None):
        self.results = list(results or [])
        self.excs = list(excs or [])

    def get_user_input(self, prompt_label="You", **kwargs):
        if self.excs:
            raise self.excs.pop(0)
        return self.results.pop(0)

    # no-ops for the abstract interface
    def display_system(self, text): pass
    def display_assistant(self, *a, **k): pass
    def display_user(self, *a, **k): pass
    def print_agent_response(self, *a, **k): pass


class TestSafeGetUserInput:

    def test_returns_value_directly(self):
        assert _Stub(results=["hello"]).safe_get_user_input() == "hello"

    def test_retries_after_error_then_succeeds(self):
        m = _Stub(results=["ok"], excs=[IndexError("boom"), ValueError("again")])
        assert m.safe_get_user_input() == "ok"
        # two failures + one success consumed everything
        assert not m.results and not m.excs

    def test_extras_forwarded(self):
        seen = {}

        class M(_Stub):
            def get_user_input(self, prompt_label="You", **kwargs):
                seen.update(kwargs)
                return "x"

        M().safe_get_user_input(prompt_label="Goal?", multiline=False)
        assert seen == {"multiline": False}

    def test_retries_are_unbounded_until_success(self):
        m = _Stub(results=["ok"], excs=[IndexError("a"), IndexError("b")])
        with mock.patch.object(m, "display_system") as disp:
            out = m.safe_get_user_input()
        assert out == "ok"
        assert disp.call_count == 2  # each failure produced a re-prompt notice

    def test_display_failure_does_not_break_recovery(self):
        class M(_Stub):
            def display_system(self, text):
                raise RuntimeError("console gone")

        m = M(results=["ok"], excs=[IndexError("a")])
        assert m.safe_get_user_input() == "ok"


class TestRichConsoleManagerSafeInput:

    def test_rich_manager_retries_then_succeeds(self):
        from JFI.manager.rich_console_manager import RichConsoleManager

        mgr = RichConsoleManager.__new__(RichConsoleManager)  # skip console init for unit test
        state = {"n": 0}

        def flaky(prompt_label="You", **kwargs):
            state["n"] += 1
            if state["n"] == 1:
                raise IndexError("event-loop redraw glitch")
            return "typed"

        mgr.get_user_input = flaky
        assert mgr.safe_get_user_input() == "typed"
        assert state["n"] == 2

    def test_rich_manager_uses_inherited_safe_path(self):
        from JFI.manager.rich_console_manager import RichConsoleManager
        assert isinstance(RichConsoleManager, type)
        assert issubclass(RichConsoleManager, AbstractManager)
