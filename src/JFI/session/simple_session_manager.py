import os
import re
from pathlib import Path
from typing import Dict

from JFI.manager.abstract_manager import AbstractManager
from JFI.models import get_engine
from JFI.session.abstract_session_manager import SessionManager
from JFI.session.history_store import append_history_to_db, has_history, load_history_from_db
from JFI.session.metadata_store import load_metadata_from_db, save_metadata_to_db
from JFI.tool.plan_db_tools import built_files, make_plan_db_tools

# flock is Unix-only (Linux/macOS), the project's targets; session locking
# degrades to a no-op on Windows rather than failing to import.
try:
    import fcntl
except ImportError:
    fcntl = None


class SessionInUseError(Exception):
    """Raised when another process already holds this project's `.jfi/`
    lock (see SimpleSessionManager._acquire_session_lock) — the lock is
    project-wide, not per session_id (see __init__'s own note on why
    `.jfi/` is flat), so only one JFI session can run against a given
    project at a time, regardless of session name."""
    pass


# Per-process registry of held session locks: {resolved lock path: [refcount,
# file_handle]} — see SimpleSessionManager._acquire_session_lock for why a
# second SimpleSessionManager for the same session_id within this same
# process must not trip the cross-process guard.
_SESSION_LOCKS: dict = {}

