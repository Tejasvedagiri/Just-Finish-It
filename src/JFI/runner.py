import inspect
import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotenv import find_dotenv, load_dotenv
from openai import APIConnectionError, APIStatusError

# LLM and Console Management
from JFI.llm.openai_compatable_stream import OpenAICompatableStream
from JFI.manager.abstract_manager import AbstractManager, phase_display_name
from JFI.manager.pt_console_manager import PromptToolkitConsoleManager

# Session Management
from JFI.session.simple_session_manager import (
    DEFAULT_CONTEXT_CACHE_PATH, SimpleSessionManager, get_phase_trigger, phase_completed,
)

# Tools and Schemas
from JFI.tool.schemas import AVAILABLE_TOOLS
from JFI.tool.file_tools import write_file, read_file, append_to_file, replace_in_file
from JFI.tool.cmd_tools import execute_command, make_gated_execute_command
from JFI.tool.context_tools import make_context_tools
from JFI.tool.image_tools import capture_screenshot, view_image
from JFI.tool.web_tools import fetch_webpage_images

# Dynamic mapping of tool names to their python functions
TOOL_MAP = {
    "write_file": write_file,
    "read_file": read_file,
    "append_to_file": append_to_file,
    "replace_in_file": replace_in_file,
    "execute_command": execute_command,
    "capture_screenshot": capture_screenshot,
    "fetch_webpage_images": fetch_webpage_images,
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
}

PHASES = ["planner", "imp", "testing", "reviewer"]

# .env prefix each phase's model/endpoint override is read from (see
# JFI.llm.base_llm_stream.phase_env) — e.g. PLANNER_MODEL, PLANNER_OPENAI_URL,
# PLANNER_OPENAI_API_KEY, PLANNER_TEMPERATURE. Any that are unset fall back to
# the shared MODEL/OPENAI_URL/OPENAI_API_KEY/TEMPERATURE, so a single model
# for every phase (today's default) needs no per-phase vars at all.
PHASE_ENV_PREFIX = {"planner": "PLANNER", "imp": "IMP", "testing": "TESTING", "reviewer": "REVIEWER"}

EXIT_WORDS = {"exit", "done", "quit", "no", "nothing"}

# How many times the same failing call is coached before we tell the model to
# abandon that approach entirely.
FAILURE_RETRY_LIMIT = 3

# How many times a transient LLM-server failure (connection drop, 5xx) is
# retried before giving up on this turn — see _is_retryable_llm_error.
LLM_RETRY_LIMIT = 2
LLM_RETRY_DELAY_SECONDS = 3.0


# ------------------------------------------------------------------ LLM errors

def _is_retryable_llm_error(e: Exception) -> bool:
    """
    Worth retrying: a network-level failure (server down/restarting, a
    dropped connection) or a 5xx-class server error — both are typically
    transient on a local LLM server. NOT a 4xx client error: retrying an
    identical request the server already rejected (bad request, auth,
    context-length) won't produce a different result.
    """
    if isinstance(e, APIConnectionError):
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

    if func_name in ("read_file", "write_file", "append_to_file", "view_image") and "does not exist" in lowered:
        return (
            "AUTO-RECTIFY: that path is wrong. Run execute_command with "
            "'ls -la .' (or the parent directory) to find the real path, then retry."
        )

    if func_name == "execute_command":
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


def execute_tool_call(console: AbstractManager, ssm: SimpleSessionManager,
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

    return result, image_data_url


# ------------------------------------------------------------------ the queue

def drain_forced_input(console: AbstractManager, ssm: SimpleSessionManager) -> None:
    """
    Folds forced input ('!something') into the AI's very next turn, mid-phase.
    Plain queued input is left alone for :func:`collect_next_iteration`.
    """
    for note in console.drain_forced_input():
        ssm.add_message(
            "user",
            f"USER INTERJECTION (apply this from here on):\n{note}"
        )


def handle_skip_request(console: AbstractManager, ssm: SimpleSessionManager, phase: str) -> None:
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


def handle_skip_all_request(console: AbstractManager, ssm: SimpleSessionManager, phase: str) -> None:
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


MAX_REVIEW_ITERATIONS = 3


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

def run_phase(console: AbstractManager, llms: Dict[str, OpenAICompatableStream], ssm: SimpleSessionManager,
              phase: str) -> bool:
    """
    Drives one phase to completion. Returns False if the run should stop early
    (user interrupt or LLM failure), True when the phase finished cleanly.

    `llms` maps each phase to its own stream (see PHASE_ENV_PREFIX) — every
    key resolves to the same shared model/endpoint unless .env sets a
    per-phase override, so this indexing is a no-op in the common case.
    """
    llm = llms[phase]
    console.set_status(phase=phase, state="thinking", plan=ssm.plan_progress(),
                       phase_plan=ssm.phase_progress(phase), tokens=ssm.token_usage(),
                       task=ssm.current_task_title(phase) or "")
    console.display_rule(f"PHASE: {phase_display_name(phase).upper()}")

    failures: Dict[tuple, int] = {}

    while not console.should_stop():
        # Ctrl+P: hold here, between turns, so an in-flight tool call or LLM
        # response is never interrupted mid-way.
        console.wait_while_paused()
        if console.should_stop():
            break

        drain_forced_input(console, ssm)
        handle_skip_request(console, ssm, phase)
        handle_skip_all_request(console, ssm, phase)
        console.set_status(plan=ssm.plan_progress(), phase_plan=ssm.phase_progress(phase),
                           task=ssm.current_task_title(phase) or "")

        messages = ssm.get_messages(phase)
        attempt = 0
        while True:
            try:
                response = llm.send_message(messages, tools=AVAILABLE_TOOLS)
                parsed_response = console.print_agent_response(
                    response, prompt_tokens_estimate=ssm.estimate_request_tokens(messages)
                )
                break
            except Exception as e:
                attempt += 1
                if _is_retryable_llm_error(e) and attempt <= LLM_RETRY_LIMIT and not console.should_stop():
                    console.display_error(
                        f"LLM request failed (attempt {attempt}/{LLM_RETRY_LIMIT + 1}): "
                        f"{_format_llm_error(e)} — retrying in {LLM_RETRY_DELAY_SECONDS:.0f}s..."
                    )
                    _interruptible_sleep(console, LLM_RETRY_DELAY_SECONDS)
                    continue
                console.display_error(f"LLM request failed: {_format_llm_error(e)}")
                console.display_system("Progress is saved — rerun with the same session name to resume.")
                return False

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
                               phase_plan=ssm.phase_progress(phase), tokens=ssm.token_usage(),
                               task=ssm.current_task_title(phase) or "")

        # --- CHECK FOR COMPLETION ---
        # Checked even alongside tool calls: ticking the last box and signing
        # off usually arrive in the same message.
        if phase_completed(content, phase):
            console.display_system(f"✅ Phase '{phase}' completed successfully.")
            console.mark_phase_done(phase)

            if phase == "planner":
                ssm.ensure_plan_file()
            console.set_status(plan=ssm.plan_progress(), phase_plan=ssm.phase_progress(phase),
                               tokens=ssm.token_usage(), task=ssm.current_task_title(phase) or "")
            return True

        if tool_calls:
            continue

        # --- AUTO-NUDGE (STALL PREVENTION) ---
        ssm.add_message(
            "user",
            f"Please continue your work. Remember, when you are entirely finished with this "
            f"phase, you MUST output the exact phrase: '{phase.upper()}_COMPLETE'."
        )

    return False


