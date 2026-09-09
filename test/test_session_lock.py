"""Session locking: two processes must never run the same session_id at
once (they'd race on history.jsonl.gz/plan.md/context.json), but two
SimpleSessionManager instances for the same session_id *within this same
process* (the common test-suite pattern of building a fresh manager to
simulate a restart, or a brief Ctrl+N handoff) must not trip that guard.
See SimpleSessionManager._acquire_session_lock/release_session_lock."""

import fcntl
from pathlib import Path

import pytest

from JFI.manager.abstract_manager import AbstractManager
from JFI.session.simple_session_manager import SessionInUseError, SimpleSessionManager


def test_two_managers_for_the_same_session_in_one_process_do_not_conflict(console):
    """The test suite's own pattern (and a brief Ctrl+N overlap) — must not
    raise SessionInUseError just because this process already holds it."""
    first = SimpleSessionManager(console, "shared")
    second = SimpleSessionManager(console, "shared")
    first.release_session_lock()
    second.release_session_lock()


def test_a_genuinely_external_lock_holder_is_refused(console):
    """Simulates a real second OS process: opens and flocks the lock file
    directly, bypassing SimpleSessionManager's own in-process registry
    entirely — exactly what an unrelated process would do, unaware of it."""
    lock_path = Path("JFI") / "external" / ".lock"
    lock_path.parent.mkdir(parents=True)
    external_fd = open(lock_path, "w")
    try:
        fcntl.flock(external_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(SessionInUseError):
            SimpleSessionManager(console, "external")
    finally:
        fcntl.flock(external_fd, fcntl.LOCK_UN)
        external_fd.close()


def test_release_then_reacquire_succeeds(console):
    manager = SimpleSessionManager(console, "reacquire")
    manager.release_session_lock()
    # Must not raise -- the lock was actually released, not just refcounted
    # down while an OS-level flock lingers.
    manager2 = SimpleSessionManager(console, "reacquire")
    manager2.release_session_lock()


def test_release_is_safe_to_call_twice(console):
    manager = SimpleSessionManager(console, "double-release")
    manager.release_session_lock()
    manager.release_session_lock()  # must not raise


def test_different_session_ids_do_not_interfere(console):
    a = SimpleSessionManager(console, "session-a")
    b = SimpleSessionManager(console, "session-b")
    a.release_session_lock()
    b.release_session_lock()


def test_refcount_keeps_the_os_lock_held_until_every_claim_releases(console):
    """Releasing only one of two same-process claims must not let a real
    external process in -- the underlying flock is still legitimately
    needed by the other live SimpleSessionManager instance."""
    first = SimpleSessionManager(console, "refcounted")
    second = SimpleSessionManager(console, "refcounted")
    first.release_session_lock()

    lock_path = Path("JFI") / "refcounted" / ".lock"
    external_fd = open(lock_path, "w")
    try:
        with pytest.raises(OSError):
            fcntl.flock(external_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        external_fd.close()
        second.release_session_lock()


class _RetryConsole(AbstractManager):
    """Feeds session-name/goal answers in order; stops right after the last
    one is consumed so _run_session returns cleanly without ever reaching
    run_phase (which this test doesn't need to exercise)."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.errors = []
        self._stopped = False

    def safe_get_user_input(self, prompt_label="You", **kwargs):
        answer = self.answers.pop(0)
        if not self.answers:
            self._stopped = True
        return answer

    def should_stop(self):
        return self._stopped

    def request_stop(self):
        self._stopped = True

    def display_error(self, text):
        self.errors.append(text)

    def display_system(self, *a, **k): pass
    def display_rule(self, *a, **k): pass
    def display_user(self, *a, **k): pass
    def display_assistant(self, *a, **k): pass
    def get_user_input(self, *a, **k): return ""
    def print_agent_response(self, *a, **k): return {"content": None, "tool_calls": None}
    def set_status(self, **kwargs): pass
    def start_session_log(self, *a, **k): pass
    def set_queue_store(self, *a, **k): pass
    def start_iteration(self, *a, **k): pass
    def mark_phase_done(self, *a, **k): pass


def test_run_session_reprompts_for_a_name_instead_of_crashing_when_locked():
    from JFI.runner import _run_session

    lock_path = Path("JFI") / "taken" / ".lock"
    lock_path.parent.mkdir(parents=True)
    external_fd = open(lock_path, "w")
    fcntl.flock(external_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        console = _RetryConsole(["taken", "free", "my goal"])
        _run_session(console, {})  # llms is never indexed: we stop before run_phase

        assert len(console.errors) == 1
        assert "taken" in console.errors[0]
        assert "already running" in console.errors[0]
        assert console.answers == []  # every queued answer was actually consumed
    finally:
        fcntl.flock(external_fd, fcntl.LOCK_UN)
        external_fd.close()
