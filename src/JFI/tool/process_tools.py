"""Structured background-process management -- a safer, scoped alternative
to the model freehanding `ps`/`kill`/`pkill` through execute_command.

Observed failure mode this exists to prevent (the run-jfi skill's own
"common failure patterns" section names it as a real recurring one): a
broad `pkill -f <pattern>` meant to kill a test server the model itself
started instead matches execute_command's own wrapper shell (or an
unrelated process sharing part of the pattern) and kills that instead.

These four tools sidestep the whole class of bug by only ever operating on
a process THIS module itself started: start_background_process returns an
opaque handle (never the raw pid) that list_processes/stop_background_process
take, so there is no pattern-matching step to get wrong, and nothing here
can ever touch a process it didn't launch. Each process is started in its
own process group (start_new_session=True) so a server framework that forks
helper/reloader children (uvicorn --reload, npm run dev, ...) is stopped as
a whole group, not just its immediate pid.

Registry is in-memory and per-process (this Python process's lifetime,
i.e. one JFI session) -- there is no persistence across a restart, matching
execute_command's own lack of persistence for anything it starts. Two
things DO carry this state further, both built on snapshot_processes()
below so there is exactly one source of truth for what a process "is":
  - make_process_tools binds start/stop_background_process to also mirror
    each process's command/pid/host/port/status into the session's own
    context cache (context_tools.py), one key per handle -- the same
    "survives history compression, and a resumed session" durability the
    model's own context_save facts already get.
  - PromptToolkitConsoleManager.get_status_snapshot includes
    snapshot_processes()'s structured data directly, so it flows to BOTH
    web UIs for free: web_bridge.py mirrors the whole snapshot to
    web_status.json for jfi-web, and socket_reporter.py mirrors the same
    snapshot over the wire to jfi-master -- neither had to be taught
    anything new about processes specifically, they already relay whatever
    the snapshot contains.
"""

import os
import signal
import subprocess
import time
from typing import Callable, Dict, List

_registry: Dict[str, subprocess.Popen] = {}
_metadata: Dict[str, dict] = {}
_next_id = 0

_CONTEXT_KEY_PREFIX = "bg_process:"


def _new_handle() -> str:
    global _next_id
    _next_id += 1
    return f"bg{_next_id}"


def snapshot_processes() -> List[dict]:
    """Structured, live view of every process this session has started --
    handle, command, pid, host/port (when given), log_file, elapsed
    seconds, and status ("running" or "exited (code N)"). The single
    source of truth list_processes (string form), make_process_tools'
    context-cache mirror, and get_status_snapshot's UI-facing field all
    build on, so the three can never describe the same process
    differently."""
    result = []
    for handle, proc in _registry.items():
        meta = _metadata[handle]
        code = proc.poll()
        status = "running" if code is None else f"exited (code {code})"
        result.append({
            "handle": handle,
            "command": meta["command"],
            "pid": meta["pid"],
            "host": meta.get("host"),
            "port": meta.get("port"),
            "log_file": meta.get("log_file"),
            "status": status,
            "elapsed": round(time.time() - meta["started_at"]),
        })
    return result


def _describe(entry: dict) -> str:
    """One-line human-readable summary of one snapshot_processes() entry."""
    parts = [f"cmd={entry['command']!r}", f"pid={entry['pid']}"]
    if entry.get("host"):
        parts.append(f"host={entry['host']}")
    if entry.get("port"):
        parts.append(f"port={entry['port']}")
    if entry.get("log_file"):
        parts.append(f"log={entry['log_file']}")
    parts.append(f"status={entry['status']}")
    return " ".join(parts)


def start_background_process(command: str, log_file: str = "", host: str = "", port: str = "") -> str:
    """Starts `command` as a detached background process (e.g. a dev/test
    server) and returns a handle for use with list_processes/
    stop_background_process. The handle, not the OS pid, is what those
    tools take. Give `log_file` to capture stdout+stderr somewhere you can
    read it back (with read_file or `tail`, not by attaching to this
    process's own output); omitted, output is discarded. Give `host`/`port`
    when you know them (you're the one constructing `command`) so they're
    recorded alongside the pid -- e.g. host="127.0.0.1", port="8000" -- and
    shown in the web dashboards, not just to you."""
    command = (command or "").strip()
    if not command:
        return "Error: start_background_process needs a non-empty command."

    handle = _new_handle()
    log_file = log_file.strip()
    if log_file:
        stdout = open(log_file, "w")
        stderr = subprocess.STDOUT
    else:
        stdout = subprocess.DEVNULL
        stderr = subprocess.DEVNULL

    try:
        proc = subprocess.Popen(
            command,
            shell=True,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            start_new_session=True,  # own process group -- see module docstring
        )
    except OSError as e:
        return f"Error: failed to start '{command}': {e}"

    _registry[handle] = proc
    _metadata[handle] = {
        "command": command,
        "pid": proc.pid,
        "started_at": time.time(),
        "log_file": log_file or None,
        "host": host.strip() or None,
        "port": port.strip() or None,
    }
    suffix = f", logging to {log_file}" if log_file else " (output discarded, no log_file given)"
    return f"Started '{command}' as {handle} (pid {proc.pid}){suffix}"


