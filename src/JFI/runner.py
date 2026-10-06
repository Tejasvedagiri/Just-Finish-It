import argparse
import os
import shutil
import signal
import socket
import subprocess
import sys
from importlib.metadata import PackageNotFoundError, version as _package_version
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from dotenv import find_dotenv, load_dotenv

# LLM and Console Management
from JFI.llm.backend_select import make_llm_stream
from JFI.llm.base_llm_stream import BaseLLMStream
from JFI.manager.abstract_manager import AbstractManager
from JFI.manager.pt_console_manager import PromptToolkitConsoleManager
from JFI.manager.web_bridge import WebBridge
from JFI.manager.socket_reporter import SocketReporter, master_ws_url as _read_master_ws_url

# Session Management
from JFI.session.abstract_session_manager import SessionManager
from JFI.create_env_detect import startup_warning
from JFI.session.pipeline import session_pipeline
from JFI.session.simple_session_manager import (
    SessionInUseError, SimpleSessionManager, _marker_present,
)

# Tools and Schemas
from JFI.tool.file_tools import write_file, read_file, append_to_file, replace_in_file
from JFI.tool.cmd_tools import execute_command, make_gated_execute_command
from JFI.tool.context_tools import make_context_tools
from JFI.tool.note_tools import clear_note, get_note, make_note_tools, REVIEW_REPORT, REVIEWER_NOTES
from JFI.tool.image_tools import capture_screenshot, view_image
from JFI.tool.plan_db_tools import plan_status_fields
from JFI.tool.web_tools import fetch_webpage_images
from JFI.tool.browser_session import browser, close_browser
from JFI.tool.browser_tools import browse_webpage, check_page
from JFI.tool.http_tools import http_request
from JFI.tool.video_tools import extract_video_frames
from JFI.tool.process_tools import (
    start_background_process, list_processes, stop_background_process,
    clear_finished_processes, make_process_tools,
)


# Dynamic mapping of tool names to their python functions
TOOL_MAP = {
    "write_file": write_file,
    "read_file": read_file,
    "append_to_file": append_to_file,
    "replace_in_file": replace_in_file,
    "execute_command": execute_command,
    "capture_screenshot": capture_screenshot,
    "fetch_webpage_images": fetch_webpage_images,
    "browse_webpage": browse_webpage,
    "check_page": check_page,
    "browser": browser,
    "http_request": http_request,
    "extract_video_frames": extract_video_frames,
    "start_background_process": start_background_process,
    "list_processes": list_processes,
    "stop_background_process": stop_background_process,
    "clear_finished_processes": clear_finished_processes,
    # view_image returns (status_text, data_url) instead of a plain string —
    # every other tool's TOOL_MAP entry returns str; see the isinstance(tuple)
    # check in execute_tool_call, which is the one place that distinction
    # matters. Kept in TOOL_MAP anyway so the generic "unknown tool" /
    # signature-introspection error paths still cover it uniformly.
    "view_image": view_image,
    # Rebound to the actual session's own DB engine in _run_session, same
    # as execute_command below — these defaults only matter before a
    # session exists (import time, direct testing).
    "context_save": lambda key="", value="": "Error: context_save is not available yet — no session is active.",
    "context_lookup": lambda keyword="": "Error: context_lookup is not available yet — no session is active.",
    "add_reviewer_note": lambda text="": "Error: add_reviewer_note is not available yet — no session is active.",
    "get_reviewer_notes": lambda: "Error: get_reviewer_notes is not available yet — no session is active.",
    "write_review_report": lambda text="": "Error: write_review_report is not available yet — no session is active.",
    # Rebound to the actual session's DB engine in _run_session via
    # ssm.plan_db_tools() (see JFI.tool.plan_db_tools) — these defaults
    # only matter before a session exists.
    "get_plan": lambda: "Error: get_plan is not available yet — no session is active.",
    "get_leaf": lambda leaf_id=0: "Error: get_leaf is not available yet — no session is active.",
}

#: The phase keys are persisted in history (`<PHASE>_COMPLETE` markers drive
#: resume) and mirrored by the fleet dashboard: never rename them. v1's
#: `product_owner` and `testing` phases are gone -- the planner's judge settles
#: every node (laya_plan.md D3) and every leaf carries its own unit test (D10).
PHASES = ["planner", "imp", "reviewer", "cleanup"]

# .env prefix each phase's model/endpoint override is read from (see
# JFI.llm.base_llm_stream.phase_env) — e.g. PLANNER_MODEL, PLANNER_OPENAI_URL,
# PLANNER_OPENAI_API_KEY, PLANNER_TEMPERATURE. Any that are unset fall back to
# the shared MODEL/OPENAI_URL/OPENAI_API_KEY/TEMPERATURE, so a single model
# for every phase (today's default) needs no per-phase vars at all.
PHASE_ENV_PREFIX = {"planner": "PLANNER", "imp": "IMP", "reviewer": "REVIEWER", "cleanup": "CLEANUP"}