class SimpleSessionManager(SessionManager):
    """SessionManager backed by the project's `.jfi/JFI.db` (history,
    metadata, the plan) -- see SessionManager for the interface contract
    this fulfills."""

    def __init__(self, console: AbstractManager, session_id: str):
        self.console: AbstractManager = console
        self.session_id = session_id.lower().replace(" ", "_")

        # Path logic handled entirely inside the manager
        os_session_path = os.environ.get("SESSION_PATH", ".")
        self._project_root = Path(os_session_path)
        # Flat -- everything JFI-related (the shared DB, and the couple of
        # things still not DB-backed: llm_debug.jsonl,
        # web_status.json/web_answer.json, .lock) lives directly in
        # `.jfi/`, not in a per-session subfolder under it. Per explicit
        # decision: the DB is the real per-session store (every table
        # already keyed by session_id, and would stay that way even
        # behind a shared postgres/mysql DATABASE_URL, see JFI.models.db);
        # these few remaining files are secondary artifacts of whichever
        # session is CURRENTLY RUNNING, not meant to coexist per-session on
        # disk. Consequence: `.lock` (see _acquire_session_lock) is now
        # project-wide, not per-session_id -- only one JFI session, of any
        # name, can run against a given project at a time.
        self.session_path = self._project_root / ".jfi"
        self._lock_path = None
        self._acquire_session_lock()
        self.session_path.mkdir(parents=True, exist_ok=True)

        # DB-backed session persistence (see JFI.models and
        # JFI.tool.plan_db_tools) -- the sole store now for the plan, the
        # conversation history, context facts, and metadata (unlocked
        # tools, implemented files, queued requests, digest state). ONE
        # database per PROJECT (`.jfi/JFI.db`, everything JFI-related kept
        # inside the single hidden `.jfi/` folder so that folder alone is
        # what a project's own .gitignore needs to name), shared by every
        # session ever run there -- every table is already keyed by
        # session_id, so nothing about the schema needed to change, only
        # where the file lives. Built first, before load_history/
        # load_metadata below, since both now read from it. .lock stays a
        # file (a process mutex, not data); JFI.db is everything else.
        self.db_engine = get_engine(self._project_root)

        self.is_resuming = has_history(self.db_engine, self.session_id)

        # How many of self.history's messages are already durably persisted
        # to the DB — set by load_history(), advanced by save_history().
        self._flushed_count = 0
        self.history = self.load_history()
        self.metadata = self.load_metadata()

    def plan_db_tools(self) -> Dict:
        """{"get_plan": ..., "get_leaf": ...} bound to this session's
        own db_engine -- what runner.py's _run_session wires into
        TOOL_MAP, the same rebinding pattern as execute_command."""
        return make_plan_db_tools(self.db_engine, self.session_id)


    def _acquire_session_lock(self) -> None:
        """
        Exclusive OS-level lock (flock) on the project's own `.jfi/` folder
        — refuses to run ANY session against this project while another
        JFI process already holds it, which would otherwise let two
        processes race on the shared `.jfi/JFI.db` and its few remaining
        file-based artifacts (llm_debug.jsonl, ...) and corrupt them.
        Project-wide, not per session_id: `.jfi/` is flat (see
        __init__), so a second session name does not get its own lock
        scope to race safely within.

        Released automatically when this process exits or the underlying
        file handle is closed (see release_session_lock), even on a crash
        — flock is held by the OS against the open file description, not a
        stale PID file that would need its own cleanup/staleness logic. A
        no-op wherever fcntl isn't available (Windows).

        flock's exclusivity is scoped to the open file description, not the
        process: two independent open()s of the same path *in this same
        process* would otherwise block each other exactly like a genuinely
        different process would — which a second SimpleSessionManager
        constructed within this process legitimately does (a Ctrl+N
        handoff overlapping briefly, or simply building a fresh manager to
        inspect state while another is open elsewhere in the same run).
        _SESSION_LOCKS refcounts by resolved lock path so only a genuinely
        different process ever gets refused.
        """
        if fcntl is None:
            return
        self.session_path.mkdir(parents=True, exist_ok=True)
        lock_path = str((self.session_path / ".lock").resolve())

        entry = _SESSION_LOCKS.get(lock_path)
        if entry is not None:
            entry[0] += 1
            self._lock_path = lock_path
            return

        lock_file = open(lock_path, "w")
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            lock_file.close()
            raise SessionInUseError(
                f"A JFI session is already running in this project (lock held on "
                f"{lock_path}). Only one session can run per project at a time — "
                f"stop that run first before starting '{self.session_id}'."
            )
        _SESSION_LOCKS[lock_path] = [1, lock_file]
        self._lock_path = lock_path

    def release_session_lock(self) -> None:
        """Releases this session's claim on its lock, if held — call once
        this SimpleSessionManager is done being used (see
        runner._run_session), so a later SimpleSessionManager (this
        process or another) can acquire it. The underlying OS lock is only
        actually released once every claim on it in this process has been
        released (see _acquire_session_lock)."""
        lock_path, self._lock_path = getattr(self, "_lock_path", None), None
        if lock_path is None:
            return
        entry = _SESSION_LOCKS.get(lock_path)
        if entry is None:
            return
        entry[0] -= 1
        if entry[0] > 0:
            return
        del _SESSION_LOCKS[lock_path]
        _, lock_file = entry
        try:
            if fcntl is not None:
                fcntl.flock(lock_file, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            lock_file.close()
        except OSError:
            pass


    # ------------------------------------------------------------- metadata

    def _repo_path(self) -> str:
        return str(Path(os.environ.get("SESSION_PATH", ".")))

    def load_metadata(self):
        """Loads the session's metadata (queued requests) from the DB -- the
        replacement for metadata.json (see JFI.session.metadata_store)."""
        return load_metadata_from_db(self.db_engine, self.session_id, self._repo_path())

    def save_metadata(self):
        """Persists project metadata to the DB."""
        save_metadata_to_db(self.db_engine, self.session_id, self._repo_path(), self.metadata)

    def load_queued_requests(self) -> list[str]:
        """Plain-queued (not yet consumed) console input from a prior run of
        this session, if any — see :meth:`save_queued_requests`."""
        return list(self.metadata.get("queued_requests", []))

    def save_queued_requests(self, items: list) -> None:
        """
        Persists the console's *current* queued-input contents to
        metadata.json, called by the console every time that queue changes
        (something typed in, or the whole queue drained/promoted).

        The queue used to live only in the console's in-memory
        ``queue.Queue`` — closing the process (even cleanly) lost anything
        the user had queued but the pipeline hadn't reached the "review
        landed, drain queue" point for yet. Persisting it here means
        `console.set_queue_store(ssm.load_queued_requests(), ...)` on the
        next resume can hand it right back.
        """
        self.metadata["queued_requests"] = list(items)
        self.save_metadata()

    def get_project_state_summary(self) -> str:
        """The files built so far (the done leaves' files), for a fresh
        iteration's feedback. It used to list files the old file tools
        recorded with track_file; v2's code tools never did, so every
        re-plan was told "No files have been tracked yet." """
        files = built_files(self.db_engine, self.session_id)
        if not files:
            return "No leaf has been built yet."
        return "Files built so far:\n" + "\n".join(f"- {f}" for f in files)

    # -------------------------------------------------------------- history

    def load_history(self):
        """Loads the conversation from the DB (see JFI.session.history_store)."""
        history = load_history_from_db(self.db_engine, self.session_id) if self.is_resuming else []
        self._flushed_count = len(history)
        if history:
            self.console.display_system(f"Resumed existing session '{self.session_id}' with {len(history)} past messages.")
        return history

    def save_history(self):
        """
        Appends only the messages added since the last save, as new
        HistoryMessage rows — O(new messages), never O(total history
        already in the DB). The old pickle version rewrote the entire
        history on every call, which made each turn's save cost grow with
        the whole session's size; the gzip-append version fixed that for
        the file era, and this DB version keeps the same O(new) property.
        """
        pending = self.history[self._flushed_count:]
        if not pending:
            return
        append_history_to_db(self.db_engine, self.session_id, pending)
        self._flushed_count = len(self.history)

    def add_message(self, role, message):
        """For simple text messages."""
        self.history.append({"role": role, "content": message})
        self.save_history()


    # ---------------------------------------------------------- compression


    # --------------------------------------------------------------- phases

    def get_remaining_phases(self, all_phases: list[str]) -> list[str]:
        """
        Scans history to see which phases have already completed.
        Returns a sliced list starting from the first incomplete phase.
        """
        completed_phases = set()
        for msg in self.history:
            if msg.get("role") == "assistant" and msg.get("content"):
                for phase in all_phases:
                    if phase_completed(msg["content"], phase):
                        completed_phases.add(phase)

        # Find the first phase in sequence that hasn't completed yet
        for i, phase in enumerate(all_phases):
            if phase not in completed_phases:
                return all_phases[i:]

        # If all phases are already marked complete, return empty list
        return []


def _marker_present(content: str, keyword: str) -> bool:
    """
    True when `keyword` stands alone on its own line in `content` (markdown
    decoration -- *emphasis*, `code`, blockquote '>', a trailing '.'/'!' --
    tolerated around it). A plan that merely *mentions* the keyword in prose
    does not count: this is what phase_completed uses for '{PHASE}_COMPLETE'
    markers, and what runner.py's tiered-planner stages use for their own
    stage markers (ARCHITECT_STAGE_COMPLETE, TEAM_LEAD_STAGE_COMPLETE) --
    same rule, same regex, one place to keep them consistent.
    """
    if not content:
        return False
    for line in str(content).splitlines():
        if re.fullmatch(rf"[\s*_`#>-]*{keyword}[\s*_`.!:]*", line):
            return True
    return False


def phase_completed(content: str, phase: str) -> bool:
    """
    True when the model signed off on a phase.

    The keyword must stand alone on its own line: a plan that merely *mentions*
    'IMP_COMPLETE' in prose used to end the phase instantly.
    """
    return _marker_present(content, f"{phase.upper()}_COMPLETE")
