import json
import threading
import queue
from dotenv import load_dotenv

from llm.colibri_llm_stream import ColibriLLMStream
from manager.pt_console_manager import PromptToolkitConsoleManager

from session.simple_session_manager import SimpleSessionManager, get_phase_trigger
from tool.schemas import AVAILABLE_TOOLS
from tool.file_tools import write_file, read_file, append_to_file
from tool.cmd_tools import execute_command

TOOL_MAP = {
    "write_file": write_file,
    "read_file": read_file,
    "append_to_file": append_to_file,
    "execute_command": execute_command
}


def agent_worker(ssm, llm, console, initial_goal, PHASES, is_first_run, user_queue, shutdown_event):
    while not shutdown_event.is_set():
        if not is_first_run:
            console.display_system("\n" + "-" * 50)
            console.display_system(" ⏸️  PIPELINE COMPLETE - WAITING FOR FEEDBACK")
            console.display_system("-" * 50)

            feedback = user_queue.get()

            if feedback.strip().lower() in ['exit', 'done', 'quit', 'no', 'nothing']:
                shutdown_event.set()
                break

            ssm.add_message(
                "user",
                f"USER FEEDBACK FOR ITERATION:\n{feedback}\n\n"
                f"{ssm.get_project_state_summary()}\n\n"
                f"Please re-evaluate and update the project to satisfy these changes."
            )
            active_phases = PHASES
        else:
            active_phases = ssm.get_remaining_phases(PHASES)
            if ssm.is_resuming:
                skipped = [p for p in PHASES if p not in active_phases]
                if skipped:
                    console.display_system(f" ⏩ Skipping completed phases: {', '.join(skipped).upper()}")

        is_first_run = False

        if not active_phases:
            console.display_system(" ⚙️ All phases have already been completed for this session.")
            continue

        for phase in active_phases:
            if shutdown_event.is_set(): return

            console.display_system(f"\n" + "-" * 50)
            console.display_system(f" 🚀 STARTING PHASE: {phase.upper()}")
            console.display_system("-" * 50 + "\n")

            completion_keyword = f"{phase.upper()}_COMPLETE"
            trigger_message = get_phase_trigger(phase, initial_goal)

            last_user_msg = next((m.get("content", "") for m in reversed(ssm.history) if m.get("role") == "user"), "")
            if trigger_message not in last_user_msg:
                ssm.add_message("user", trigger_message)

            while not shutdown_event.is_set():
                while not user_queue.empty():
                    msg = user_queue.get()
                    if msg.strip().lower() in ['exit', 'quit']:
                        shutdown_event.set()
                        return
                    console.display_system(f"\n 📥 Queued Instruction Processed: {msg}")
                    ssm.add_message("user", f"USER INTERRUPT INSTRUCTION:\n{msg}")

                try:
                    response = llm.send_message(ssm.get_messages(phase), tools=AVAILABLE_TOOLS)
                    parsed_response = console.print_agent_response(response)
                except Exception as e:
                    console.display_system(f"\n ⚠️ Execution paused or errored: {e}")
                    shutdown_event.set()
                    return

                content = parsed_response.get("content")
                tool_calls = parsed_response.get("tool_calls")

                assistant_message = {"role": "assistant"}
                if content:
                    assistant_message["content"] = content
                if tool_calls:
                    assistant_message["tool_calls"] = tool_calls

                ssm.append_raw(assistant_message)

                if tool_calls:
                    for tc in tool_calls:
                        func_name = tc["function"]["name"]
                        args_str = tc["function"]["arguments"]
                        tc_id = tc["id"]

                        console.display_system(f"\n ⚙️ Executing Tool: {func_name}")

                        try:
                            args = json.loads(args_str)
                            tool_result = TOOL_MAP[func_name](**args)

                            if func_name in ["write_file", "append_to_file"] and "Success" in str(tool_result):
                                ssm.track_file(args.get("file_path"))

                        except json.JSONDecodeError as e:
                            tool_result = (
                                f"JSON parsing failed: {str(e)}. "
                                "You attempted to output too much text at once, causing a truncation error. "
                                "Please write to the file in smaller chunks using the append_to_file tool."
                            )
                            tc["function"]["arguments"] = json.dumps({"error": "malformed json stripped"})
                            ssm.save_history()
                        except Exception as e:
                            tool_result = f"Error executing tool {func_name}: {str(e)}"

                        console.display_system(f" ⚙️ Result: {tool_result}")
                        ssm.append_raw({
                            "role": "tool",
                            "tool_call_id": tc_id,
                            "name": func_name,
                            "content": str(tool_result)
                        })
                    continue

                if content and completion_keyword in content:
                    console.display_system(f"\n ✅ Phase '{phase}' completed successfully.")
                    if phase == "planner":
                        ssm.generate_plan_markdown()
                    break

                if not tool_calls and completion_keyword not in (content or ""):
                    ssm.add_message(
                        "user",
                        f"Please continue your work. Remember, when finished, output: '{completion_keyword}'."
                    )

    console.display_system("\n" + "-" * 50)
    console.display_system(" 🎉 JUST FINISH IT - SESSION TERMINATED 🎉")
    console.display_system("-" * 50)


def main():
    load_dotenv()
    llm = ColibriLLMStream()
    console = PromptToolkitConsoleManager()

    console.display_system(" ⚙️ Welcome to Just Finish It - Generic Autonomous Mode 🤖")

    session_name = console.get_user_input("Please enter a session name to begin: ", multiline=False)
    ssm = SimpleSessionManager(console, session_name)

    if ssm.is_resuming:
        console.display_system(f" 📁 Resuming existing session '{session_name}'...")
        initial_goal = "(Resuming previous session goal from history)"
    else:
        initial_goal = console.get_user_input("What is your goal? (Be as detailed as possible)", multiline=True)

    PHASES = ["planner", "imp", "testing", "reviewer"]
    user_queue = queue.Queue()
    shutdown_event = threading.Event()

    worker = threading.Thread(
        target=agent_worker,
        args=(ssm, llm, console, initial_goal, PHASES, True, user_queue, shutdown_event),
        daemon=True
    )
    worker.start()

    console.start_input_loop(user_queue, shutdown_event)

    worker.join(timeout=2)


if __name__ == "__main__":
    main()