EXIT_WORDS = {"exit", "done", "quit", "no", "nothing"}


# ------------------------------------------------------------------ LLM errors


# --------------------------------------------------------------- tool running


# ------------------------------------------------------------------ the queue


def _clear_reviewer_notes(ssm: SessionManager) -> None:
    """Clears this session's SessionNote(kind=REVIEWER_NOTES) row (see
    JFI.tool.note_tools) once the reviewer phase has finished reading it
    (see get_system_message's imp/reviewer branches -- imp calls
    add_reviewer_note whenever a step hit a problem worth flagging;
    reviewer calls get_reviewer_notes before deciding PASS/FAIL). Cleared
    here at the harness level, unconditionally and regardless of PASS/FAIL,
    rather than relying on the model to tidy it up itself: the notes are
    only relevant to the ONE review pass that just consumed them, and a
    stale note left behind would otherwise resurface in a later, unrelated
    review pass. Any issue a note pointed to either already made it into
    the review report (which schedules its own follow-up iteration) or was
    confirmed harmless -- the note itself has done its job either way."""
    if hasattr(ssm, "db_engine"):
        clear_note(ssm.db_engine, ssm.session_id, REVIEWER_NOTES)


MAX_REVIEW_ITERATIONS = 3

def review_outcome(engine, session_id: str) -> Optional[str]:
    """
    Post-review decision (1): a SessionNote(kind=REVIEW_REPORT) row means
    the reviewer found issues and wants another full iteration (planner →
    imp → reviewer). No row means the review was good — return
    None to end the run normally.

    The returned feedback embeds the report's full content, so the next
    planner sees every issue even after the row is cleared away.
    """
    report = get_note(engine, session_id, REVIEW_REPORT)
    if report is None:
        return None
    return (
        f"REVIEW FAILED: the reviewer found issues in the finished work. Its report:\n\n"
        f"{report}\n\n"
        f"Plan a fix for every issue listed above. Never change or delete a leaf that is "
        f"already done."
    )


def collect_next_iteration(console: AbstractManager, engine=None, session_id: Optional[str] = None) -> tuple[Optional[str], bool]:
    """
    Decides what happens once the review phase has landed.

    A failed review (a SessionNote(kind=REVIEW_REPORT) row present) and
    anything the user queued while the run was in flight are no longer
    mutually exclusive: both fold into the SAME next iteration's feedback
    when both are present, instead of a review failure starving queued
    follow-ups until some later iteration happens to pass cleanly. Nothing
    here asks for approval; with neither signal present, the pipeline
    idles with the input line live so feeding it more work stays optional.
    Returns a (feedback, review_failed) tuple — feedback is None to end the
    run; review_failed is True only when this iteration was (at least
    partly) triggered by a failed review, for the caller's loop-guard
    counter and to enrich the next planner trigger.
    """
    review_feedback = review_outcome(engine, session_id) if engine is not None else None
    if review_feedback:
        console.display_rule("🔁 REVIEW FAILED — SCHEDULING ANOTHER FULL ITERATION")
        # Remove the stale report so it cannot re-trigger a loop on its own;
        # only a freshly written review report may schedule another iteration.
        clear_note(engine, session_id, REVIEW_REPORT)

    queued = console.drain_queued_input()
    if not queued and not review_feedback:
        console.set_status(phase="", state="idle · queue empty", task="")
        console.display_rule("✅ PIPELINE COMPLETE — IDLE (queue anything to continue)")
        queued = console.wait_for_queued_input()

    requests = [q for q in (queued or []) if q.strip().lower() not in EXIT_WORDS]
    if not review_feedback and not requests:
        return None, False  # stopped, nothing queued, and the review passed

    parts = []
    if review_feedback:
        parts.append(review_feedback)
    if requests:
        console.display_rule(f"▶  RUNNING {len(requests)} QUEUED REQUEST(S)")
        for request in requests:
            console.display_user(request)
        queued_block = "\n".join(f"- {request}" for request in requests)
        if review_feedback:
            queued_block = (
                "ADDITIONALLY, the user queued these requests while this run was in "
                "flight — fold them in as new plan items alongside any review fixes "
                f"above:\n{queued_block}"
            )
        parts.append(queued_block)

    return "\n\n".join(parts), bool(review_feedback)


# ----------------------------------------------------------------- the phases

def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def _web_bridge_enabled() -> bool:
    """JFI_WEB_BRIDGE=1: mirror this session's live status to
    .jfi/web_status.json and accept answers to get_user_choice /
    get_user_input prompts, plus new queued requests, from
    .jfi/web_answer.json -- see web_bridge.WebBridge. Also makes
    run_pipeline auto-launch the jfi-web dashboard itself (see
    _launch_web_dashboard) unless JFI_WEB_DASHBOARD=0. Off by default:
    nobody who isn't running the web dashboard should pay for a background
    thread and a file write every tick."""
    return _env_flag("JFI_WEB_BRIDGE")


