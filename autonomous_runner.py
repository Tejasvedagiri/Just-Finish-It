import inspect
import json
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv

# LLM and Console Management
from llm.colibri_llm_stream import ColibriLLMStream
from manager.abstract_manager import AbstractManager
from manager.pt_console_manager import PromptToolkitConsoleManager

# Session Management
from session.simple_session_manager import SimpleSessionManager, get_phase_trigger, phase_completed

# Tools and Schemas
from tool.schemas import AVAILABLE_TOOLS
from tool.file_tools import write_file, read_file, append_to_file, replace_in_file
from tool.cmd_tools import execute_command

# Dynamic mapping of tool names to their python functions
TOOL_MAP = {
    "write_file": write_file,
    "read_file": read_file,
    "append_to_file": append_to_file,
    "replace_in_file": replace_in_file,
    "execute_command": execute_command
}

PHASES = ["planner", "imp", "testing", "reviewer"]

EXIT_WORDS = {"exit", "done", "quit", "no", "nothing"}

# How many times the same failing call is coached before we tell the model to
# abandon that approach entirely.
FAILURE_RETRY_LIMIT = 3


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

    if func_name in ("read_file", "write_file", "append_to_file") and "does not exist" in lowered:
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

    return (
        f"AUTO-RECTIFY: the {func_name} call failed and nothing was changed. Diagnose the "
        "message above and issue a corrected call."
    )


