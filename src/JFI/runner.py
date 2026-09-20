import argparse
import inspect
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from datetime import datetime
from importlib.metadata import PackageNotFoundError, version as _package_version
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotenv import find_dotenv, load_dotenv
from openai import APIConnectionError, APIStatusError

# LLM and Console Management
from JFI.llm.backend_select import make_llm_stream
from JFI.llm.base_llm_stream import BaseLLMStream
from JFI.manager.abstract_manager import AbstractManager, ResponseTooLongError, phase_display_name
from JFI.manager.pt_console_manager import PromptToolkitConsoleManager
from JFI.manager.web_bridge import WebBridge
from JFI.manager.socket_reporter import SocketReporter, master_ws_url as _read_master_ws_url

# Session Management
from JFI.session.abstract_session_manager import SessionManager
from JFI.session.adaptive_session_manager import AdaptiveSessionManager
from JFI.session.simple_session_manager import (
    DEFAULT_CONTEXT_CACHE_PATH, SessionInUseError, SimpleSessionManager, get_phase_trigger, _marker_present,
)

# Tools and Schemas
from JFI.tool.schemas import CORE_TOOLS, DEFERRED_TOOLS
from JFI.tool.file_tools import write_file, read_file, append_to_file, replace_in_file
from JFI.tool.cmd_tools import execute_command, make_gated_execute_command
from JFI.tool.context_tools import make_context_tools
from JFI.tool.deferred_tools import make_load_tool
from JFI.tool.image_tools import capture_screenshot, view_image
from JFI.tool.llm_tools import make_ask_llm
from JFI.tool.web_tools import fetch_webpage_images
from JFI.tool.browser_tools import browse_webpage
from JFI.tool.video_tools import extract_video_frames
from JFI.tool.process_tools import (
    start_background_process, list_processes, stop_background_process,
    clear_finished_processes, make_process_tools,
)

# Looked up by name at request time — see _tools_for_session.
_DEFERRED_TOOLS_BY_NAME = {t["function"]["name"]: t for t in DEFERRED_TOOLS}


def _tools_for_session(ssm: SessionManager) -> list:
    """CORE_TOOLS (always sent) plus whichever DEFERRED_TOOLS `ssm` has
    unlocked via load_tool — see tool/schemas.py's module docstring for why
    this split exists (every request used to pay for all ~13 tools'
    schemas, including 5 browser/media ones most sessions never touch).
    A SessionManager without unlocked_tools() (shouldn't happen for either
    concrete implementation today, see abstract_session_manager.py) just
    gets CORE_TOOLS, same as a session that hasn't unlocked anything."""
    if not hasattr(ssm, "unlocked_tools"):
        return CORE_TOOLS
    extra = [
        _DEFERRED_TOOLS_BY_NAME[name]
        for name in ssm.unlocked_tools()
        if name in _DEFERRED_TOOLS_BY_NAME
    ]
    return CORE_TOOLS + extra if extra else CORE_TOOLS

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
    # Rebound to the actual session's context.json in _run_session, same as
    # execute_command above — these defaults only matter before a session
    # exists (import time, direct testing).
    **make_context_tools(DEFAULT_CONTEXT_CACHE_PATH),
    # Rebound to the active phase's own LLM stream in run_phase (a phase can
    # have its own model/endpoint — see PHASE_ENV_PREFIX) — this default
    # only matters before any phase has run.
    "ask_llm": lambda prompt="": "Error: ask_llm is not available yet — no phase is currently running.",
    # Rebound to the actual session's SessionManager in _run_session, same
    # as execute_command/context_save/context_lookup above — this default
    # only matters before a session exists.
    "load_tool": lambda name="": "Error: load_tool is not available yet — no session is active.",
}

PHASES = ["planner", "product_owner", "imp", "testing", "reviewer", "cleanup"]

# .env prefix each phase's model/endpoint override is read from (see
# JFI.llm.base_llm_stream.phase_env) — e.g. PLANNER_MODEL, PLANNER_OPENAI_URL,
# PLANNER_OPENAI_API_KEY, PLANNER_TEMPERATURE. Any that are unset fall back to
# the shared MODEL/OPENAI_URL/OPENAI_API_KEY/TEMPERATURE, so a single model
# for every phase (today's default) needs no per-phase vars at all.
PHASE_ENV_PREFIX = {
    "planner": "PLANNER", "product_owner": "PRODUCT_OWNER", "imp": "IMP", "testing": "TESTING",
    "reviewer": "REVIEWER", "cleanup": "CLEANUP",
}

EXIT_WORDS = {"exit", "done", "quit", "no", "nothing"}

# How many times the same failing call is coached before we tell the model to
# abandon that approach entirely.
FAILURE_RETRY_LIMIT = 3

# How many times a transient LLM-server failure (connection drop, 5xx) is
# retried before giving up on this turn — see _is_retryable_llm_error.
LLM_RETRY_LIMIT = 2
LLM_RETRY_DELAY_SECONDS = 3.0

# Defaults for _task_stuck_time_limit / _task_stuck_token_limit (see
# _check_task_stuck) — how long the SAME plan leaf (ssm.current_task_title)
# may stay current before run_phase forces a decomposition nudge instead of
# letting it grind on one oversized leaf indefinitely. Observed in practice:
# a leaf that turned out to hide real diagnose-and-fix work (not just a
# quick check) ran for hours and 700k+ tokens of re-sent context without
# ever splitting itself up, spawning a pile of throwaway scripts along the
# way. Either threshold alone can fire; both are generous defaults meant to
# catch genuine sprawl, not a normal multi-turn leaf.
DEFAULT_TASK_STUCK_TIME_LIMIT_SECONDS = 300.0
DEFAULT_TASK_STUCK_TOKEN_LIMIT = 150_000


# ------------------------------------------------------------------ LLM errors

def _is_retryable_llm_error(e: Exception) -> bool:
    """
    Worth retrying: a network-level failure (server down/restarting, a
    dropped connection), a 5xx-class server error — both are typically
    transient on a local LLM server — or a response that blew past
    STREAM_OUTPUT_CAP (see ResponseTooLongError): the model was still
    going, not finished, so re-sending the identical turn is worth another
    shot. NOT a 4xx client error: retrying an identical request the server
    already rejected (bad request, auth, context-length) won't produce a
    different result.
    """
    if isinstance(e, (APIConnectionError, ResponseTooLongError)):
        return True
    if isinstance(e, APIStatusError):
        return e.status_code >= 500
    return False