def _run_session(console: AbstractManager, llms: Dict[str, OpenAICompatableStream]) -> None:
    """
    Runs exactly one session end-to-end: gather its name/goal, then drive
    planner -> imp -> testing -> reviewer, looping on failed reviews or
    queued follow-ups, until the review passes and the idle queue stays
    empty. Returns either because the run should stop for good (Ctrl+C, an
    unrecoverable LLM failure, the review-loop cap) or because the user
    asked to start a new session (Ctrl+N while idle) — run_pipeline tells
    the two apart via console.should_restart() and loops accordingly.
    """
    # 1. Gather Session Name
    session_name = console.safe_get_user_input("Please enter a session name to begin:", multiline=False)
    if not session_name or console.should_stop():
        return
    console.set_status(session=session_name)

    # 2. Initialize Session Manager once
    ssm = SimpleSessionManager(console, session_name)
    # Gate execute_command behind the human's approval, backed by this
    # session's own context.json (approved "Save" prefixes live there,
    # alongside whatever facts the LLM itself has stashed there).
    TOOL_MAP["execute_command"] = make_gated_execute_command(console, ssm.context_cache_path)
    TOOL_MAP.update(make_context_tools(ssm.context_cache_path))
    console.start_session_log(ssm.session_path / "run.log")
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
    console.set_status(plan=ssm.plan_progress(), tokens=ssm.token_usage())
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
            trigger_message = get_phase_trigger(phase, initial_goal, ssm.plan_path, iteration,
                                                review_path=review_feedback)

            # Avoid inserting duplicate trigger if already present in history
            last_user_msg = next((m.get("content", "") for m in reversed(ssm.history) if m.get("role") == "user"), "")
            if trigger_message not in last_user_msg:
                ssm.add_message("user", trigger_message)

            if not run_phase(console, llms, ssm, phase):
                return

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


def run_pipeline(console: AbstractManager, llms: Dict[str, OpenAICompatableStream]) -> None:
    console.set_status(phases=PHASES, state="waiting")
    console.display_rule("Just Finish It — Generic Autonomous Mode 🤖")
    console.display_system(
        "Type at any time. Enter queues a request for after the review phase; "
        "prefix with ! to run it in the current turn. Once a review completes and "
        "the queue is idle, Ctrl+N starts a brand-new session from scratch."
    )

    while True:
        _run_session(console, llms)
        # should_restart() is only ever set by Ctrl+N, which is only live
        # while _run_session was idling on wait_for_queued_input — so it
        # can't be true after a Ctrl+C stop or an unrecoverable failure.
        if console.should_stop() or not console.should_restart():
            break
        console.clear_restart()
        console.display_rule("🔄 STARTING A NEW SESSION (Ctrl+N)")

    console.set_status(phase="", state="finished", task="")
    console.display_rule("🎉 JUST FINISH IT — SESSION TERMINATED 🎉")


def main():
    # Load the project's .env from an explicit path (searched upward from the
    # current working directory) BEFORE any console/theme code runs, so a user
    # setting THEME=... in their .env is honored no matter how JFI was launched.
    load_dotenv(find_dotenv())

    console = PromptToolkitConsoleManager()
    # One stream per phase (see PHASE_ENV_PREFIX) — each falls back to the
    # shared MODEL/OPENAI_URL/OPENAI_API_KEY/TEMPERATURE when that phase has
    # no .env override, so this is one shared connection in the common case
    # and up to four independent ones when a user has configured per-phase
    # models/endpoints.
    llms = {phase: OpenAICompatableStream(prefix) for phase, prefix in PHASE_ENV_PREFIX.items()}

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