def _master_ws_url() -> Optional[str]:
    """MASTER_WS_URL: mirror this session's live status to a remote fleet
    dashboard (src/JFI/web/master_server.py, `jfi-master`) over a
    WebSocket -- see manager.socket_reporter.SocketReporter. Independent of
    JFI_WEB_BRIDGE/JFI_WEB_DASHBOARD (that pair is file-based and local-
    dashboard-only); this is for a master that may be running on a
    different machine entirely. Unset (the default) means this session
    reports nowhere but its own terminal/run.log, exactly as before this
    existed."""
    return _read_master_ws_url()


def _web_dashboard_disabled() -> bool:
    """JFI_WEB_DASHBOARD=0 (or false/no/off): with JFI_WEB_BRIDGE=1, ./jfi
    normally auto-launches the `jfi-web` dashboard itself as a child
    process -- see _launch_web_dashboard -- so a single command gives both
    the terminal and the browser. Set this when you want the bridge's
    status files written but the dashboard run separately, e.g. on another
    machine (the split-process setup the README's Web dashboard section
    documents) -- the bridge itself stays on regardless of this flag."""
    return os.environ.get("JFI_WEB_DASHBOARD", "").strip().lower() in ("0", "false", "no", "off")


def _web_dashboard_port() -> int:
    from JFI.web.launcher import DEFAULT_PORT
    try:
        return int(os.environ.get("JFI_WEB_PORT", "").strip() or DEFAULT_PORT)
    except ValueError:
        return DEFAULT_PORT


def _port_listening(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.3)
        return sock.connect_ex((host, port)) == 0


def _web_dashboard_command() -> Optional[List[str]]:
    """
    How to launch the dashboard as a separate process, or None if there's no
    way to.

    Prefers the real `jfi-web` console script when it's on PATH (the normal
    pip-installed/dev-venv case: cheap, and reuses whatever env that script
    itself resolves to). Falls back to re-invoking THIS SAME process's own
    executable with a hidden flag when running as a frozen standalone binary
    (see build_binary -- it bundles streamlit in via --collect-all so the
    binary is self-sufficient) -- `sys.executable` in a PyInstaller app is
    the running binary itself, so this needs no separate install.
    """
    jfi_web = shutil.which("jfi-web")
    if jfi_web:
        return [jfi_web]
    if getattr(sys, "frozen", False):
        return [sys.executable, "--internal-web-dashboard"]
    return None


def _launch_web_dashboard(console: AbstractManager) -> Optional[subprocess.Popen]:
    """
    Best-effort auto-start of the dashboard as a child process, so a single
    `./jfi` (with JFI_WEB_BRIDGE=1) gives both the terminal and the browser
    without a second command in a second terminal. Every failure here is
    reported as one display_system line, never an exception: the terminal
    is the one surface that must always work regardless of whether the
    dashboard can be started.

    Skips launching -- leaving whatever's already there alone -- when
    something is already listening on the target port: either a dashboard
    the user started themselves, or one left over from an earlier `./jfi`.
    """
    port = _web_dashboard_port()
    if _port_listening(port):
        console.display_system(f"🌐 Web dashboard already running — http://localhost:{port}")
        return None

    cmd = _web_dashboard_command()
    if cmd is None:
        console.display_system(
            "ℹ️  JFI_WEB_BRIDGE is on but 'jfi-web' isn't installed/on PATH — the dashboard "
            "was not auto-started (install it with: pip install just-finish-it[web], or set "
            "JFI_WEB_DASHBOARD=0 to silence this if you're running the dashboard elsewhere)."
        )
        return None

    try:
        # Its own process group (POSIX), so _stop_web_dashboard can stop the
        # whole tree: jfi-web is a launcher that starts Streamlit as a child.
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                start_new_session=os.name != "nt")
    except OSError as e:
        console.display_system(f"ℹ️  Web dashboard failed to start ({e}); continuing with the terminal only.")
        return None

    console.display_system(
        f"🌐 Web dashboard starting — http://localhost:{port} "
        f"(set JFI_WEB_DASHBOARD=0 to stop this auto-start)"
    )
    return proc


def _stop_web_dashboard(proc: Optional[subprocess.Popen]) -> None:
    """Stops the dashboard's whole process tree. Observed on Windows:
    jfi-web.exe -> python -> python (Streamlit); terminating only jfi-web.exe
    left the Streamlit server running, still holding the port and attached to
    the run's terminal after JFI had finished."""
    if proc is None or proc.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)
    else:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            proc.terminate()
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()


