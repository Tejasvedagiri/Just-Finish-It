"""Socket-based counterpart to web_bridge.py's file-based bridge: mirrors
one session's live status to a remote "master" (frontend/server/master.js,
the Node-based fleet dashboard server) over a WebSocket instead of writing
JFI/<session>/web_status.json to disk -- the mechanism a session on one
machine uses to report into a fleet dashboard running on a different one,
where there is no shared filesystem to write a status file into in the
first place.

Bidirectional: alongside pushing status on its own timer, this also LISTENS
on the same connection for control messages the master relays from a
viewer's browser (pause/resume/queue/answer/stop -- see _dispatch_control),
so the fleet dashboard can drive a session, not just observe it. Each
action maps onto an existing AbstractManager method a local keypress or
web_bridge.py's own file-based relay already use, so this is a new
transport for existing capabilities, not new session behavior.

Enabled by setting MASTER_WS_URL (see runner._master_ws_url), e.g.
MASTER_WS_URL=ws://203.0.113.5:8765/report -- unset means this stays
completely inert, same "off by default, opt in" posture as JFI_WEB_BRIDGE.
The two are independent and can run at once: MASTER_WS_URL never touches
JFI_WEB_BRIDGE's files, and vice versa.

A reporting session is identified to the master purely by an opaque `key`
(hostname + session id -- see _session_key) and a short display `repo`
label (see _repo_label) -- never a filesystem path. The master has no way
to read this machine's disk anyway once it's remote, so the protocol never
assumes it can.
"""

import asyncio
import json
import os
import socket
import threading
from pathlib import Path
from typing import Optional

from JFI.manager.abstract_manager import AbstractManager

_POLL_SECONDS = 1.0
_RECONNECT_BACKOFF_SECONDS = 3.0


def _session_key(session_id: str) -> str:
    """Opaque identity sent to the master: this machine's hostname plus the
    session id -- never a filesystem path, so it stays meaningful even
    though the master cannot resolve anything on this machine's disk."""
    return f"{socket.gethostname()}::{session_id}"


def _repo_label() -> str:
    """Short display label for which project this session is working in --
    just the current directory's own name, not the path to it."""
    return Path.cwd().name


class SocketReporter:
    """
    Background-thread counterpart to WebBridge, for a remote master instead
    of a local dashboard. Own one instance for the lifetime of a single
    _run_session call: start() once the SessionManager exists (so
    session_id is known), stop() in that call's `finally`, same lifecycle
    WebBridge already has.

    Runs its own asyncio event loop on a dedicated thread (the rest of the
    console/session machinery is synchronous, not asyncio-native) and
    reconnects with a fixed backoff on any drop -- a flaky link between two
    machines must never take the actual pipeline down with it, so every
    failure here is swallowed, exactly like WebBridge's own tick loop.
    """

    def __init__(self, console: AbstractManager, master_ws_url: str, session_id: str):
        self._console = console
        self._url = master_ws_url
        self._key = _session_key(session_id)
        self._repo = _repo_label()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="jfi-socket-reporter", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(self._reconnect_loop())
        finally:
            loop.close()

    async def _reconnect_loop(self) -> None:
        import websockets

        while not self._stop.is_set():
            try:
                async with websockets.connect(self._url, open_timeout=10) as ws:
                    await ws.send(json.dumps({"type": "hello", "key": self._key, "repo": self._repo}))
                    # Bidirectional from here: this side keeps pushing status
                    # on its own timer while concurrently listening for
                    # control messages the master relays from a viewer (see
                    # _recv_control_loop) -- pause/resume/queue/answer/stop,
                    # the same actions a local keypress/web_bridge already
                    # support, now reachable from a remote fleet dashboard
                    # with no filesystem access to this machine. TaskGroup so
                    # either side failing (send finds the socket dead, recv
                    # sees it close) tears down both and falls through to the
                    # same reconnect-with-backoff every other failure here does.
                    async with asyncio.TaskGroup() as tg:
                        tg.create_task(self._send_status_loop(ws))
                        tg.create_task(self._recv_control_loop(ws))
            except Exception:
                # Connection refused, DNS failure, dropped mid-session, the
                # master not running yet -- all the same to this side: wait
                # and try again. This is a side channel for an optional
                # remote dashboard; it must never interrupt the real run.
                await asyncio.sleep(_RECONNECT_BACKOFF_SECONDS)

    async def _send_status_loop(self, ws) -> None:
        while not self._stop.is_set():
            snapshot = self._console.get_status_snapshot()
            await ws.send(json.dumps({"type": "status", "key": self._key, "data": snapshot}))
            await asyncio.sleep(_POLL_SECONDS)

    async def _recv_control_loop(self, ws) -> None:
        async for message in ws:
            try:
                payload = json.loads(message)
            except (json.JSONDecodeError, TypeError):
                continue
            if payload.get("type") == "control":
                self._dispatch_control(payload)

    def _dispatch_control(self, payload: dict) -> None:
        """One incoming {"type": "control", "action": ...} message from the
        master (relayed from a viewer's browser -- see master.js) -- each
        action maps straight onto the same AbstractManager methods a local
        keypress or web_bridge.py's file-based relay already drive, so this
        is a new transport for existing capabilities, not new behavior."""
        action = payload.get("action")
        if action == "pause":
            self._console.submit_external_pause(True)
        elif action == "resume":
            self._console.submit_external_pause(False)
        elif action == "queue":
            text = payload.get("text")
            if text:
                self._console.submit_external_queue_item(str(text))
        elif action == "answer":
            key = payload.get("key")
            if key:
                self._console.submit_external_answer(str(key))
        elif action == "stop":
            self._console.request_stop()


def master_ws_url() -> Optional[str]:
    """MASTER_WS_URL in .env -- e.g. ws://203.0.113.5:8765/report. Blank or
    unset disables the reporter entirely. Read fresh each call, same
    reasoning as runner._show_stream_prompts: cheap, checked rarely."""
    value = os.environ.get("MASTER_WS_URL", "").strip()
    return value or None
