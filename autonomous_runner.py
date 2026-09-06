import json
from dotenv import load_dotenv

# LLM and Console Management
from llm.colibri_llm_stream import ColibriLLMStream
from manager.rich_console_manager import RichConsoleManager

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


def main():
    # 1. Initialization
    load_dotenv()
    llm = ColibriLLMStream()
    console = RichConsoleManager()

    console.display_system("Welcome to Just Finish It - Generic Autonomous Mode 🤖")

    # 2. Gather Session Name
    session_name = console.get_user_input("Please enter a session name to begin: ")

    # 3. Initialize Session Manager once
    ssm = SimpleSessionManager(console, session_name)

    if ssm.is_resuming:
        console.display_system(f"📁 Resuming existing session '{session_name}'...")
        initial_goal = "(Resuming previous session goal from history)"
    else:
        initial_goal = console.get_user_input("\nWhat is your goal? (Be as detailed as possible): ")

    PHASES = ["planner", "imp", "testing", "reviewer"]
    is_first_run = True

    # 4. Outer Loop for Re-runs and Feedback
    while True:
        if not is_first_run:
            console.display_system("\n==========================================")
            console.display_system(" ⏸️  PIPELINE COMPLETE - WAITING FOR FEEDBACK")
            console.display_system("==========================================")

            feedback = console.get_user_input(
                "\nDo you want to change anything? (Type your feedback, or 'exit'/'done' to quit): "
            )

            if feedback.strip().lower() in ['exit', 'done', 'quit', 'no', 'nothing']:
                break

            ssm.add_message(
                "user",
                f"USER FEEDBACK FOR ITERATION:\n{feedback}\n\nPlease re-evaluate and update the project to satisfy these changes."
            )
            active_phases = PHASES
        else:
            active_phases = ssm.get_remaining_phases(PHASES)
            if ssm.is_resuming:
                skipped = [p for p in PHASES if p not in active_phases]
                if skipped:
                    console.display_system(f"⏩ Skipping completed phases: {', '.join(skipped).upper()}")

        is_first_run = False

        if not active_phases:
            console.display_system("All phases have already been completed for this session.")
            continue

        # 5. Main Autonomous Pipeline
        for phase in active_phases:
            console.display_system(f"\n==========================================")
            console.display_system(f"   STARTING PHASE: {phase.upper()}")
            console.display_system(f"==========================================\n")

            completion_keyword = f"{phase.upper()}_COMPLETE"
            trigger_message = get_phase_trigger(phase, initial_goal)

            # Avoid inserting duplicate trigger if already present in history
            last_user_msg = next((m.get("content", "") for m in reversed(ssm.history) if m.get("role") == "user"), "")
            if trigger_message not in last_user_msg:
                ssm.add_message("user", trigger_message)

            # 6. Continuous Tool Execution Loop for the current phase
            while True:
                try:
                    response = llm.send_message(ssm.get_messages(phase), tools=AVAILABLE_TOOLS)
                    parsed_response = console.print_agent_response(response)
                except KeyboardInterrupt:
                    console.display_system("\n⚠️ Execution paused by user. Saving state and exiting...")
                    return

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
                    for tc in tool_calls:
                        func_name = tc["function"]["name"]
                        args_str = tc["function"]["arguments"]
                        tc_id = tc["id"]

                        console.display_system(f"\n⚙️ Executing Tool: {func_name}")

                        try:
                            args = json.loads(args_str)
                            tool_result = TOOL_MAP[func_name](**args)

                        except json.JSONDecodeError as e:
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

                        console.display_system(f"Result: {tool_result}")

                        ssm.append_raw({
                            "role": "tool",
                            "tool_call_id": tc_id,
                            "name": func_name,
                            "content": str(tool_result)
                        })

                    continue

                # --- CHECK FOR COMPLETION ---
                if content and completion_keyword in content:
                    console.display_system(f"\n✅ Phase '{phase}' completed successfully.")

                    if phase == "planner":
                        ssm.generate_plan_markdown()

                    break

                # --- AUTO-NUDGE (STALL PREVENTION) ---
                if not tool_calls and completion_keyword not in (content or ""):
                    ssm.add_message(
                        "user",
                        f"Please continue your work. Remember, when you are entirely finished with this phase, you MUST output the exact phrase: '{completion_keyword}'."
                    )

    console.display_system("\n==========================================")
    console.display_system(" 🎉 JUST FINISH IT - SESSION TERMINATED 🎉")
    console.display_system("==========================================")


if __name__ == "__main__":
    main()