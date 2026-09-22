"""Session locking: two processes must never run ANY session against the
same project at once (they'd race on the shared .jfi/JFI.db and its few
remaining file-based artifacts) -- the lock is PROJECT-WIDE now, scoped to
the flat `.jfi/` folder, not per session_id (see SimpleSessionManager.
__init__'s own note on why `.jfi/` is flat). But two SimpleSessionManager
instances *within this same process* (the common test-suite pattern of
building a fresh manager to simulate a restart, or a brief Ctrl+N
handoff), even for different session_ids, must not trip that guard --
they share this process's own in-process refcount registry regardless of
session name, since the lock file itself is the same one path either way.
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
    lock_path = Path(".jfi") / ".lock"
    lock_path.parent.mkdir(parents=True)
    external_fd = open(lock_path, "w")
    try:
        fcntl.flock(external_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(SessionInUseError):
            SimpleSessionManager(console, "external")
    finally:
        fcntl.flock(external_fd, fcntl.LOCK_UN)
        external_fd.close()


def test_a_genuinely_external_holder_refuses_a_different_session_id_too(console):
    """The lock is project-wide now, not per session_id (see module
    docstring) -- an external process holding it must refuse EVERY session
    name, not just the one it happened to start with."""
    lock_path = Path(".jfi") / ".lock"
    lock_path.parent.mkdir(parents=True)
    external_fd = open(lock_path, "w")
    try:
        fcntl.flock(external_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(SessionInUseError):
            SimpleSessionManager(console, "some-other-session-name")
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


def test_different_session_ids_in_the_same_process_share_the_one_lock(console):
    """Not "do not interfere" in the old per-session-lock sense (that
    guarantee no longer exists -- see module docstring): this only proves
    two different session_ids from the SAME process don't spuriously trip
    SessionInUseError against each other, because they resolve to the
    exact same lock path (`.jfi/.lock`) and refcount together, same as two
    managers for the identical session_id would."""
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

    lock_path = Path(".jfi") / ".lock"
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


def test_run_session_reprompts_instead_of_crashing_when_locked():
    """The lock is project-wide now (see module docstring), so trying a
    DIFFERENT session name no longer helps while another process holds
    it -- unlike the old per-session lock, where the retry loop's whole
    point was that a different name would succeed. What still matters,
    and is still true: a SessionInUseError must never crash the process,
    just display the error and keep re-prompting until the caller gives
    up (should_stop) or the external holder eventually releases it."""
    from JFI.runner import _run_session

    lock_path = Path(".jfi") / ".lock"
    lock_path.parent.mkdir(parents=True)
    external_fd = open(lock_path, "w")
    fcntl.flock(external_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        # A 3rd name is queued but never actually attempted: popping the
        # LAST answer sets should_stop() before the loop gets back around
        # to trying it (see _RetryConsole.safe_get_user_input) -- same
        # "every queued answer consumed, loop exits cleanly" shape the
        # original version of this test relied on for its successful-retry
        # case, just with no success possible here since the external lock
        # never releases.
        console = _RetryConsole(["taken", "still-locked", "never-attempted"])
        _run_session(console, {})  # llms is never indexed: we stop before run_phase

        assert len(console.errors) == 2
        for error in console.errors:
            assert "already running" in error
        assert console.answers == []  # every queued answer was actually consumed
    finally:
        fcntl.flock(external_fd, fcntl.LOCK_UN)
        external_fd.close()