def execute_tool_call(console: AbstractManager, ssm: SimpleSessionManager,
                      tool_call: Dict[str, Any], failures: Dict[tuple, int]) -> str:
    """
    Runs one tool call and turns any failure into actionable guidance.

    Every error path returns a string rather than raising, so a bad call costs a
    turn instead of killing the run.
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
        )

    if not isinstance(args, dict):
        console.display_tool_call(func_name)
        return (
            f"Error: the arguments for {func_name} must be a JSON object, got "
            f"{type(args).__name__}. AUTO-RECTIFY: retry as {_expected_arguments(func_name)}."
        )

    # --- a tool that doesn't exist ---
    if func_name not in TOOL_MAP:
        console.display_tool_call(func_name, args)
        return (
            f"Error: there is no tool called '{func_name}'. AUTO-RECTIFY: use one of "
            f"{', '.join(sorted(TOOL_MAP))} instead."
        )

    console.display_tool_call(func_name, args)

    # --- wrong or missing arguments ---
    try:
        result = TOOL_MAP[func_name](**args)
    except TypeError as e:
        result = (
            f"Error: wrong arguments for {func_name} ({e}). "
            f"AUTO-RECTIFY: the signature is {_expected_arguments(func_name)}."
        )
    except Exception as e:
        result = f"Error executing tool {func_name}: {e}"

    # --- coach, then escalate, on repeated identical failures ---
    signature = (func_name, args_str)
    if _is_failure(result):
        failures[signature] = failures.get(signature, 0) + 1
        attempt = failures[signature]
        console.display_error(f"{func_name} failed (attempt {attempt})")
        result = f"{result}\n\n{_repair_directive(func_name, args, result, attempt)}"
    else:
        failures.pop(signature, None)
        if func_name in ("write_file", "append_to_file", "replace_in_file"):
            ssm.track_file(args.get("file_path"))

    return result


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


def collect_next_iteration(console: AbstractManager) -> Optional[str]:
    """
    Decides what happens once the review phase has landed.

    Nothing here asks for approval. Queued requests start the next pass
    immediately; with an empty queue the pipeline idles with the input line
    live, so feeding it more work stays optional. Returns None to end the run.
    """
    queued = console.drain_queued_input()

    if not queued:
        console.set_status(phase="", state="idle · queue empty")
        console.display_rule("✅ PIPELINE COMPLETE — IDLE (queue anything to continue)")
        queued = console.wait_for_queued_input()

    if not queued:
        return None  # stopped, or the console has no queue at all

    requests = [q for q in queued if q.strip().lower() not in EXIT_WORDS]
    if not requests:
        return None

    console.display_rule(f"▶  RUNNING {len(requests)} QUEUED REQUEST(S)")
    for request in requests:
        console.display_user(request)
    return "\n".join(f"- {request}" for request in requests)


# ----------------------------------------------------------------- the phases

def run_phase(console: AbstractManager, llm: ColibriLLMStream, ssm: SimpleSessionManager,
              phase: str) -> bool:
    """
    Drives one phase to completion. Returns False if the run should stop early
    (user interrupt or LLM failure), True when the phase finished cleanly.
    """
    console.set_status(phase=phase, state="thinking", plan=ssm.plan_progress())
    console.display_rule(f"PHASE: {phase.upper()}")

    failures: Dict[tuple, int] = {}

    while not console.should_stop():
        drain_forced_input(console, ssm)

        try:
            response = llm.send_message(ssm.get_messages(phase), tools=AVAILABLE_TOOLS)
            parsed_response = console.print_agent_response(response)
        except Exception as e:
            console.display_error(f"LLM request failed: {e}")
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
                tool_result = execute_tool_call(console, ssm, tool_call, failures)
                console.display_tool_result(tool_result)

                ssm.append_raw({
                    "role": "tool",
                    "tool_call_id": tool_call["id"],
                    "name": tool_call["function"]["name"],
                    "content": str(tool_result)
                })

            console.set_status(state="thinking", plan=ssm.plan_progress())

        # --- CHECK FOR COMPLETION ---
        # Checked even alongside tool calls: ticking the last box and signing
        # off usually arrive in the same message.
        if phase_completed(content, phase):
            console.display_system(f"✅ Phase '{phase}' completed successfully.")
            console.mark_phase_done(phase)

            if phase == "planner":
                ssm.ensure_plan_file()
            console.set_status(plan=ssm.plan_progress())
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


def run_pipeline(console: AbstractManager, llm: ColibriLLMStream) -> None:
    console.set_status(phases=PHASES, state="waiting")
    console.display_rule("Just Finish It — Generic Autonomous Mode 🤖")
    console.display_system(
        "Type at any time. Enter queues a request for after the review phase; "
        "prefix with ! to run it in the current turn."
    )

    # 1. Gather Session Name
    session_name = console.get_user_input("Please enter a session name to begin:", multiline=False)
    if not session_name or console.should_stop():
        return
    console.set_status(session=session_name)

    # 2. Initialize Session Manager once
    ssm = SimpleSessionManager(console, session_name)

    if ssm.is_resuming:
        console.display_system(f"📁 Resuming existing session '{session_name}'...")
        initial_goal = "(Resuming previous session goal from history)"
    else:
        initial_goal = console.get_user_input("What is your goal? (Be as detailed as possible):", multiline=True)
        if not initial_goal or console.should_stop():
            return

    # 3. Resumed sessions pick up at the first phase that never completed
    iteration = 1
    console.start_iteration(iteration, PHASES)
    console.set_status(plan=ssm.plan_progress())
    active_phases = ssm.get_remaining_phases(PHASES)
    skipped = [p for p in PHASES if p not in active_phases]
    for phase in skipped:
        console.mark_phase_done(phase)
    if ssm.is_resuming and skipped:
        console.display_system(f"⏩ Skipping completed phases: {', '.join(skipped).upper()}")

    # 4. Outer Loop for Re-runs and Feedback
    while not console.should_stop():
        if not active_phases:
            console.display_system("All phases have already been completed for this session.")

        for phase in active_phases:
            trigger_message = get_phase_trigger(phase, initial_goal, ssm.plan_path, iteration)

            # Avoid inserting duplicate trigger if already present in history
            last_user_msg = next((m.get("content", "") for m in reversed(ssm.history) if m.get("role") == "user"), "")
            if trigger_message not in last_user_msg:
                ssm.add_message("user", trigger_message)

            if not run_phase(console, llm, ssm, phase):
                return

        # 5. Review has landed: queued requests loop us round again, no prompt
        feedback = collect_next_iteration(console)
        if feedback is None:
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

    console.set_status(phase="", state="finished", plan=ssm.plan_progress())
    console.display_rule("🎉 JUST FINISH IT — SESSION TERMINATED 🎉")


def main():
    load_dotenv()

    console = PromptToolkitConsoleManager()
    llm = ColibriLLMStream()

    try:
        # The console owns the terminal and runs the pipeline on a worker
        # thread, which is what keeps the bottom input line alive throughout.
        console.run(lambda: run_pipeline(console, llm))
    finally:
        llm.close()
        # Full-screen UI is gone by now; replay the transcript into scrollback.
        console.dump_transcript()


if __name__ == "__main__":
    main()