def _review_loop_approval_required() -> bool:
    """REVIEW_LOOP_APPROVAL=1: pause at a failed-review boundary for an
    explicit Approve/Reject (via get_user_choice -- answerable from the
    terminal or, with JFI_WEB_BRIDGE also on, the web dashboard) instead of
    automatically starting another fix iteration. Off by default: this
    project's whole premise is finishing autonomously, so the default stays
    exactly what it was before this flag existed."""
    return _env_flag("REVIEW_LOOP_APPROVAL")


#: Appended to history once the planner finishes (never asked of the model),
#: so get_remaining_phases' resume scan, which looks for exactly
#: f"{phase.upper()}_COMPLETE", moves past the planner.
PLANNER_PHASE_COMPLETE_MARKER = "PLANNER_COMPLETE"


#: How _run_session records the goal in history, and how _session_goal
#: finds it again on resume.
GOAL_PREFIX = "My goal is:"


def _session_goal(history: List[Dict[str, Any]]) -> str:
    """The goal the user typed, from the "My goal is: ..." message. Sessions
    created while the v1 pipeline existed have its planner instruction
    after the goal; that's cut off."""
    for m in history:
        content = m.get("content")
        if m.get("role") == "user" and isinstance(content, str) and content.startswith(GOAL_PREFIX):
            return content[len(GOAL_PREFIX):].rsplit("\n\nBuild the step-by-step plan", 1)[0].strip()
    return ""


ITERATION_FEEDBACK_PREFIX = "USER FEEDBACK FOR ITERATION:\n"
ITERATION_FEEDBACK_TAIL = "\n\nCall get_plan(), leave every done leaf"


def _replan_feedback(history: List[Dict[str, Any]]) -> str:
    """The iteration feedback (a failed review, queued requests) since the
    last finished planning round, or "" on the first planning pass. Only the
    session loop's own feedback messages count -- the phase triggers in
    between are instructions to the v1 phases, not something the user
    asked for."""
    last_done = max((i for i, m in enumerate(history) if m.get("role") == "assistant"
                     and _marker_present(m.get("content") or "", PLANNER_PHASE_COMPLETE_MARKER)), default=None)
    if last_done is None:
        return ""
    texts = [m["content"][len(ITERATION_FEEDBACK_PREFIX):].rsplit(ITERATION_FEEDBACK_TAIL, 1)[0].strip()
             for m in history[last_done + 1:]
             if m.get("role") == "user" and isinstance(m.get("content"), str)
             and m["content"].startswith(ITERATION_FEEDBACK_PREFIX)]
    return "\n\n".join(texts)


def _role_llms() -> Callable[[str], BaseLLMStream]:
    """One model client per v2 role, built on first use from its env chain
    (e.g. ARCHITECT_MODEL, else PLANNER_MODEL, else MODEL)."""
    from JFI.episode.roles import ROLE_ENV_PREFIXES

    cache: Dict[str, BaseLLMStream] = {}

    def llm_for_role(role: str) -> BaseLLMStream:
        if role not in cache:
            cache[role] = make_llm_stream(ROLE_ENV_PREFIXES[role])
        return cache[role]
    return llm_for_role


#: One judge per session (LAYA in .env), so Laya's checkpoints load once and
#: stay loaded across planning rounds and imp's re-plans (laya_plan.md D21).
_JUDGES: Dict[str, Any] = {}


def _pool_tools(llm_for_role):
    """The optional tools (JFI.episode.roles.OPTIONAL_POOL) every episode can
    load_tool, bound to this session (TOOL_MAP is rebound in _run_session)
    and, for ask_llm, to the episode's own role model. Before this, no
    episode was given them, so load_tool had nothing to load."""
    from JFI.episode.roles import OPTIONAL_POOL
    from JFI.tool.llm_tools import make_ask_llm

    def tools(role: str) -> Dict[str, Callable]:
        pool = {name: TOOL_MAP[name] for name in OPTIONAL_POOL if name in TOOL_MAP and name != "ask_llm"}
        # Only roles with it in their core set get it (the Architect probes the
        # ground truth with it); EpisodeTools drops it for the rest.
        pool["execute_command"] = TOOL_MAP["execute_command"]
        pool["ask_llm"] = make_ask_llm(llm_for_role(role))
        return pool
    return tools


def _v2_planner(console: AbstractManager, ssm: SessionManager, llm_for_role, feedback: str = ""):
    from JFI.planner.judge import make_judge
    from JFI.planner.loop import Planner

    goal = _session_goal(ssm.history)
    if ssm.session_id not in _JUDGES:
        _JUDGES[ssm.session_id] = make_judge(goal, llm=llm_for_role("architect"), log=console.display_system)
    return Planner(console, ssm.db_engine, ssm.session_id, goal, Path(ssm.session_path).parent, llm_for_role,
                   _JUDGES[ssm.session_id], feedback=feedback, pool_tools=_pool_tools(llm_for_role))