def list_processes() -> str:
    """Lists every process started via start_background_process THIS
    SESSION (never the whole OS process table) -- handle, pid, running/
    exited state, and exit code once it has exited. Use this instead of
    `ps aux`/`ps -ef` to find a background process this session itself
    started; there is nothing here to grep or pattern-match."""
    processes = snapshot_processes()
    if not processes:
        return "No background processes started this session."
    return "\n".join(f"{p['handle']}: {_describe(p)} elapsed={p['elapsed']}s" for p in processes)


def stop_background_process(handle: str, timeout: float = 5.0) -> str:
    """Stops a process by ITS HANDLE (from start_background_process/
    list_processes) -- never a raw pid or a name pattern, so this can never
    match and kill something this session didn't itself start (the
    self-inflicted `pkill -f` failure this tool set exists to prevent).
    Sends SIGTERM to the whole process group first, then SIGKILL if it
    hasn't exited within `timeout` seconds.

    Waits on the PROCESS GROUP's existence, not on Popen.wait() alone: with
    shell=True, `proc` tracks the SHELL WRAPPER, not the command it ran --
    a wrapper dies from an unhandled SIGTERM well before a grandchild that
    explicitly ignores it does, so trusting proc.wait() here would report
    "stopped cleanly" while that grandchild (e.g. the actual dev server)
    is still running, orphaned, still bound to its port. Polling the group
    instead of the single tracked pid is what makes "stopped" mean the
    whole group is actually gone."""
    handle = (handle or "").strip()
    proc = _registry.get(handle)
    if proc is None:
        return f"Error: no background process with handle {handle!r} (see list_processes)."

    code = proc.poll()
    if code is not None:
        return f"{handle} already exited (code {code})."

    pid = _metadata[handle]["pid"]
    try:
        pgid = os.getpgid(pid)
    except ProcessLookupError:
        return f"{handle} (pid {pid}) was already gone."
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return f"{handle} (pid {pid}) was already gone."

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        # Reap the tracked wrapper the instant IT exits (Popen.poll() is a
        # non-blocking waitpid) -- otherwise it sits as a zombie, and a
        # zombie's pid still satisfies killpg(pgid, 0) below, which would
        # make a lone wrapper process look "still alive" forever and starve
        # this loop until the timeout even though it already exited.
        proc.poll()
        try:
            os.killpg(pgid, 0)  # existence probe -- delivers no signal, just checks the group is non-empty
        except ProcessLookupError:
            return f"Stopped {handle} (pid {pid}) cleanly."
        time.sleep(0.1)

    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        pass
    return f"{handle} (pid {pid}) didn't stop within {timeout:.0f}s — force-killed."


def clear_finished_processes() -> str:
    """Prunes every EXITED process's bookkeeping entry from the registry --
    never a still-running one, and never sends any signal (the underlying
    OS process is already gone by the time this runs). Exists because a
    long session that starts/stops many one-off verification servers
    otherwise accumulates dead entries in list_processes/the dashboard's
    background-processes panel forever, with no way to tell "still
    relevant" from "leftover noise" at a glance."""
    finished = [handle for handle, proc in _registry.items() if proc.poll() is not None]
    for handle in finished:
        del _registry[handle]
        del _metadata[handle]
    if not finished:
        return "No exited processes to clear."
    return f"Cleared {len(finished)} exited process(es): {', '.join(finished)}."


# ---------------------------------------------------------------------------
# Context-cache mirroring -- make_process_tools binds start/stop to also
# write a durable record of each process into the session's own context
# cache, the same per-session binding pattern as
# cmd_tools.make_gated_execute_command / context_tools.make_context_tools.
# ---------------------------------------------------------------------------

def _mirror_to_context(handle: str, cache_path: str) -> None:
    from JFI.tool.context_tools import context_save  # local import: avoid a hard dependency for callers that never bind a cache

    entry = next((p for p in snapshot_processes() if p["handle"] == handle), None)
    if entry is None:
        return
    context_save(_CONTEXT_KEY_PREFIX + handle, _describe(entry), cache_path)


def make_process_tools(cache_path: str) -> Dict[str, Callable]:
    """{"start_background_process": ..., "stop_background_process": ...},
    each bound to one session's context.json so a process's command/pid/
    host/port/status survives history compression and session resumption
    as an ordinary context-cache fact (key "bg_process:<handle>") -- not
    just live in this process's memory. list_processes needs no binding;
    it already reads the live registry directly."""

    def bound_start(command: str, log_file: str = "", host: str = "", port: str = "") -> str:
        result = start_background_process(command, log_file=log_file, host=host, port=port)
        if result.startswith("Started "):
            handle = result.split(" as ", 1)[1].split(" ", 1)[0]
            _mirror_to_context(handle, cache_path)
        return result

    def bound_stop(handle: str, timeout: float = 5.0) -> str:
        result = stop_background_process(handle, timeout=timeout)
        _mirror_to_context(handle, cache_path)
        return result

    return {
        "start_background_process": bound_start,
        "stop_background_process": bound_stop,
    }