def _format_llm_error(e: Exception) -> str:
    """
    Turns an LLM call failure into a message worth reading.

    The openai SDK embeds the raw response body verbatim into str(e) when a
    server error isn't valid JSON (see its _make_status_error_from_response):
    a server crash that falls back to a framework's generic HTML error page
    — nginx, Flask, Werkzeug, whatever's fronting the model — dumps that
    whole page into the exception message instead of a clean API error. That
    HTML page is what a raw `f"...: {e}"` was printing verbatim. Detected via
    status_code/response (present on openai.APIStatusError and its
    subclasses), not by string-sniffing the message.
    """
    status_code = getattr(e, "status_code", None)
    response = getattr(e, "response", None)
    body_text = getattr(response, "text", None) if response is not None else None

    if status_code is not None and body_text and body_text.strip()[:15].lower().lstrip().startswith(("<!doctype", "<html")):
        preview = " ".join(body_text.split())[:200]
        ellipsis = "…" if len(body_text) > 200 else ""
        return (
            f"HTTP {status_code} — the server returned an HTML error page instead of a "
            f"proper API error. This is almost always a crash or restart on the LLM "
            f"server's own side, not something this request caused; check its logs. "
            f"Preview: {preview}{ellipsis}"
        )
    return str(e)


def _interruptible_sleep(console: AbstractManager, seconds: float, poll: float = 0.2) -> None:
    """time.sleep(seconds), but checks console.should_stop() every `poll`
    seconds so Ctrl+C during a retry delay is responsive instead of waiting
    out the full delay first."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline and not console.should_stop():
        time.sleep(min(poll, max(0.0, deadline - time.monotonic())))


# --------------------------------------------------------------- tool running

def _is_failure(result: Any) -> bool:
    """Every tool reports failure with a leading 'Error'."""
    return str(result).lstrip().startswith("Error")


def _expected_arguments(func_name: str) -> str:
    try:
        return f"{func_name}({', '.join(inspect.signature(TOOL_MAP[func_name]).parameters)})"
    except (KeyError, ValueError, TypeError):
        return func_name


def _repair_directive(func_name: str, args: Dict[str, Any], result: str, attempt: int) -> str:
    """
    Concrete next action for a failed call, so the model corrects itself instead
    of re-issuing the same call. Generic advice gets ignored; naming the exact
    tool and argument to change does not.
    """
    if attempt >= FAILURE_RETRY_LIMIT:
        return (
            f"AUTO-RECTIFY: this identical {func_name} call has now failed {attempt} times. "
            "Stop repeating it. Either solve the step a different way, or if it genuinely "
            "cannot be done, leave its checkbox unticked, note the blocker in the plan file, "
            "and move on to the next unchecked item."
        )

    lowered = str(result).lower()

    if func_name == "replace_in_file":
        if "not found" in lowered:
            return (
                "AUTO-RECTIFY: the file was NOT modified. read_file "
                f"'{args.get('file_path', '')}' and copy the target line exactly as it appears, "
                "including its leading '- ' and indentation, then call replace_in_file again."
            )
        if "appears" in lowered and "times" in lowered:
            return (
                "AUTO-RECTIFY: the file was NOT modified because old_string matched several "
                "places. Extend old_string with the line above or below it so it matches once."
            )

    if func_name in ("read_file", "write_file", "append_to_file", "view_image", "extract_video_frames") \
            and "does not exist" in lowered:
        return (
            "AUTO-RECTIFY: that path is wrong. Run execute_command with "
            "'ls -la .' (or the parent directory) to find the real path, then retry."
        )

    if func_name == "extract_video_frames":
        if "not installed" in lowered:
            return (
                "AUTO-RECTIFY: no video processing capability in this environment (the "
                "'ffmpeg' binary is missing) — retrying will not help. Skip this step, note "
                "the blocker in the plan, and continue without extracted frames."
            )
        if "no distinct frames" in lowered:
            return (
                "AUTO-RECTIFY: retry the same call with a lower threshold (e.g. 0.1) to catch "
                "subtler scene changes."
            )

    if func_name == "execute_command":
        if "timed out after" in lowered:
            current_timeout = args.get("timeout", 300)
            next_timeout = max(int(current_timeout) * 2, 900)
            return (
                f"AUTO-RECTIFY: the command did not fail — it simply needed more than "
                f"{current_timeout}s (no STDERR is shown because nothing went wrong, it just "
                f"wasn't finished yet). This is normal for package installs/downloads and "
                f"builds. Re-run the SAME command again, this time passing "
                f"timeout={next_timeout} to execute_command. Do not add flags or change the "
                "command to work around this — a longer timeout is the fix."
            )
        return (
            "AUTO-RECTIFY: the command failed — read the STDERR above and fix the cause "
            "(missing dependency, wrong path, syntax error) before re-running it. Do not "
            "re-run the identical command unchanged."
        )

    if func_name == "capture_screenshot" and ("no display" in lowered or "not installed" in lowered):
        return (
            "AUTO-RECTIFY: no screenshot capability in this environment (no display, or the "
            "'mss' package is missing) — retrying will not help. Skip this step, note the "
            "blocker in the plan, and continue without a screenshot."
        )

    return (
        f"AUTO-RECTIFY: the {func_name} call failed and nothing was changed. Diagnose the "
        "message above and issue a corrected call."
    )


def _announce_context_lookup_hit(console: AbstractManager, result: str) -> None:
    """
    Surfaces a successful context_lookup as its own system line — "found
    this, cost that" — separate from the raw tool-result dump, the same way
    a history compression gets its own "Context compressed: ..." line
    instead of being buried in the turn. Silent for the two no-content
    cases (empty cache, no keyword match): both lack a "- key: ..." line,
    which is what every real hit (the blank-keyword index listing included)
    always has, so this needs no separate success/failure signal from
    context_lookup itself.
    """
    keys = re.findall(r"^- (.+?):", result, re.M)
    if not keys:
        return
    tokens_estimate = (len(result) + 3) // 4
    console.display_system(
        f"🔎  Found context → {', '.join(keys)} (loaded ~{tokens_estimate} tokens)"
    )


def execute_tool_call(console: AbstractManager, ssm: SessionManager,
                      tool_call: Dict[str, Any], failures: Dict[tuple, int]) -> tuple[str, Optional[str]]:
    """
    Runs one tool call and turns any failure into actionable guidance.

    Every error path returns a string rather than raising, so a bad call costs a
    turn instead of killing the run. Returns (tool_result_text, image_data_url):
    image_data_url is non-None only for a successful view_image call — the
    caller appends it as a follow-up message so the model actually sees the
    image (a tool result itself must be plain text on the wire).
    """
    func_name = tool_call["function"]["name"]
    args_str = tool_call["function"]["arguments"]

    # --- arguments that aren't valid JSON (usually a truncated payload) ---
    try:
        args = json.loads(args_str or "{}")
    except json.JSONDecodeError as e:
        console.display_tool_call(func_name)
        # Keep the transcript valid: an oversized broken payload would be
        # replayed on every later turn.
        tool_call["function"]["arguments"] = json.dumps(
            {"error": "malformed json stripped to prevent server crash"}
        )
        ssm.save_history()
        return (
            f"Error: the arguments for {func_name} were not valid JSON ({e}). Nothing ran. "
            "This usually means you emitted too much text in one call. AUTO-RECTIFY: split the "
            "work up — write_file the first chunk, then append_to_file the rest, or use "
            "replace_in_file for a small edit."
        ), None

    if not isinstance(args, dict):
        console.display_tool_call(func_name)
        return (
            f"Error: the arguments for {func_name} must be a JSON object, got "
            f"{type(args).__name__}. AUTO-RECTIFY: retry as {_expected_arguments(func_name)}."
        ), None

    # --- a tool that doesn't exist ---
    if func_name not in TOOL_MAP:
        console.display_tool_call(func_name, args)
        return (
            f"Error: there is no tool called '{func_name}'. AUTO-RECTIFY: use one of "
            f"{', '.join(sorted(TOOL_MAP))} instead."
        ), None

    console.display_tool_call(func_name, args)

    # --- wrong or missing arguments ---
    image_data_url: Optional[str] = None
    try:
        raw_result = TOOL_MAP[func_name](**args)
    except TypeError as e:
        raw_result = (
            f"Error: wrong arguments for {func_name} ({e}). "
            f"AUTO-RECTIFY: the signature is {_expected_arguments(func_name)}."
        )
    except Exception as e:
        raw_result = f"Error executing tool {func_name}: {e}"

    # view_image is the one tool that returns (status_text, data_url) instead
    # of a plain string; every other tool's result is used as-is.
    if isinstance(raw_result, tuple):
        result, image_data_url = raw_result
    else:
        result = raw_result

    # --- coach, then escalate, on repeated identical failures ---
    signature = (func_name, args_str)
    if _is_failure(result):
        failures[signature] = failures.get(signature, 0) + 1
        attempt = failures[signature]
        console.display_error(f"{func_name} failed (attempt {attempt})")
        result = f"{result}\n\n{_repair_directive(func_name, args, result, attempt)}"
        image_data_url = None
    else:
        failures.pop(signature, None)
        if func_name in ("write_file", "append_to_file", "replace_in_file"):
            ssm.track_file(args.get("file_path"))
        elif func_name == "context_lookup":
            _announce_context_lookup_hit(console, result)

    return result, image_data_url


# ------------------------------------------------------------------ the queue

def drain_forced_input(console: AbstractManager, ssm: SessionManager) -> None:
    """
    Folds forced input ('!something') into the AI's very next turn, mid-phase.
    Plain queued input is left alone for :func:`collect_next_iteration`.
    """
    for note in console.drain_forced_input():
        ssm.add_message(
            "user",
            f"USER INTERJECTION (apply this from here on):\n{note}"
        )


def handle_skip_request(console: AbstractManager, ssm: SessionManager, phase: str) -> None:
    """
    Ctrl+K: skips the current checklist item outright, independent of
    whatever the model is doing right now — marks it "- [○]" in the plan
    (see SimpleSessionManager.skip_current_task) rather than waiting for the
    model to agree to move on. Only imp/testing have an item to skip;
    anywhere else (or with nothing pending) this is a no-op.
    """
    if not console.drain_skip_request():
        return
    skipped = ssm.skip_current_task(phase)
    if skipped is None:
        console.display_system("Nothing to skip right now.")
        return
    console.display_system(f"⏭  Skipped: {skipped}")
    ssm.add_message(
        "user",
        "USER ACTION: the current task was skipped (marked \"- [○]\" in the plan) — "
        "it is done with, not something you should redo or revert. Move on to the "
        "next unchecked item."
    )


def handle_skip_all_request(console: AbstractManager, ssm: SessionManager, phase: str) -> None:
    """
    Ctrl+Q: skips every remaining checklist item in the current phase in one
    go (see SimpleSessionManager.skip_remaining_tasks), then tells the model
    directly to wrap the phase up — marking the items alone doesn't end the
    phase, since completion is still driven by the model emitting its exact
    "<PHASE>_COMPLETE" phrase.
    """
    if not console.drain_skip_all_request():
        return
    skipped = ssm.skip_remaining_tasks(phase)
    if not skipped:
        console.display_system("Nothing to skip right now.")
        return
    console.display_system(f"⏭  Skipped {skipped} remaining item(s) in this phase.")
    ssm.add_message(
        "user",
        f"USER ACTION: every remaining item in this phase's checklist was skipped "
        f"(marked \"- [○]\" in the plan) — they are done with, not something you "
        f"should redo or revert. Finish up now and output the exact phrase "
        f"'{phase.upper()}_COMPLETE' on its own line."
    )


def _clear_reviewer_notes(ssm: SessionManager) -> None:
    """Removes JFI/<session>/NotesForReviewer.md once the reviewer phase has
    finished reading it (see get_system_message's imp/reviewer branches --
    imp appends a note there whenever a step hit a problem worth flagging;
    reviewer reads it before deciding PASS/FAIL). Cleared here at the
    harness level, unconditionally and regardless of PASS/FAIL, rather than
    relying on the model to tidy it up itself: the notes are only relevant
    to the ONE review pass that just consumed them, and a stale note left
    behind would otherwise resurface in a later, unrelated review pass. Any
    issue a note pointed to either already made it into review.md (which
    schedules its own follow-up iteration) or was confirmed harmless -- the
    note itself has done its job either way."""
    try:
        (ssm.session_path / "NotesForReviewer.md").unlink(missing_ok=True)
    except OSError:
        pass


def _read_plan_markdown(ssm: SessionManager) -> str:
    """Raw current text of plan.md, for AbstractManager.set_status's
    plan_markdown param -- a fleet-dashboard viewer has no filesystem access
    to read plan.md itself, so the full checklist rides along in the status
    snapshot. Empty string before the planner has written the file yet (no
    session ever starts implementation without one, so this is transient)."""
    try:
        return Path(ssm.plan_path).read_text(encoding="utf-8")
    except OSError:
        return ""


def _run_product_owner_loop(console: AbstractManager, llms: Dict[str, BaseLLMStream],
                             ssm: SessionManager, initial_goal: str) -> bool:
    """
    The planner<->product_owner cycle: a SEPARATE, tighter loop from the
    reviewer's own planner->imp->testing->reviewer restart, living entirely
    between the planner's first pass and implementation ever starting (see
    PHASES -- "product_owner" sits right after "planner"). Nothing has been
    implemented yet at this point, so a round trip here costs one planner
    turn and one product_owner turn, not a whole pipeline re-run.

    Called once the outer phase loop in _run_session reaches "product_owner"
    -- by then "planner" has already had its own first run via that same
    loop's generic path, so this function's own first iteration is
    product_owner's first run, not a second planner run.

    Returns True to let the outer loop continue on to "imp"; False means
    the whole run should stop (mirrors every other "if not run_phase(...):
    return" call site in _run_session).
    """
    feedback_path = str(ssm.session_path / "feedback_to_plan.md")
    rounds = 0

    while True:
        trigger = get_phase_trigger("product_owner", initial_goal, ssm.plan_path)
        last_user_msg = next((m.get("content", "") for m in reversed(ssm.history) if m.get("role") == "user"), "")
        if trigger not in last_user_msg:
            ssm.add_message("user", trigger)
        if not run_phase(console, llms, ssm, "product_owner"):
            return False

        feedback = product_owner_feedback_outcome(feedback_path)
        # Cleared immediately and unconditionally, whether or not there was
        # feedback to act on -- this file's presence is a one-shot signal
        # for THIS round only; a stale copy must never resurface in a later,
        # unrelated round. Never deferred to cleanup or any later pass.
        try:
            Path(feedback_path).unlink(missing_ok=True)
        except OSError:
            pass

        if feedback is None:
            console.display_rule("✅ PRODUCT OWNER APPROVED — proceeding to implementation")
            return True

        rounds += 1
        console.display_rule(f"🔁 PRODUCT OWNER REQUESTED CHANGES #{rounds}/{MAX_PRODUCT_OWNER_ITERATIONS}")
        if rounds >= MAX_PRODUCT_OWNER_ITERATIONS:
            console.display_rule(
                f"⛔ PRODUCT OWNER LOOP CAP REACHED ({MAX_PRODUCT_OWNER_ITERATIONS} rounds) — "
                f"ending the run so a plan that can't satisfy review is visible here instead of "
                f"looping forever. Fix the plan by hand, or queue a request to keep going."
            )
            return False

        ssm.add_message("user", feedback)
        planner_trigger = get_phase_trigger("planner", initial_goal, ssm.plan_path, po_feedback_path=feedback_path)
        ssm.add_message("user", planner_trigger)
        if not run_phase(console, llms, ssm, "planner"):
            return False
        # Loop back to the top: product_owner reviews the updated plan again.


MAX_REVIEW_ITERATIONS = 3

# How many planner<->product_owner rounds this LOCAL loop (see
# _run_product_owner_loop) allows before giving up and stopping the run --
# a bad plan that never satisfies Product Owner needs a human, not more
# rounds. Deliberately its own cap, separate from MAX_REVIEW_ITERATIONS:
# this loop runs entirely before implementation even starts, so a low cap
# here costs far less than one on the reviewer's loop (which is guarding a
# much more expensive planner->imp->testing->reviewer full pass).
MAX_PRODUCT_OWNER_ITERATIONS = 3


def product_owner_feedback_outcome(feedback_path: str) -> Optional[str]:
    """
    Mirrors review_outcome() for the planner<->product_owner loop: a
    generated feedback_to_plan.md means Product Owner found a real problem
    with the plan and wants the planner to fix it before implementation
    starts. No file means the plan was approved as-is — return None.

    The returned feedback embeds the report's full content, so the planner
    sees every point even after the file is cleared away.
    """
    if not Path(feedback_path).exists():
        return None
    try:
        report = Path(feedback_path).read_text(encoding="utf-8")
    except OSError:
        report = "(the feedback file could not be read — re-inspect the plan and the repo)"
    return (
        f"PRODUCT OWNER FEEDBACK: the plan was reviewed against the real repo state before "
        f"implementation and found wanting. Its feedback:\n\n"
        f"{report}\n\n"
        f"Update the plan with new '- [ ]' items (continuing the existing numbering), or adjust "
        f"existing un-ticked items, to address every point raised above — nothing is ticked yet "
        f"at this stage, so there is no '- [x]' line to preserve a distinction against."
    )


def review_outcome(review_path: str) -> Optional[str]:
    """
    Post-review decision (1): a generated review.md means the reviewer found
    issues and wants another full iteration (planner → imp → testing →
    reviewer). No review.md means the review was good — return None to end
    the run normally.

    The returned feedback embeds the report's full content, so the next
    planner sees every issue even after the file is cleared away.
    """
    if not Path(review_path).exists():
        return None
    try:
        report = Path(review_path).read_text(encoding="utf-8")
    except OSError:
        report = "(the review report could not be read — re-inspect the plan and the code)"
    return (
        f"REVIEW FAILED: the reviewer found issues in the finished work. Its report:\n\n"
        f"{report}\n\n"
        f"Update the plan with new '- [ ]' items (continuing the existing numbering) to fix "
        f"every issue listed above — do NOT touch any already-ticked '- [x]' lines. Then "
        f"implement and test those fixes."
    )


def collect_next_iteration(console: AbstractManager, review_path: Optional[str] = None) -> tuple[Optional[str], Optional[str]]:
    """
    Decides what happens once the review phase has landed.

    A failed review (review.md present) and anything the user queued while
    the run was in flight are no longer mutually exclusive: both fold into
    the SAME next iteration's feedback when both are present, instead of a
    review failure starving queued follow-ups until some later iteration
    happens to pass cleanly. Nothing here asks for approval; with neither
    signal present, the pipeline idles with the input line live so feeding
    it more work stays optional. Returns a (feedback, review_path) tuple —
    feedback is None to end the run; review_path is set only when this
    iteration was (at least partly) triggered by a failed review, for the
    caller's loop-guard counter and to enrich the next planner trigger.
    """
    review_feedback = review_outcome(review_path) if review_path else None
    if review_feedback:
        console.display_rule("🔁 REVIEW FAILED — SCHEDULING ANOTHER FULL ITERATION")
        # Remove the stale report so it cannot re-trigger a loop on its own;
        # only a freshly written review.md may schedule another iteration.
        try:
            Path(review_path).unlink()
        except OSError:
            pass

    queued = console.drain_queued_input()
    if not queued and not review_feedback:
        console.set_status(phase="", state="idle · queue empty", task="")
        console.display_rule("✅ PIPELINE COMPLETE — IDLE (queue anything to continue)")
        queued = console.wait_for_queued_input()

    requests = [q for q in (queued or []) if q.strip().lower() not in EXIT_WORDS]
    if not review_feedback and not requests:
        return None, None  # stopped, nothing queued, and the review passed

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

    return "\n\n".join(parts), (review_path if review_feedback else None)


# ----------------------------------------------------------------- the phases

def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


_SESSION_MANAGER_CLASSES = {"simple": SimpleSessionManager, "adaptive": AdaptiveSessionManager}


def _session_manager_class(console: AbstractManager):
    """SESSION_MANAGER=simple|adaptive in .env selects which SessionManager
    drives a session. Defaults to "adaptive": task-type-specific planning
    rules (python/javascript/story -- see JFI.session.task_rules) instead
    of one generic rule block for every goal, which both cuts context cost
    for smaller models (a javascript task no longer pays for python/story
    guidance it'll never use, and vice versa) and adds architecture
    guidance the generic rules never gave (componentization for frontend
    work, dispatch tables over near-duplicate leaves for python, beat-based
    decomposition for prose) -- see adaptive_session_manager.py for the
    real run that motivated this. "simple" opts back into the original
    one-size-fits-all rules. An unrecognized value logs a hint and falls
    back to the default, same never-crash-on-a-typo convention as THEME."""
    raw = os.environ.get("SESSION_MANAGER", "").strip().lower()
    if not raw:
        return AdaptiveSessionManager
    cls = _SESSION_MANAGER_CLASSES.get(raw)
    if cls is None:
        console.display_system(
            f"⚠️  Unknown SESSION_MANAGER={raw!r} in .env (expected 'simple' or "
            f"'adaptive') — falling back to 'adaptive'."
        )
        return AdaptiveSessionManager
    return cls


def _web_bridge_enabled() -> bool:
    """JFI_WEB_BRIDGE=1: mirror this session's live status to
    JFI/<session>/web_status.json and accept answers to get_user_choice /
    get_user_input prompts, plus new queued requests, from
    JFI/<session>/web_answer.json -- see web_bridge.WebBridge. Also makes
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
    binary is self-sufficient) -- `sys.executable` in a PyInstaller onefile
    app is the running binary itself, so this needs no separate install.
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
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as e:
        console.display_system(f"ℹ️  Web dashboard failed to start ({e}); continuing with the terminal only.")
        return None

    console.display_system(
        f"🌐 Web dashboard starting — http://localhost:{port} "
        f"(set JFI_WEB_DASHBOARD=0 to stop this auto-start)"
    )
    return proc


def _stop_web_dashboard(proc: Optional[subprocess.Popen]) -> None:
    if proc is None or proc.poll() is not None:
        return
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


def _planner_single_pass() -> bool:
    """PLANNER_SINGLE_PASS=1: escape hatch back to the original one-pass
    planner (today's combined instructions, one PLANNER_COMPLETE marker)
    instead of the default 4-stage Architect -> Team Lead -> Journeyman ->
    Function Breakdown sequence (see run_phase's planner branch and
    get_system_message's planner_stage param). Off by default: tiering
    roughly quadruples the planner phase's own LLM-call count in exchange
    for catching compound leaves (and un-decomposed code leaves) a single
    pass was observed letting through -- worth it by default, but some runs
    may prefer the cheaper single pass."""
    return _env_flag("PLANNER_SINGLE_PASS")


def _task_stuck_time_limit() -> float:
    """TASK_STUCK_TIME_LIMIT_SECONDS override (default
    DEFAULT_TASK_STUCK_TIME_LIMIT_SECONDS) — see _check_task_stuck. Read
    fresh each call, same reasoning as _show_stream_prompts."""
    try:
        return float(os.environ.get("TASK_STUCK_TIME_LIMIT_SECONDS", DEFAULT_TASK_STUCK_TIME_LIMIT_SECONDS))
    except ValueError:
        return DEFAULT_TASK_STUCK_TIME_LIMIT_SECONDS


def _task_stuck_token_limit() -> int:
    """TASK_STUCK_TOKEN_LIMIT override (default DEFAULT_TASK_STUCK_TOKEN_LIMIT)
    — see _check_task_stuck."""
    try:
        return int(os.environ.get("TASK_STUCK_TOKEN_LIMIT", DEFAULT_TASK_STUCK_TOKEN_LIMIT))
    except ValueError:
        return DEFAULT_TASK_STUCK_TOKEN_LIMIT


def _show_stream_prompts() -> bool:
    """SHOW_STREAM_PROMPTS=1 (or true/yes/on) in .env: dump the exact
    messages sent to the LLM every turn — see dump_prompt. Read fresh each
    call rather than cached: it's checked once per turn at most, never in a
    hot loop, and a live .env edit (e.g. via Ctrl+N into a fresh process)
    should still take effect without a restart being required."""
    return os.environ.get("SHOW_STREAM_PROMPTS", "").strip().lower() in ("1", "true", "yes", "on")


def dump_prompt(console: AbstractManager, phase: str, messages: List[Dict[str, Any]]) -> None:
    """
    SHOW_STREAM_PROMPTS=1: prints every message about to be sent to the LLM
    this turn, in full — deliberately untruncated, unlike
    display_tool_result's 8-line/width-capped preview elsewhere in the
    console. Meant for prompt-engineering and context-compression
    debugging, where seeing exactly what the model is about to see (all of
    it) is the entire point.
    """
    console.display_rule(f"PROMPT SENT — {phase_display_name(phase).upper()} ({len(messages)} message(s))")
    for i, message in enumerate(messages, 1):
        header = f"[{i}] {message.get('role', '?')}"
        tool_calls = message.get("tool_calls") or []
        if tool_calls:
            names = ", ".join(tc.get("function", {}).get("name", "?") for tc in tool_calls)
            header += f"  (tool_calls: {names})"
        if message.get("tool_call_id"):
            header += f"  (tool_call_id: {message['tool_call_id']})"
        console.display_system(header)

        content = message.get("content")
        if isinstance(content, list):
            # Multimodal content (view_image's follow-up user turn): show the
            # text parts in full, note images without dumping raw base64.
            for part in content:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "text":
                    console.display_system(str(part.get("text") or ""))
                elif part.get("type") == "image_url":
                    console.display_system("(image attached)")
        elif content:
            console.display_system(str(content))
    console.display_rule("END PROMPT")


def _log_llm_call_debug() -> bool:
    """LOG_LLM_CALL_DEBUG=1 (or true/yes/on) in .env: append every LLM
    request/response pair to JFI/<session>/llm_debug.jsonl -- one JSON
    object per line, full request (messages + tools) and full response
    (content + tool_calls), no truncation. Unlike SHOW_STREAM_PROMPTS
    (console/TUI-only, meant for watching a run live), this persists to
    disk so a run can be inspected afterward without having had the flag's
    console output scrolling past at the time. Read fresh each call, same
    reasoning as _show_stream_prompts."""
    return _env_flag("LOG_LLM_CALL_DEBUG")


def log_llm_call(ssm: SessionManager, phase: str, messages: List[Dict[str, Any]],
                  tools: Optional[List[Dict[str, Any]]], parsed_response: Dict[str, Any]) -> None:
    """
    Appends one JSON-line record of this turn's exact request and response
    to JFI/<session>/llm_debug.jsonl. Gated by LOG_LLM_CALL_DEBUG -- see
    _log_llm_call_debug. The log is a debugging aid, not part of the
    pipeline's contract, so any I/O error here is swallowed rather than
    interrupting the run (same tradeoff pt_console_manager's own _log
    makes for run.log).
    """
    record = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "phase": phase,
        "request": {"messages": messages, "tools": tools},
        "response": parsed_response,
    }
    try:
        with open(ssm.session_path / "llm_debug.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except OSError:
        pass


def _stuck_task_directive(task_title: str, elapsed_seconds: float, tokens_spent: int) -> str:
    """Forced message for when the same plan leaf has stayed "current" too
    long (see _task_stuck_time_limit/_task_stuck_token_limit in run_phase) —
    tells the model to stop and split THIS leaf into numbered sub-leaves
    (a leaf numbered 3.1 becomes parent 3.1 with children 3.1.1, 3.1.2,
    3.1.3, ...) instead of continuing to grind on one oversized item.
    Reported facts (elapsed minutes / tokens spent) are included so the
    model doesn't have to guess why it's being interrupted."""
    return (
        f"AUTO-RECTIFY: the current task has stayed the same for {elapsed_seconds / 60:.1f} "
        f"minutes and ~{tokens_spent:,} tokens of re-sent context without being ticked off:\n"
        f"  {task_title}\n"
        f"That is a strong sign this leaf was never actually small enough — it hid more work "
        f"than one focused step (a diagnose-then-fix, several distinct checks bundled together, "
        f"a fix plus its own verification, ...). Stop whatever you were about to do next and "
        f"instead, right now: edit the plan file to turn this ONE leaf into a parent with 2 or "
        f"more numbered sub-leaves — e.g. a leaf numbered 3.1 becomes parent '- 3.1 <original "
        f"description>' with children '- [ ] 3.1.1 ...', '- [ ] 3.1.2 ...', '- [ ] 3.1.3 ...' and "
        f"so on (same nesting rule as everywhere else in this plan), each one small enough to "
        f"finish and verify in a single focused step. The split itself is the required action "
        f"this turn, before any further tool calls toward actually finishing the work."
    )


#: The tiered planner's 4 internal stages, in order -- (stage name passed to
#: ssm.set_planner_stage/get_system_message's planner_stage, this stage's
#: own completion marker, the display label for its transition rule, a short
#: tag for the header/dashboard's "stage" status field -- see
#: AbstractManager.set_status's `stage` param). The LAST stage deliberately
#: reuses today's original PLANNER_COMPLETE marker (see get_system_message's
#: planner_stage docstring) so nothing downstream of "is planner done"
#: (phase_completed/get_remaining_phases/resumability) needs to know
#: intermediate stages exist at all. "function_breakdown" (after
#: journeyman) takes every already-atomic leaf that writes code and breaks
#: IT down further into one child leaf per function/method it implements --
#: a non-code leaf (verification, research, docs) is left untouched.
PLANNER_STAGES = [
    ("architect", "ARCHITECT_STAGE_COMPLETE", "ARCHITECT", "Arc"),
    ("team_lead", "TEAM_LEAD_STAGE_COMPLETE", "TEAM LEAD", "Lead"),
    ("journeyman", "JOURNEYMAN_STAGE_COMPLETE", "JOURNEYMAN", "Journy"),
    ("function_breakdown", "PLANNER_COMPLETE", "FUNCTION BREAKDOWN", "Func"),
]

#: Stage tag shown when PLANNER_SINGLE_PASS=1 opts out of tiering (see
#: _planner_single_pass) -- the counterpart to PLANNER_STAGES' own tags for
#: the one case where "planner" runs as a single, undifferentiated pass.
PLANNER_SINGLE_PASS_STAGE_TAG = "Task"


def _drive_turn_loop(console: AbstractManager, llm: BaseLLMStream, ssm: SessionManager,
                     phase: str, completion_keyword: str, failures: Dict[tuple, int]) -> bool:
    """
    Runs turns for one phase (or, for the tiered planner, one STAGE within
    "planner" -- see PLANNER_STAGES/run_phase) until `completion_keyword`
    appears as a stand-alone marker in the assistant's own content, or the
    run should stop. Returns True on completion, False if the run should
    stop early (user interrupt or unrecoverable LLM failure) -- same
    contract this loop had inline in run_phase before it was extracted to
    let a tiered planner call it 3x with 3 different markers instead of
    once with the phase's fixed f"{phase.upper()}_COMPLETE".

    Callers own everything that happens BEFORE the first turn (status/rule
    display) and AFTER a True return (marking the phase/stage done) --
    this only drives the turn-by-turn loop itself.
    """
    # Tracks how long/how many re-sent tokens the SAME leaf (current_task)
    # has stayed current, so a leaf that turns out to hide far more work
    # than one focused step gets forced to split itself up instead of
    # grinding indefinitely — see _stuck_task_directive. Local to this one
    # loop run (like `failures` above): a manual restart, or a new tiered-
    # planner stage, earns a fresh window, same tradeoff already made for
    # the failure-retry counter.
    stuck_task_title = ""
    stuck_since = time.monotonic()
    stuck_tokens = 0
    # Wall-clock (time.time(), not the monotonic stuck_since above) start of
    # the CURRENT leaf -- rides set_status's task_started_at every turn so a
    # live viewer can show "running Xm" for the in-progress leaf, and is
    # handed to record_task_tokens as the just-finished leaf's start once
    # the NEXT leaf becomes current (see AbstractManager.record_task_tokens'
    # started_at/ended_at params).
    task_started_at = time.time()

    while not console.should_stop():
        # Ctrl+P: hold here, between turns, so an in-flight tool call or LLM
        # response is never interrupted mid-way.
        console.wait_while_paused()
        if console.should_stop():
            break

        drain_forced_input(console, ssm)
        handle_skip_request(console, ssm, phase)
        handle_skip_all_request(console, ssm, phase)
        current_task = ssm.current_task_title(phase) or ""
        console.set_status(plan=ssm.plan_progress(), phase_plan=ssm.phase_progress(phase),
                           task=current_task, plan_markdown=_read_plan_markdown(ssm),
                           task_started_at=task_started_at)

        if current_task != stuck_task_title:
            # Progress since last turn (ticked a box, or a fresh phase) —
            # this is a new leaf's own window now. Record what the leaf
            # that just finished cost before resetting the counter (free
            # data: stuck_tokens was already being accumulated for the
            # stuck-task-split trigger below) — see
            # AbstractManager.record_task_tokens.
            now = time.time()
            console.record_task_tokens(stuck_task_title, stuck_tokens,
                                       started_at=task_started_at, ended_at=now, phase=phase)
            stuck_task_title = current_task
            stuck_since = time.monotonic()
            task_started_at = now
            stuck_tokens = 0
        elif current_task:
            elapsed = time.monotonic() - stuck_since
            if elapsed > _task_stuck_time_limit() or stuck_tokens > _task_stuck_token_limit():
                ssm.add_message(
                    "user", _stuck_task_directive(current_task, elapsed, stuck_tokens)
                )
                # Give it a fresh window to actually act on the split rather
                # than firing again next turn while it's busy doing so; if
                # it's ignored, the same leaf staying current re-trips this
                # after another full window.
                stuck_since = time.monotonic()
                stuck_tokens = 0

        messages = ssm.get_messages(phase)
        # get_messages() is what actually runs compress_history() and updates
        # ssm's last-sent-tokens figure — push it to the header now, not just
        # when this turn happens to produce tool calls (see the tokens=
        # set_status calls below): a run of plain-content turns (a model
        # thinking out loud, an auto-nudge reply, ...) would otherwise leave
        # ctx showing a stale figure from several turns back.
        console.set_status(tokens=ssm.token_usage(phase))
        stuck_tokens += ssm.token_usage(phase)[0]
        if _show_stream_prompts():
            dump_prompt(console, phase, messages)
        turn_tools = _tools_for_session(ssm)
        attempt = 0
        while True:
            try:
                response = llm.send_message(messages, tools=turn_tools)
                parsed_response = console.print_agent_response(
                    response, prompt_tokens_estimate=ssm.estimate_request_tokens(messages)
                )
                if _log_llm_call_debug():
                    log_llm_call(ssm, phase, messages, turn_tools, parsed_response)
                break
            except Exception as e:
                attempt += 1

                if isinstance(e, ResponseTooLongError):
                    # Blindly resending the identical `messages` after this
                    # specific failure just invites the same overly-long
                    # reasoning again (observed in practice: several
                    # STREAM_OUTPUT_CAP hits in a row on the same turn, with
                    # no forward progress). This only touches the local copy
                    # for this retry -- ssm's real history is untouched, so a
                    # later, unrelated turn starts clean again.
                    messages.append({
                        "role": "user",
                        "content": (
                            "AUTO-RECTIFY: your last response was discarded for exceeding the "
                            "reasoning budget (STREAM_OUTPUT_CAP) before producing a real answer "
                            "or tool call. Re-sending the identical request risks the identical "
                            "outcome. This time: skip the lengthy internal debate and commit to "
                            "ONE concrete action immediately — call a tool, or state the required "
                            "phase-complete phrase — with minimal deliberation."
                        ),
                    })

                if _is_retryable_llm_error(e) and attempt <= LLM_RETRY_LIMIT and not console.should_stop():
                    console.display_error(
                        f"LLM request failed (attempt {attempt}/{LLM_RETRY_LIMIT + 1}): "
                        f"{_format_llm_error(e)} — retrying in {LLM_RETRY_DELAY_SECONDS:.0f}s..."
                    )
                    _interruptible_sleep(console, LLM_RETRY_DELAY_SECONDS)
                    continue

                # Automatic retries are exhausted (or this wasn't a retryable
                # error at all, e.g. a bad request) -- don't give up and end
                # the run on the model's/network's say-so. The run only
                # actually stops now if the user asks it to, either from
                # this menu or with Ctrl+C; anything else just retries for
                # as long as it takes.
                console.display_error(f"LLM request failed: {_format_llm_error(e)}")
                if console.should_stop():
                    return False
                choice = console.get_user_choice(
                    "LLM request failed. Retry, or stop the run?",
                    [("r", "Retry now"), ("s", "Stop (progress is saved)")],
                )
                if choice == "s" or console.should_stop():
                    console.request_stop()
                    return False
                attempt = 0  # a deliberate manual retry earns a fresh automatic-retry budget
                continue

        content = parsed_response.get("content")
        tool_calls = parsed_response.get("tool_calls")

        # An assistant message with neither content nor tool calls is rejected
        # by most servers when it is replayed, so it must not enter the history.
        if content or tool_calls:
            assistant_message: Dict[str, Any] = {"role": "assistant"}
            if content:
                assistant_message["content"] = content
            if tool_calls:
                assistant_message["tool_calls"] = tool_calls
            ssm.append_raw(assistant_message)

        # --- HANDLE TOOL CALLS ---
        if tool_calls:
            console.set_status(state="running tools")

            for tool_call in tool_calls:
                tool_result, image_data_url = execute_tool_call(console, ssm, tool_call, failures)
                console.display_tool_result(tool_result)

                ssm.append_raw({
                    "role": "tool",
                    "tool_call_id": tool_call["id"],
                    "name": tool_call["function"]["name"],
                    "content": str(tool_result)
                })

                # view_image succeeded: the tool result above is plain text
                # (a tool message can't carry image content), so the actual
                # picture goes in as a follow-up user turn instead — that's
                # what puts it in front of the model on its next inference.
                if image_data_url:
                    ssm.append_raw({
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "(image attached — see the view_image result above for its path)"},
                            {"type": "image_url", "image_url": {"url": image_data_url}},
                        ],
                    })

            console.set_status(state="thinking", plan=ssm.plan_progress(),
                               phase_plan=ssm.phase_progress(phase), tokens=ssm.token_usage(phase),
                               task=ssm.current_task_title(phase) or "")

        # --- CHECK FOR COMPLETION ---
        # Checked even alongside tool calls: ticking the last box and signing
        # off usually arrive in the same message.
        if _marker_present(content, completion_keyword):
            return True

        if tool_calls:
            continue

        # --- AUTO-NUDGE (STALL PREVENTION) ---
        if parsed_response.get("had_reasoning") and not content:
            # The model deliberated but never committed to an action -- and
            # that deliberation left no trace in history (nothing gets
            # appended when both content and tool_calls are empty), so next
            # turn it has zero memory of having just reasoned this through.
            # A generic "please continue" invites re-deriving the identical
            # reasoning again; naming the actual failure (thought, didn't
            # act) is what breaks that loop.
            ssm.add_message(
                "user",
                "AUTO-RECTIFY: your last turn was entirely reasoning — it never produced a real "
                "reply or a tool call, so nothing was recorded and that reasoning is now lost. "
                "Stop re-deliberating the same decision. This turn, take exactly ONE concrete "
                "action (call a tool, or state the required phrase) — do not just think about it "
                "again."
            )
        else:
            ssm.add_message(
                "user",
                f"Please continue your work. Remember, when you are entirely finished with this "
                f"step, you MUST output the exact phrase: '{completion_keyword}'."
            )

    return False


