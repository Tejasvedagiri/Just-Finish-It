"""Shared fixtures: consoles and SimpleSessionManagers that are safe in tests.

The manager resolves its .JFI folder relative to SESSION_PATH (default cwd), so
every test runs inside a fresh temp directory — no real files are touched and
the plan path comes out as ".JFI/<session>/plan.md".
"""

import pytest


class Console:
    """Minimal stand-in for the rich console used by the manager.

    Records every display_system message in `system_messages` so tests can assert
    on the status-bar text (e.g. "Plan ready at ... — 2/5 items ticked.").
    """

    def __init__(self):
        self.system_messages: list[str] = []

    def display_system(self, *args, **kwargs):
        self.system_messages.append(" ".join(str(a) for a in args))

    def print(self, *args, **kwargs):
        pass


@pytest.fixture(autouse=True)
def _isolate_cwd(tmp_path, monkeypatch):
    """Run every test inside its own working directory."""
    monkeypatch.chdir(tmp_path)
    yield tmp_path


@pytest.fixture()
def console():
    return Console()


@pytest.fixture()
def manager(console):
    from JFI.session.simple_session_manager import SimpleSessionManager

    return SimpleSessionManager(console, "demo")


@pytest.fixture()
def make_manager(console):
    """Factory: build a manager for an arbitrary session id."""

    def _make(session_id="demo"):
        from JFI.session.simple_session_manager import SimpleSessionManager

        return SimpleSessionManager(console, session_id)

    return _make