def _run_planner(console: AbstractManager, llms: Dict[str, BaseLLMStream], ssm: SessionManager) -> bool:
    """The planner (laya_plan.md §2): Architect -> judge -> Lead -> judge ->
    Task -> judge. Completion is recorded both as DB state (every node GOOD)
    and as the PLANNER_COMPLETE marker the phase-level resume reads."""
    console.set_status(phase="planner", state="thinking")
    console.display_rule("PHASE: PLANNER")
    result = _v2_planner(console, ssm, _role_llms(), _replan_feedback(ssm.history)).run()
    if not result.complete:
        console.display_error(f"Planning did not finish: {result.reason or 'stopped'}.")
        return False
    ssm.add_message("assistant", PLANNER_PHASE_COMPLETE_MARKER)
    console.display_system(f"✅ Phase 'planner' completed ({result.episodes} planner episodes).")
    console.mark_phase_done("planner")
    return True


#: The shared file/shell tools a Dev episode reuses as they are; everything
#: else Dev gets comes from the episode tool modules (JFI.imp.dev).
DEV_V1_TOOLS = ("write_file", "replace_in_file", "execute_command")


def _imp(console: AbstractManager, ssm: SessionManager, llm_for_role):
    from JFI.imp.dev import Imp

    return Imp(console, ssm.db_engine, ssm.session_id, Path(ssm.session_path).parent, llm_for_role("dev"),
               {**_pool_tools(llm_for_role)("dev"), **{name: TOOL_MAP[name] for name in DEV_V1_TOOLS}},
               replan=lambda: _v2_planner(console, ssm, llm_for_role).run()).run()


def _evidence_review(console: AbstractManager, ssm: SessionManager) -> bool:
    """EVIDENCE_REVIEW=1 (off by default): before building, wait for a person
    to accept the ground-truth evidence the Lead captured (docs/old_new.md),
    since a wrong ground truth costs every compare leaf built on it. Asked
    again only when the evidence changed since it was last accepted. False:
    the person stopped the run."""
    from sqlmodel import select

    from JFI.models import PlanEvent, get_session
    from JFI.tool.evidence_tools import evidence_dir, evidence_hash, list_cases, read_evidence, shown

    if not _env_flag("EVIDENCE_REVIEW"):
        return True
    root = Path(ssm.session_path).parent
    folder = shown(root, evidence_dir(root, ssm.session_id))
    while True:
        cases = list_cases(root, ssm.session_id)
        if not cases:
            return True
        fingerprint = evidence_hash(root, ssm.session_id, cases)
        with get_session(ssm.db_engine) as db:
            accepted = db.exec(select(PlanEvent).where(PlanEvent.session_id == ssm.session_id,
                                                       PlanEvent.type == "evidence_accepted")
                               .order_by(PlanEvent.id.desc())).first()
        if accepted is not None and accepted.detail == fingerprint:
            return True
        lines = []
        for case in cases:
            evidence = read_evidence(root, ssm.session_id, case)
            flag = "  ⚠ not verified" if evidence.unverified else ""
            lines.append(f"  {case}: {evidence.source}{flag}")
        console.display_system(f"Ground-truth evidence in {folder}/ (EVIDENCE_REVIEW=1):\n" + "\n".join(lines))
        console.set_status(state="awaiting evidence review")
        choice = console.get_user_choice(
            f"Check {folder}/ (or the dashboard's Evidence list): accept it and start building?",
            [("a", "Accept -- start building"), ("r", "I changed it -- show it again"), ("s", "Stop the run")])
        if choice == "s":
            return False
        if choice == "a":
            with get_session(ssm.db_engine) as db:
                db.add(PlanEvent(session_id=ssm.session_id, node_id=None, type="evidence_accepted",
                                 detail=evidence_hash(root, ssm.session_id, list_cases(root, ssm.session_id))))
                db.commit()
            return True


def _run_imp(console: AbstractManager, llms: Dict[str, BaseLLMStream], ssm: SessionManager) -> bool:
    """Implementation (laya_plan.md §6): one short Dev episode per leaf.
    IMP_COMPLETE is written from DB state -- every leaf finished (D27) --
    never from model text."""
    console.set_status(phase="imp", state="thinking")
    console.display_rule("PHASE: IMPLEMENTATION")
    if not _evidence_review(console, ssm):
        console.display_error("Stopped at the evidence review.")
        return False
    console.set_status(state="thinking")
    result = _imp(console, ssm, _role_llms())
    if not result.complete:
        console.display_error(f"Implementation did not finish: {result.reason or 'stopped'}.")
        return False
    ssm.add_message("assistant", "IMP_COMPLETE")
    console.display_system(f"✅ Phase 'imp' completed ({result.episodes} dev episodes).")
    console.mark_phase_done("imp")
    return True


#: The session-bound tools the reviewer episode reuses (see JFI.review).
REVIEWER_BASE_TOOLS = ("execute_command", "start_background_process", "stop_background_process", "check_page",
                       "get_plan",
                       "get_reviewer_notes", "write_review_report")