def run_phase(console: AbstractManager, llms: Dict[str, BaseLLMStream], ssm: SessionManager,
              phase: str) -> bool:
    """
    Drives one phase to completion. Returns False if the run should stop early
    (user interrupt or LLM failure), True when the phase finished cleanly.

    `llms` maps each phase to its own stream (see PHASE_ENV_PREFIX) — every
    key resolves to the same shared model/endpoint unless .env sets a
    per-phase override, so this indexing is a no-op in the common case.

    "planner" runs as 3 internal stages by default -- Architect (top-level
    shape) -> Team Lead (feature breakdown) -> Journeyman (genuinely atomic
    leaves) -- instead of one combined pass, because a single pass was
    observed letting compound leaves through (see PLANNER_STAGES and
    get_system_message's planner_stage docstring). PLANNER_SINGLE_PASS=1
    (see _planner_single_pass) opts back into the original one-pass
    behavior. Every other phase is unaffected either way.
    """
    llm = llms[phase]
    # ask_llm delegates to whatever model this phase itself is using — a
    # phase with its own .env override (PLANNER_MODEL, etc.) gets an ask_llm
    # backed by that same model, not always the shared default.
    TOOL_MAP["ask_llm"] = make_ask_llm(llm, console)
    console.set_status(phase=phase, state="thinking", plan=ssm.plan_progress(),
                       phase_plan=ssm.phase_progress(phase), tokens=ssm.token_usage(phase),
                       task=ssm.current_task_title(phase) or "", stage="")
    console.display_rule(f"PHASE: {phase_display_name(phase).upper()}")

    failures: Dict[tuple, int] = {}
    tiered_planner = (
        phase == "planner" and hasattr(ssm, "set_planner_stage") and not _planner_single_pass()
    )

    if tiered_planner:
        for i, (stage, keyword, label, tag) in enumerate(PLANNER_STAGES):
            ssm.set_planner_stage(stage)
            console.set_status(stage=tag)
            if i > 0:
                # The very first stage's own instructions already frame it
                # as "the first of three passes" -- only later transitions
                # need their own announcement, live and in run.log alike
                # (see PromptToolkitConsoleManager._log's RULE tag).
                console.display_rule(f"PLANNER STAGE: {label}")
            if not _drive_turn_loop(console, llm, ssm, phase, keyword, failures):
                return False
    else:
        if phase == "planner" and hasattr(ssm, "set_planner_stage"):
            ssm.set_planner_stage(None)  # PLANNER_SINGLE_PASS=1: today's original combined prompt
            console.set_status(stage=PLANNER_SINGLE_PASS_STAGE_TAG)
        completion_keyword = f"{phase.upper()}_COMPLETE"
        if not _drive_turn_loop(console, llm, ssm, phase, completion_keyword, failures):
            return False

    console.display_system(f"✅ Phase '{phase}' completed successfully.")
    console.mark_phase_done(phase)
    if phase == "planner":
        ssm.ensure_plan_file()
    console.set_status(plan=ssm.plan_progress(), phase_plan=ssm.phase_progress(phase),
                       tokens=ssm.token_usage(phase), task=ssm.current_task_title(phase) or "",
                       stage="")
    return True


