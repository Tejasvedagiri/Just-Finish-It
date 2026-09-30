from abc import ABC, abstractmethod
from typing import List


class SessionManager(ABC):
    """
    Abstract interface for a JFI session's own persistence/state layer --
    everything runner.py needs from a session regardless of how (or where)
    it actually stores history, the plan, and its bookkeeping files.

    SimpleSessionManager (the project's `.jfi/JFI.db`) is the only
    implementation today; this exists so a second
    one (a database-backed session, a remote/shared session, ...) can be
    dropped into runner.py's `ssm` slot without runner.py itself changing.

    Every method below is REQUIRED -- runner.py calls all of it somewhere in
    the pipeline. Unlike AbstractManager (console output), there is no
    "optional polish" tier here: a session manager that can't do all of this
    can't drive a session at all.

    Implementations are also expected to set these instance attributes
    (plain attributes, not properties -- kept out of the abstract-method
    list below so a subclass is free to set them however it likes, e.g.
    computed once in __init__):

    - session_path: this session's own bookkeeping folder (a filesystem Path
      for SimpleSessionManager, but callers only ever pass it straight
      through to WebBridge / build a log path from it -- any object your
      __init__ hands back and later methods are happy to receive back does
      the job).
    - db_engine / session_id: the DB-backed context_save/context_lookup
      tools and the execute_command approval gate's persisted "Save"
      prefixes are both keyed by these, not by a file path (see
      JFI.tool.context_tools / JFI.tool.cmd_tools).
    - is_resuming: True when this session_id already had history before this
      process started -- runner.py skips the goal prompt and any
      already-completed phases when this is true.
    - history: the full raw message list so far, oldest first -- runner.py
      only ever reads this (to check whether a phase trigger was already
      inserted); all writes go through add_message below.

    A constructor is deliberately not declared here (same as AbstractManager
    declares none): implementations take whatever they need to open/create a
    session (SimpleSessionManager takes `(console, session_id)` and raises
    SessionInUseError if another process already holds this session_id's
    lock), and errors on that path are implementation-defined.
    """

    # ------------------------------------------------------------ lifecycle

    @abstractmethod
    def release_session_lock(self) -> None:
        """Releases whatever exclusivity this session holds, so a later
        Ctrl+N restart or another process can reuse this session_id. Must be
        safe to call unconditionally (finally-block cleanup)."""
        raise NotImplementedError

    # ------------------------------------------------------------- phases

    @abstractmethod
    def get_remaining_phases(self, all_phases: List[str]) -> List[str]:
        """Which of `all_phases` (in order) have not already completed for
        this session -- lets a resumed run skip straight past finished
        phases."""
        raise NotImplementedError

    # ----------------------------------------------------------- messages

    @abstractmethod
    def add_message(self, role: str, message: str) -> None:
        """Appends one plain-text message and persists it immediately."""
        raise NotImplementedError

    @abstractmethod
    def save_history(self) -> None:
        """Flushes any not-yet-persisted history to durable storage --
        called explicitly on paths that mutate history without going
        through add_message (e.g. sanitizing a malformed tool
        call in place)."""
        raise NotImplementedError

    @abstractmethod
    def get_project_state_summary(self) -> str:
        """What has been built so far this session, folded into a fresh
        iteration's feedback."""
        raise NotImplementedError

    @abstractmethod
    def load_queued_requests(self) -> List[str]:
        """Requests queued but never drained before the process last
        closed -- handed back to the console's queue on startup."""
        raise NotImplementedError

    @abstractmethod
    def save_queued_requests(self, items: List[str]) -> None:
        """Persists the console's current queue contents -- wired up as its
        on_change callback, called every time the queue changes."""
        raise NotImplementedError