def _run_reviewer(console: AbstractManager, llms: Dict[str, BaseLLMStream], ssm: SessionManager) -> bool:
    """The reviewer (laya_plan.md §7, G8): one e2e episode. A bug in built
    code re-opens the leaf that owns it, Dev fixes just that leaf, and the
    reviewer runs again -- up to MAX_REVIEW_ITERATIONS rounds, then what's
    still failing becomes a review report. Missing work is a review report
    straight away; the run's next iteration takes it to the Architect."""
    from JFI.review import Reviewer
    from JFI.tool.note_tools import write_review_report

    llm_for_role = _role_llms()
    console.set_status(phase="reviewer", state="thinking")
    console.display_rule("PHASE: REVIEWER")
    reviewer = Reviewer(console, ssm.db_engine, ssm.session_id, Path(ssm.session_path).parent,
                        llm_for_role("reviewer"), {**_pool_tools(llm_for_role)("reviewer"),
                                                   **{name: TOOL_MAP[name] for name in REVIEWER_BASE_TOOLS}})
    for round_no in range(1, MAX_REVIEW_ITERATIONS + 1):
        result = reviewer.run()
        if result.verdict == "stopped":
            console.display_error("The review did not finish (stopped, or the model stopped responding).")
            return False
        if result.verdict != "fix":
            break
        console.display_rule(f"🔁 REVIEW: {len(result.reopened)} leaf(s) re-opened for Dev "
                             f"(round {round_no}/{MAX_REVIEW_ITERATIONS})")
        if not _imp(console, ssm, llm_for_role).complete:
            console.display_error("The fixes did not finish.")
            return False
    else:
        write_review_report(ssm.db_engine, ssm.session_id,
                            f"The reviewer re-opened leaves for {MAX_REVIEW_ITERATIONS} rounds and the e2e still "
                            f"fails; see the reviewer notes and the last fix notes.")
    ssm.add_message("assistant", "REVIEWER_COMPLETE")
    console.display_system(f"✅ Phase 'reviewer' completed ({result.verdict}).")
    console.mark_phase_done("reviewer")
    return True


def _run_cleanup(console: AbstractManager, llms: Dict[str, BaseLLMStream], ssm: SessionManager) -> bool:
    """Cleanup (laya_plan.md G12): one episode tidying the working directory."""
    from JFI.review import run_cleanup

    console.set_status(phase="cleanup", state="thinking")
    console.display_rule("PHASE: CLEANUP")
    if not run_cleanup(console, ssm.db_engine, ssm.session_id, Path(ssm.session_path).parent,
                       _role_llms()("cleanup"), {"execute_command": TOOL_MAP["execute_command"]}):
        if console.should_stop():
            return False
        console.display_error("Cleanup did not finish; the working directory may still have stray files.")
    ssm.add_message("assistant", "CLEANUP_COMPLETE")
    console.mark_phase_done("cleanup")
    return True


def run_phase(console: AbstractManager, llms: Dict[str, BaseLLMStream], ssm: SessionManager,
              phase: str) -> bool:
    """
    Drives one phase to completion. Returns False if the run should stop early
    (user interrupt or LLM failure), True when the phase finished cleanly.
    Every phase runs as scoped episodes (JFI.planner, JFI.imp, JFI.review);
    each writes its <PHASE>_COMPLETE marker from DB state, never from model
    text.
    """
    runners = {"planner": _run_planner, "imp": _run_imp, "reviewer": _run_reviewer,
               "cleanup": _run_cleanup}
    return runners[phase](console, llms, ssm)


