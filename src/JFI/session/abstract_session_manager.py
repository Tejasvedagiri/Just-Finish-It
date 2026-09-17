from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional


class SessionManager(ABC):
    """
    Abstract interface for a JFI session's own persistence/state layer --
    everything runner.py needs from a session regardless of how (or where)
    it actually stores history, the plan, and its bookkeeping files.

    SimpleSessionManager (gzip'd JSONL history + a plan.md checkbox tree on
    local disk) is the only implementation today; this exists so a second
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
    - plan_path: where the plan lives -- surfaced verbatim to the model in
      every phase trigger message so it knows where to read/write it.
    - context_cache_path: backing store for the context_save/context_lookup
      tools and the execute_command approval gate's persisted "Save"
      prefixes.
    - is_resuming: True when this session_id already had history before this
      process started -- runner.py skips the goal prompt and any
      already-completed phases when this is true.
    - history: the full raw message list so far, oldest first -- runner.py
      only ever reads this (to check whether a phase trigger was already
      inserted); all writes go through add_message/append_raw below.

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

    # ------------------------------------------------------------- the plan

    @abstractmethod
    def ensure_plan_file(self) -> bool:
        """Creates an empty plan if the planner phase somehow completed
        without writing one. Returns True if it had to create one."""
        raise NotImplementedError

    @abstractmethod
    def plan_progress(self) -> tuple:
        """(ticked, total) checkbox counts across the whole plan."""
        raise NotImplementedError

    @abstractmethod
    def phase_progress(self, phase: str) -> tuple:
        """(ticked, total) checkbox counts within just this phase's own
        section of the plan -- (0, 0) for phases with no per-item checklist
        (planner/reviewer)."""
        raise NotImplementedError

    @abstractmethod
    def current_task_title(self, phase: str, max_len: int = 140) -> Optional[str]:
        """The next not-yet-ticked leaf's text for `phase`, for the live
        status line -- None if nothing is pending or `phase` has no
        per-item checklist."""
        raise NotImplementedError

    @abstractmethod
    def skip_current_task(self, phase: str) -> Optional[str]:
        """User-initiated (Ctrl+K): marks the first pending leaf in `phase`
        skipped and returns its text, or None if nothing was pending."""
        raise NotImplementedError

    @abstractmethod
    def skip_remaining_tasks(self, phase: str) -> int:
        """User-initiated (Ctrl+Q): marks every pending leaf in `phase`
        skipped. Returns how many were skipped."""
        raise NotImplementedError

    @abstractmethod
    def get_remaining_phases(self, all_phases: List[str]) -> List[str]:
        """Which of `all_phases` (in order) have not already completed for
        this session -- lets a resumed run skip straight past finished
        phases."""
        raise NotImplementedError

    # ----------------------------------------------------------- messages

    @abstractmethod
    def get_messages(self, phase: str) -> List[Dict[str, Any]]:
        """The full message list to send the LLM for `phase`'s next turn --
        history plus that phase's system message, compressed/trimmed to fit
        the context budget as needed."""
        raise NotImplementedError

    @abstractmethod
    def add_message(self, role: str, message: str) -> None:
        """Appends one plain-text message and persists it immediately."""
        raise NotImplementedError

    @abstractmethod
    def append_raw(self, message_dict: Dict[str, Any]) -> None:
        """Appends one already-shaped message dict (an assistant turn with
        tool_calls, a tool result, ...) and persists it immediately."""
        raise NotImplementedError

    @abstractmethod
    def save_history(self) -> None:
        """Flushes any not-yet-persisted history to durable storage --
        called explicitly on paths that mutate history without going
        through add_message/append_raw (e.g. sanitizing a malformed tool
        call in place)."""
        raise NotImplementedError

    @abstractmethod
    def estimate_request_tokens(self, messages: List[Dict[str, Any]]) -> int:
        """Cheap token-count estimate for `messages`, used for the header's
        read/written counters when the server doesn't report real usage."""
        raise NotImplementedError

    @abstractmethod
    def token_usage(self) -> tuple:
        """(estimated tokens used, context window) for the current phase's
        next request -- feeds the header's ctx meter."""
        raise NotImplementedError

    # -------------------------------------------------------------- misc

    @abstractmethod
    def track_file(self, file_path: str) -> None:
        """Records that the model touched `file_path` (write_file/
        append_to_file/replace_in_file), for get_project_state_summary."""
        raise NotImplementedError

    @abstractmethod
    def get_project_state_summary(self) -> str:
        """A short prose summary of what's been touched so far this
        session, folded into a fresh iteration's opening message."""
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
