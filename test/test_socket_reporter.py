"""SocketReporter's pure helpers (src/JFI/manager/socket_reporter.py) --
the identity it sends to a remote master is opaque (hostname + session id)
and never a filesystem path, since the master has no way to resolve a path
on a different machine anyway. Only the non-networking pieces are tested
here; the actual WebSocket send/recv loop is exercised by hand against a
running `jfi-master`, not by these fast unit tests -- but _dispatch_control
(the incoming-control-message -> AbstractManager-method mapping) is itself
pure and fully covered below.
"""

import socket

from JFI.manager.socket_reporter import SocketReporter, _repo_label, _session_key, master_ws_url


class TestSessionKey:
    def test_includes_hostname_and_session_id(self):
        key = _session_key("my-session")
        assert socket.gethostname() in key
        assert "my-session" in key

    def test_never_contains_a_path_separator(self):
        key = _session_key("my-session")
        assert "/" not in key

    def test_different_session_ids_produce_different_keys(self):
        assert _session_key("a") != _session_key("b")


class TestRepoLabel:
    def test_is_just_a_directory_name_not_a_path(self, tmp_path, monkeypatch):
        nested = tmp_path / "some" / "nested" / "MyRepo"
        nested.mkdir(parents=True)
        monkeypatch.chdir(nested)
        assert _repo_label() == "MyRepo"
        assert "/" not in _repo_label()


class TestMasterWsUrl:
    def test_unset_returns_none(self, monkeypatch):
        monkeypatch.delenv("MASTER_WS_URL", raising=False)
        assert master_ws_url() is None

    def test_blank_returns_none(self, monkeypatch):
        monkeypatch.setenv("MASTER_WS_URL", "   ")
        assert master_ws_url() is None

    def test_set_returns_stripped_value(self, monkeypatch):
        monkeypatch.setenv("MASTER_WS_URL", "  ws://example.com:8765/report  ")
        assert master_ws_url() == "ws://example.com:8765/report"


class _RecordingConsole:
    """Bare-minimum AbstractManager stand-in recording every call
    _dispatch_control makes, without needing a real console or a network."""

    def __init__(self):
        self.pause_calls: list[bool] = []
        self.queued: list[str] = []
        self.answers: list[str] = []
        self.stopped = False

    def submit_external_pause(self, paused: bool) -> None:
        self.pause_calls.append(paused)

    def submit_external_queue_item(self, text: str) -> None:
        self.queued.append(text)

    def submit_external_answer(self, key: str) -> None:
        self.answers.append(key)

    def request_stop(self) -> None:
        self.stopped = True


class TestDispatchControl:
    """The incoming {"type": "control", "action": ...} -> AbstractManager
    method mapping -- each action is a new WebSocket transport for a
    capability the local keypress/web_bridge.py file relay already have."""

    def _reporter(self, console):
        return SocketReporter(console, "ws://example.com/report", "sess-1")

    def test_pause_action_calls_submit_external_pause_true(self):
        console = _RecordingConsole()
        self._reporter(console)._dispatch_control({"action": "pause"})
        assert console.pause_calls == [True]

    def test_resume_action_calls_submit_external_pause_false(self):
        console = _RecordingConsole()
        self._reporter(console)._dispatch_control({"action": "resume"})
        assert console.pause_calls == [False]

    def test_queue_action_forwards_text(self):
        console = _RecordingConsole()
        self._reporter(console)._dispatch_control({"action": "queue", "text": "do the next thing"})
        assert console.queued == ["do the next thing"]

    def test_queue_action_with_no_text_is_a_noop(self):
        console = _RecordingConsole()
        self._reporter(console)._dispatch_control({"action": "queue"})
        assert console.queued == []

    def test_answer_action_forwards_key(self):
        console = _RecordingConsole()
        self._reporter(console)._dispatch_control({"action": "answer", "key": "r"})
        assert console.answers == ["r"]

    def test_answer_action_with_no_key_is_a_noop(self):
        console = _RecordingConsole()
        self._reporter(console)._dispatch_control({"action": "answer"})
        assert console.answers == []

    def test_stop_action_calls_request_stop(self):
        console = _RecordingConsole()
        self._reporter(console)._dispatch_control({"action": "stop"})
        assert console.stopped is True

    def test_unknown_action_is_ignored_not_a_crash(self):
        console = _RecordingConsole()
        self._reporter(console)._dispatch_control({"action": "something_future_and_unknown"})
        assert console.pause_calls == console.queued == console.answers == []
        assert console.stopped is False
