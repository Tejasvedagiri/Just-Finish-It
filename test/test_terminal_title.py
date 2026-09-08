"""The terminal's own tab/window title (OSC 2, via app.output.set_title)
should track the session name and live progress instead of staying on
whatever the launch command left it as — see PromptToolkitConsoleManager._apply_title."""

from JFI.manager.pt_console_manager import PromptToolkitConsoleManager


class _FakeOutput:
    def __init__(self):
        self.titles: list[str] = []

    def set_title(self, title):
        self.titles.append(title)


class _FakeApp:
    def __init__(self):
        self.output = _FakeOutput()

    def invalidate(self):
        pass


def _manager_with_fake_app():
    manager = PromptToolkitConsoleManager()
    manager._app = _FakeApp()
    return manager


def test_no_session_yet_falls_back_to_the_app_title():
    manager = _manager_with_fake_app()
    manager._apply_title()
    assert manager._app.output.titles[-1] == manager.title


def test_session_alone_becomes_the_title():
    manager = _manager_with_fake_app()
    manager.set_status(session="story")
    assert manager._app.output.titles[-1] == "story"


def test_phase_with_its_own_checklist_shows_progress():
    manager = _manager_with_fake_app()
    manager.set_status(session="story", phase="imp", phase_plan=(8, 18), plan=(8, 21))
    assert manager._app.output.titles[-1] == "story · Implement 8/18"


def test_phase_with_no_checklist_falls_back_to_whole_plan_progress():
    """planner/reviewer have no per-item checklist (phase_plan is (0, 0)) —
    the whole-plan total should be shown instead when it has anything to
    show, exactly like the header itself falls back."""
    manager = _manager_with_fake_app()
    manager.set_status(session="story", phase="reviewer", phase_plan=(0, 0), plan=(8, 21))
    assert manager._app.output.titles[-1] == "story · Review 8/21"


def test_phase_with_nothing_to_show_yet_is_just_the_label():
    manager = _manager_with_fake_app()
    manager.set_status(session="story", phase="planner", phase_plan=(0, 0), plan=(0, 0))
    assert manager._app.output.titles[-1] == "story · Plan"


def test_start_iteration_clears_the_phase_from_the_title():
    manager = _manager_with_fake_app()
    manager.set_status(session="story", phase="imp", phase_plan=(8, 18), plan=(8, 21))
    manager.start_iteration(2, ["planner", "imp", "testing", "reviewer"])
    assert manager._app.output.titles[-1] == "story"


def test_no_app_running_is_a_silent_no_op():
    manager = PromptToolkitConsoleManager()
    assert manager._app is None
    manager.set_status(session="story")  # must not raise


def test_a_broken_output_set_title_is_swallowed():
    manager = _manager_with_fake_app()

    def _raise(title):
        raise RuntimeError("terminal doesn't support OSC 2")

    manager._app.output.set_title = _raise
    manager.set_status(session="story")  # must not raise