def _run_session(console: AbstractManager, llms: Dict[str, BaseLLMStream]) -> None:
    """
    Runs exactly one session end-to-end: gather its name/goal, then drive
    planner -> imp -> reviewer -> cleanup, looping on failed
    reviews or queued follow-ups, until the review passes and the idle queue
    stays empty. Returns either because the run should stop for good
    (Ctrl+C, an unrecoverable LLM failure, the review-loop cap) or because
    the user asked to start a new session (Ctrl+N while idle) — run_pipeline
    tells the two apart via console.should_restart() and loops accordingly.
    """
    # 1. Gather Session Name — retrying if it's already locked by another
    # running JFI process (SessionInUseError) instead of racing on the same
    # history/plan/context files or crashing the whole app.
    ssm = None
    while ssm is None:
        session_name = console.safe_get_user_input("Please enter a session name to begin:", multiline=False)
        if not session_name or console.should_stop():
            return
        try:
            ssm = SimpleSessionManager(console, session_name)
        except SessionInUseError as e:
            console.display_error(str(e))
    console.set_status(session=session_name)
    if ssm.is_resuming and session_pipeline(ssm.db_engine, ssm.session_id) != "v2":
        # The v1 pipeline (tiered planner, Program Manager, testing phase) was
        # removed; its sessions can't be resumed, only exported.
        console.display_error(f"Session '{session_name}' was created by the old v1 pipeline, which JFI no "
                              f"longer has. Start a new session (`uv run export-db` still reads the old one).")
        ssm.release_session_lock()
        return
    # The server can reload a model after create-env ran: the first calc run
    # died with LM Studio at 4,096 tokens while .env said 38,000.
    context_warning = startup_warning(os.environ)
    if context_warning:
        console.display_error(context_warning)
    web_bridge: Optional[WebBridge] = None
    socket_reporter: Optional[SocketReporter] = None
    try:
        # 2. Gate execute_command behind the human's approval; approved "Save"
        # prefixes live in this session's context cache (ContextEntry rows).
        TOOL_MAP["execute_command"] = make_gated_execute_command(console, ssm.db_engine, ssm.session_id)
        TOOL_MAP.update(make_context_tools(ssm.db_engine, ssm.session_id))
        TOOL_MAP.update(make_process_tools(ssm.db_engine, ssm.session_id))
        TOOL_MAP.update(make_note_tools(ssm.db_engine, ssm.session_id))
        TOOL_MAP.update(ssm.plan_db_tools())  # get_plan / get_leaf, for the reviewer
        console.start_session_db_log(ssm.db_engine, ssm.session_id)
        if _web_bridge_enabled():
            web_bridge = WebBridge(console, ssm.session_path)
            web_bridge.start()
        master_url = _master_ws_url()
        if master_url:
            socket_reporter = SocketReporter(
                console, master_url, session_id=ssm.session_id,
                db_engine=getattr(ssm, "db_engine", None),
            )
            socket_reporter.start()
        # Requests queued but never drained before the process closed (killed,
        # crashed, or just quit) are QueuedItem rows — hand them back now, and
        # persist the queue from here on so this doesn't happen again.
        console.set_queue_store(ssm.load_queued_requests(), ssm.save_queued_requests)

        if ssm.is_resuming:
            console.display_system(f"📁 Resuming existing session '{session_name}'...")
            initial_goal = "(Resuming previous session goal from history)"
        else:
            initial_goal = console.safe_get_user_input("What is your goal? (Be as detailed as possible):", multiline=True)
            if not initial_goal or console.should_stop():
                return
            ssm.add_message("user", f"{GOAL_PREFIX} {initial_goal}")

        # 3. Resumed sessions pick up at the first phase that never completed
        iteration = 1
        console.start_iteration(iteration, PHASES)
        console.set_status(**plan_status_fields(ssm.db_engine, ssm.session_id))
        active_phases = ssm.get_remaining_phases(PHASES)
        skipped = [p for p in PHASES if p not in active_phases]
        for phase in skipped:
            console.mark_phase_done(phase)
        if ssm.is_resuming and skipped:
            console.display_system(f"⏩ Skipping completed phases: {', '.join(skipped).upper()}")

        # 4. Outer Loop for Re-runs and Feedback
        review_failed = False   # set when a failed review re-iteration starts
        review_failures = 0     # counts consecutive failed reviews (loop guard)
        while not console.should_stop():
            if not active_phases:
                console.display_system("All phases have already been completed for this session.")

            for phase in active_phases:
                if not run_phase(console, llms, ssm, phase):
                    return

                if phase == "reviewer":
                    _clear_reviewer_notes(ssm)

            # 5. Review has landed: a review report (failed review) or queued
            # requests loop us round again, no prompt.
            feedback, review_failed = collect_next_iteration(console, ssm.db_engine, ssm.session_id)
            if feedback is None:
                break

            if review_failed:
                review_failures += 1
                console.display_rule(f"🔎 REVIEW FAIL #{review_failures}/{MAX_REVIEW_ITERATIONS} THIS SESSION")
                if review_failures >= MAX_REVIEW_ITERATIONS:
                    console.display_rule(
                        f"⛔ REVIEW LOOP CAP REACHED ({MAX_REVIEW_ITERATIONS} failed reviews) — "
                        f"ending the run so a failing cycle is visible here instead of looping forever. "
                        f"Queue another request to keep going."
                    )
                    break

                if _review_loop_approval_required():
                    console.set_status(state="awaiting review approval")
                    decision = console.get_user_choice(
                        "Review failed — start another fix iteration?",
                        [("y", "Approve — run another iteration"), ("n", "Reject — stop this session")],
                    )
                    if decision != "y":
                        console.display_rule("⛔ REVIEW ITERATION REJECTED — ending the run.")
                        break

            ssm.add_message(
                "user",
                f"{ITERATION_FEEDBACK_PREFIX}{feedback}\n\n"
                f"{ssm.get_project_state_summary()}{ITERATION_FEEDBACK_TAIL} as it is and plan only the new "
                f"work."
            )
            # Fresh pass: the header rewinds to planner and counts the loop.
            iteration += 1
            active_phases = PHASES
            console.start_iteration(iteration, PHASES)
    finally:
        # Release the lock unconditionally (Ctrl+C, a run_phase failure, the
        # review cap, natural completion, ...) so a later Ctrl+N restart —
        # or another process entirely — can use this session_id again
        # without waiting for this one to actually exit.
        if web_bridge is not None:
            web_bridge.stop()
        if socket_reporter is not None:
            socket_reporter.stop()
        close_browser()
        ssm.release_session_lock()