def _run_session(console: AbstractManager, llms: Dict[str, BaseLLMStream]) -> None:
    """
    Runs exactly one session end-to-end: gather its name/goal, then drive
    planner -> imp -> testing -> reviewer -> cleanup, looping on failed
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
            ssm = _session_manager_class(console)(console, session_name)
        except SessionInUseError as e:
            console.display_error(str(e))
    console.set_status(session=session_name)
    # Optional hook (SimpleSessionManager only, not part of the required
    # SessionManager interface): lets compress_history's digest tier
    # LLM-summarize aged-out history using each phase's own configured
    # model instead of only recording tool/file/command names. A session
    # manager that doesn't define it just keeps the cheap metadata digest.
    if hasattr(ssm, "set_llm_streams"):
        ssm.set_llm_streams(llms)

    web_bridge: Optional[WebBridge] = None
    socket_reporter: Optional[SocketReporter] = None
    try:
        # 2. Gate execute_command behind the human's approval, backed by this
        # session's own context.json (approved "Save" prefixes live there,
        # alongside whatever facts the LLM itself has stashed there).
        TOOL_MAP["execute_command"] = make_gated_execute_command(console, ssm.context_cache_path)
        TOOL_MAP.update(make_context_tools(ssm.context_cache_path))
        TOOL_MAP.update(make_process_tools(ssm.context_cache_path))
        TOOL_MAP["load_tool"] = make_load_tool(ssm)
        console.start_session_log(ssm.session_path / "run.log")
        if _web_bridge_enabled():
            web_bridge = WebBridge(console, ssm.session_path)
            web_bridge.start()
        master_url = _master_ws_url()
        if master_url:
            socket_reporter = SocketReporter(console, master_url, session_id=ssm.session_id)
            socket_reporter.start()
        # Requests queued but never drained before the process closed (killed,
        # crashed, or just quit) live in metadata.json — hand them back now, and
        # persist the queue from here on so this doesn't happen again.
        console.set_queue_store(ssm.load_queued_requests(), ssm.save_queued_requests)

        if ssm.is_resuming:
            console.display_system(f"📁 Resuming existing session '{session_name}'...")
            initial_goal = "(Resuming previous session goal from history)"
        else:
            initial_goal = console.safe_get_user_input("What is your goal? (Be as detailed as possible):", multiline=True)
            if not initial_goal or console.should_stop():
                return

        # 3. Resumed sessions pick up at the first phase that never completed
        iteration = 1
        console.start_iteration(iteration, PHASES)
        console.set_status(plan=ssm.plan_progress(), tokens=ssm.token_usage(),
                           plan_markdown=_read_plan_markdown(ssm))
        active_phases = ssm.get_remaining_phases(PHASES)
        skipped = [p for p in PHASES if p not in active_phases]
        for phase in skipped:
            console.mark_phase_done(phase)
        if ssm.is_resuming and skipped:
            console.display_system(f"⏩ Skipping completed phases: {', '.join(skipped).upper()}")

        # 4. Outer Loop for Re-runs and Feedback
        review_feedback: Optional[str] = None  # set when a failed review re-iteration starts
        review_failures = 0                    # counts consecutive failed reviews (loop guard)
        while not console.should_stop():
            if not active_phases:
                console.display_system("All phases have already been completed for this session.")

            for phase in active_phases:
                if phase == "product_owner":
                    # A separate, tighter loop than the rest of this
                    # for-loop drives -- see _run_product_owner_loop's own
                    # docstring for why it owns its own trigger/run_phase
                    # calls instead of the generic ones below.
                    if not _run_product_owner_loop(console, llms, ssm, initial_goal):
                        return
                    continue

                trigger_message = get_phase_trigger(phase, initial_goal, ssm.plan_path, iteration,
                                                    review_path=review_feedback)

                # Avoid inserting duplicate trigger if already present in history
                last_user_msg = next((m.get("content", "") for m in reversed(ssm.history) if m.get("role") == "user"), "")
                if trigger_message not in last_user_msg:
                    ssm.add_message("user", trigger_message)

                if not run_phase(console, llms, ssm, phase):
                    return

                if phase == "reviewer":
                    _clear_reviewer_notes(ssm)

            # 5. Review has landed: a generated review.md (failed review) or queued
            # requests loop us round again, no prompt.
            feedback, review_feedback = collect_next_iteration(console, review_path=str(Path(ssm.plan_path).with_name("review.md")))
            if feedback is None:
                break

            if review_feedback is not None:
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
                f"USER FEEDBACK FOR ITERATION:\n{feedback}\n\n"
                f"{ssm.get_project_state_summary()}\n\n"
                f"The plan file is {ssm.plan_path}. Keep its completed '- [x]' items, add new "
                f"'- [ ]' items for this request, then implement and test them."
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

    if args.internal_web_dashboard:
        from JFI.web.launcher import main as run_web_dashboard
        run_web_dashboard(extra_args=[])
        return

    # Load the project's .env from an explicit path (searched upward from the
    # current working directory) BEFORE any console/theme code runs, so a user
    # setting THEME=... in their .env is honored no matter how JFI was launched.
    load_dotenv(find_dotenv())

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
