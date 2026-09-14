"""File-based bridge between a running JFI process and an external
observer/approver -- specifically the Streamlit dashboard (see
src/JFI/web/dashboard.py), which runs as a completely separate process and so
can only see this session through files on disk.

Enabled with JFI_WEB_BRIDGE=1 (see runner._web_bridge_enabled). Off by
default: writing a status snapshot on a timer is harmless either way, but
nobody who isn't running the dashboard should pay for a background thread
and a file write every tick, and existing terminal-only sessions shouldn't
gain new files in their session folder they never asked for.

Two files live inside the session's own JFI/<session>/ folder:

- web_status.json  -- written by this bridge every _POLL_SECONDS from
  console.get_status_snapshot(). Read-only from the dashboard's side.
- web_answer.json  -- written by the dashboard when a human clicks a button
  answering a pending choice (get_status_snapshot()'s "awaiting" field).
  This bridge picks it up, feeds it to console.submit_external_answer() --
  the exact same channel a terminal keypress uses, so whichever answers
  first simply wins -- and always deletes the file so a stale click can
  never be replayed against some later, unrelated choice.
"""

import json
import os
import threading
from pathlib import Path
from typing import Optional

from JFI.manager.abstract_manager import AbstractManager

STATUS_FILENAME = "web_status.json"
ANSWER_FILENAME = "web_answer.json"

_POLL_SECONDS = 0.5


def atomic_write_json(path: Path, data) -> None:
    """Write-then-rename so a concurrent reader (the dashboard, in a
    separate process) never sees a half-written file."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    os.replace(tmp, path)


class WebBridge:
    """
    Background thread mirroring one session's live status to disk and
    relaying web-submitted answers back into the console.

    Own one instance for the lifetime of a single _run_session call: start()
    once the session's SimpleSessionManager exists (so session_path is
    known), stop() in that call's `finally` alongside release_session_lock.
    """

    def __init__(self, console: AbstractManager, session_path: Path):
        self._console = console
        self._status_path = Path(session_path) / STATUS_FILENAME
        self._answer_path = Path(session_path) / ANSWER_FILENAME
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        # A stale answer left over from a previous run of this same session
        # (process killed mid-decision, etc.) must never be replayed against
        # whatever this run's first real choice turns out to be.
        try:
            self._answer_path.unlink()
        except OSError:
            pass
        self._thread = threading.Thread(target=self._run, name="jfi-web-bridge", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
        for path in (self._status_path, self._answer_path):
            try:
                path.unlink()
            except OSError:
                pass

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                snapshot = self._console.get_status_snapshot()
                atomic_write_json(self._status_path, snapshot)
                self._relay_answer(snapshot)
            except Exception:
                # This bridge is a side channel for an optional dashboard --
                # it must never take the actual pipeline down with it. Skip
                # this tick, try again next.
                pass
            self._stop.wait(_POLL_SECONDS)

    def _relay_answer(self, snapshot: dict) -> None:
        try:
            raw = self._answer_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return
        try:
            self._answer_path.unlink()
        except OSError:
            pass
        if not snapshot.get("awaiting"):
            return  # nothing pending right now -- a stale/late click, drop it
        try:
            key = json.loads(raw).get("key")
        except (json.JSONDecodeError, AttributeError):
            return
        if key:
            self._console.submit_external_answer(str(key))