def run_pipeline(console: AbstractManager, llms: Dict[str, BaseLLMStream]) -> None:
    console.set_status(phases=PHASES, state="waiting")
    console.display_rule("Just Finish It — Generic Autonomous Mode 🤖")
    console.display_system(
        "Type at any time. Enter queues a request for after the review phase; "
        "prefix with ! to run it in the current turn. Once a review completes and "
        "the queue is idle, Ctrl+N starts a brand-new session from scratch."
    )

    web_dashboard_proc = None
    if _web_bridge_enabled() and not _web_dashboard_disabled():
        web_dashboard_proc = _launch_web_dashboard(console)

    try:
        while True:
            _run_session(console, llms)
            # should_restart() is only ever set by Ctrl+N, which is only live
            # while _run_session was idling on wait_for_queued_input — so it
            # can't be true after a Ctrl+C stop or an unrecoverable failure.
            if console.should_stop() or not console.should_restart():
                break
            console.clear_restart()
            console.display_rule("🔄 STARTING A NEW SESSION (Ctrl+N)")
    finally:
        _stop_web_dashboard(web_dashboard_proc)

    console.set_status(phase="", state="finished", task="")
    console.display_rule("🎉 JUST FINISH IT — SESSION TERMINATED 🎉")


def _version() -> str:
    """The installed package version (pyproject.toml's [project] name is
    "just-finish-it"), or a clear fallback for the launcher's no-install
    path (PYTHONPATH=src python3 -m JFI.runner), which has no distribution
    metadata to read."""
    try:
        return _package_version("just-finish-it")
    except PackageNotFoundError:
        return "unknown (not installed as a package)"


def _parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="jfi",
        description="Just Finish It -- a multi-phase, plan-driven coding agent for local LLMs.",
    )
    parser.add_argument("--version", action="store_true", help="Print the installed version and exit.")
    parser.add_argument("--check-browser", action="store_true",
                        help="Open the headless browser JFI uses (check_page, screenshots) on a test page and exit.")
    # Internal only -- not meant to be typed by a person. This is how
    # _launch_web_dashboard re-invokes a frozen standalone binary to serve
    # the dashboard itself when no separate `jfi-web` is on PATH (see
    # _web_dashboard_command); suppressed from --help accordingly.
    parser.add_argument("--internal-web-dashboard", action="store_true", help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def main():
    args = _parse_args()
    if args.version:
        print(f"JFI {_version()}")
        return

    if args.check_browser:
        from JFI.tool.browser_tools import browser_check
        message = browser_check()
        print(message)
        raise SystemExit(1 if message.startswith("Error") else 0)

    if args.internal_web_dashboard:
        from JFI.web.launcher import main as run_web_dashboard
        run_web_dashboard(extra_args=[])
        return

    # Load the project's .env from an explicit path (searched upward from the
    # current working directory) BEFORE any console/theme code runs, so a user
    # setting THEME=... in their .env is honored no matter how JFI was launched.
    # usecwd=True: without it, find_dotenv searches upward from THIS file's
    # folder when run from source, so `uv run jfi` in a project never saw that
    # project's .env (the frozen binary already used the cwd).
    load_dotenv(find_dotenv(usecwd=True))

    console = PromptToolkitConsoleManager()
    # One stream per phase (see PHASE_ENV_PREFIX) — each falls back to the
    # shared MODEL/OPENAI_URL/OPENAI_API_KEY/TEMPERATURE/LLM_BACKEND when
    # that phase has no .env override, so this is one shared connection in
    # the common case and up to six independent ones (even across
    # different BACKENDS — e.g. REVIEWER_LLM_BACKEND=anthropic while every
    # other phase stays on a local OpenAI-compatible server) when a user
    # has configured per-phase overrides. See llm/backend_select.py for
    # which backend LLM_BACKEND picks.
    llms = {phase: make_llm_stream(prefix) for phase, prefix in PHASE_ENV_PREFIX.items()}

    try:
        # The console owns the terminal and runs the pipeline on a worker
        # thread, which is what keeps the bottom input line alive throughout.
        console.run(lambda: run_pipeline(console, llms))
    finally:
        for llm in llms.values():
            llm.close()
        # Full-screen UI is gone by now; replay the transcript into scrollback.
        console.dump_transcript()
        # Erase everything (screen + scrollback) so a closed run — Ctrl+C,
        # normal completion, or an early return before the pipeline started —
        # leaves the user's shell a clean screen. Best-effort by design; it
        # must never raise through main()'s exit path.
        console.clear_console()


if __name__ == "__main__":
    main()
