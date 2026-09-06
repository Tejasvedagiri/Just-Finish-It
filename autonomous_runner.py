import json
from typing import Optional

from dotenv import load_dotenv

# LLM and Console Management
from llm.colibri_llm_stream import ColibriLLMStream
from manager.abstract_manager import AbstractManager
from manager.pt_console_manager import PromptToolkitConsoleManager

# Session Management
from session.simple_session_manager import SimpleSessionManager, get_phase_trigger

# Tools and Schemas
from tool.schemas import AVAILABLE_TOOLS
from tool.file_tools import write_file, read_file, append_to_file
from tool.cmd_tools import execute_command

# Dynamic mapping of tool names to their python functions
TOOL_MAP = {
    "write_file": write_file,
    "read_file": read_file,
    "append_to_file": append_to_file,
    "execute_command": execute_command
}

PHASES = ["planner", "imp", "testing", "reviewer"]

EXIT_WORDS = {"exit", "done", "quit", "no", "nothing"}


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
    Decides what the pipeline does now that the review phase has landed.

    Queued input wins: it is replayed as one feedback block and the pipeline
    loops on its own. With an empty queue we stop and ask. Returns None to end
    the session.
    """
    queued = console.drain_queued_input()

    if queued:
        requests = [q for q in queued if q.strip().lower() not in EXIT_WORDS]
        if not requests:
            return None
        console.display_rule(f"▶  RUNNING {len(requests)} QUEUED REQUEST(S)")
        for request in requests:
            console.display_user(request)
        return "\n".join(f"- {request}" for request in requests)

    console.set_status(phase="", state="waiting")
    console.display_rule("⏸  PIPELINE COMPLETE — WAITING FOR FEEDBACK")
    feedback = console.get_user_input(
        "Do you want to change anything? (Type your feedback, or 'exit'/'done' to quit):"
    )
    if not feedback or feedback.strip().lower() in EXIT_WORDS:
        return None
    return feedback


def run_phase(console: AbstractManager, llm: ColibriLLMStream, ssm: SimpleSessionManager, phase: str) -> bool:
    """
    Drives one phase to completion. Returns False if the run should stop early
    (user interrupt or LLM failure), True when the phase finished cleanly.
    """
    console.set_status(phase=phase, state="thinking")
    console.display_rule(f"PHASE: {phase.upper()}")

    completion_keyword = f"{phase.upper()}_COMPLETE"

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

        assistant_message = {"role": "assistant"}
        if content:
            assistant_message["content"] = content
        if tool_calls:
            assistant_message["tool_calls"] = tool_calls

        ssm.append_raw(assistant_message)

        # --- HANDLE TOOL CALLS ---
        if tool_calls:
            console.set_status(state="running tools")

            for tc in tool_calls:
                func_name = tc["function"]["name"]
                args_str = tc["function"]["arguments"]
                tc_id = tc["id"]

                try:
                    args = json.loads(args_str)
                    console.display_tool_call(func_name, args)
                    tool_result = TOOL_MAP[func_name](**args)

                    if func_name in ["write_file", "append_to_file"] and "Success" in str(tool_result):
                        ssm.track_file(args.get("file_path"))

                except json.JSONDecodeError as e:
                    console.display_tool_call(func_name)
                    tool_result = (
                        f"JSON parsing failed: {str(e)}. "
                        "You attempted to output too much text at once, causing a truncation error. "
                        "Please write to the file in smaller chunks using the append_to_file tool."
                    )
                    tc["function"]["arguments"] = json.dumps(
                        {"error": "malformed json stripped to prevent server crash"}
                    )
                    ssm.save_history()

                except Exception as e:
                    tool_result = f"Error executing tool {func_name}: {str(e)}"

                console.display_tool_result(tool_result)

                ssm.append_raw({
                    "role": "tool",
                    "tool_call_id": tc_id,
                    "name": func_name,
                    "content": str(tool_result)
                })

            console.set_status(state="thinking")
            continue

        # --- CHECK FOR COMPLETION ---
        if content and completion_keyword in content:
            console.display_system(f"✅ Phase '{phase}' completed successfully.")
            console.mark_phase_done(phase)

            if phase == "planner":
                ssm.generate_plan_markdown()

            return True

        # --- AUTO-NUDGE (STALL PREVENTION) ---
        ssm.add_message(
            "user",
            f"Please continue your work. Remember, when you are entirely finished with this phase, "
            f"you MUST output the exact phrase: '{completion_keyword}'."
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
            trigger_message = get_phase_trigger(phase, initial_goal)

            # Avoid inserting duplicate trigger if already present in history
            last_user_msg = next((m.get("content", "") for m in reversed(ssm.history) if m.get("role") == "user"), "")
            if trigger_message not in last_user_msg:
                ssm.add_message("user", trigger_message)

            if not run_phase(console, llm, ssm, phase):
                return

        # 5. Review has landed: queued requests loop us round again by default
        feedback = collect_next_iteration(console)
        if feedback is None:
            break

        ssm.add_message(
            "user",
            f"USER FEEDBACK FOR ITERATION:\n{feedback}\n\n"
            f"{ssm.get_project_state_summary()}\n\n"
            f"Please re-evaluate and update the project to satisfy these changes."
        )
        active_phases = PHASES

    console.set_status(phase="", state="finished")
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
