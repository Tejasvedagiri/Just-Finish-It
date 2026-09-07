from dotenv import load_dotenv
from openai.types.containers import file_list_response

from llm.colibri_llm_stream import ColibriLLMStream
from manager.rich_console_manager import RichConsoleManager
from session.simple_session_manager import SimpleSessionManager
from inputimeout import inputimeout, TimeoutOccurred


def _timed_input(console: RichConsoleManager, prompt_label: str, timeout: float = 120) -> str:
    """
    Reads user input with a timeout, re-prompting on any unexpected error.

    ``inputimeout`` reads raw stdin (no event loop of its own), but the rich
    prints that surround it can still leave the terminal in a half-rendered state;
    if anything raises here we surface it as a clean retry instead of letting it
    kill the process. ``TimeoutOccurred`` is propagated to the caller so the
    auto-approve / auto-continue behaviour stays intact.
    """
    while True:
        try:
            console.display_system(f"\n{prompt_label}")
            return inputimeout(prompt="> ", timeout=timeout)
        except TimeoutOccurred:
            raise  # let the caller decide what a timeout means
        except Exception as exc:  # noqa: BLE001 - any hiccup means "try again"
            console.display_system(
                f"\u26a0\ufe0f Input interrupted ({exc.__class__.__name__}), re-prompting..."
            )


# Load environment variables
load_dotenv()
llm = ColibriLLMStream()
console = RichConsoleManager()

console.display_system("Welcome to Just Finish it")
session_name = console.safe_get_user_input(
    "Please enter a session name to begin: ", multiline=False
)

ssm = SimpleSessionManager(console, session_name or "default")
ssm.add_message("assistant", "What do you want to do?")

while True:
    try:
        user_input = _timed_input(console, "What do you want to do? (You have 120 seconds): ")
    except TimeoutOccurred:
        # This block triggers automatically if 120 seconds pass with no enter key pressed
        console.display_system("\nTimeout reached. Proceeding automatically...")

        # --- SET YOUR CUSTOM MESSAGE HERE ---
        user_input = "I Approve. Please proceed."

    # 1. Use the AI to check if the user is approving the plan
    console.display_system("Evaluating intent...")  # Optional: let user know it's thinking
    is_approved = llm.check_user_approval(user_input)
    console.display_system("Intent evaluated")

    if is_approved:
        console.display_system("Plan approved! Moving to execution...")
        ssm.add_message("user", user_input)  # Add their final confirmation to history
        break  # Exit the loop

    # 2. If NOT approved, proceed with planning
    ssm.add_message("user", user_input)

    response = llm.send_message(ssm.get_messages("planner"))
    full_response = console.print_agent_response(response)

    ssm.add_message("assistant", full_response)

# ==========================================
# --- Phase 2: EXECUTION ---
# ==========================================

console.display_system("\n--- Starting Execution Phase ---")

# 1. Kick off the execution phase automatically
kickoff_message = "The plan is approved. Please begin executing the 'Steps of Implementation' one by one. Stop and ask for my input if you need to run commands, create files, or if you finish a step."
ssm.add_message("user", kickoff_message)

# Get the initial execution response
response = llm.send_message(ssm.get_messages("execute"))
full_response = console.print_agent_response(response)
ssm.add_message("assistant", full_response)

# 2. Start the interactive execution loop
while True:
    try:
        user_input = _timed_input(
            console, "Provide feedback, type 'done' to finish, or wait 120s to auto-continue: "
        )
    except TimeoutOccurred:
        console.display_system("\nTimeout reached. Auto-prompting agent to continue...")
        user_input = "Looks good so far. Please continue with the next step."

    # 3. Allow manual exit when the project is finished
    if user_input.strip().lower() in ["done", "exit", "quit", "finish"]:
        console.display_system("Execution complete. Shutting down Just Finish It. Great job!")
        break

    # 4. Proceed with execution feedback
    ssm.add_message("user", user_input)

    # Note: We pass "execute" here to fetch the execution system prompt
    response = llm.send_message(ssm.get_messages("execute"))
    full_response = console.print_agent_response(response)

    ssm.add_message("assistant", full_response)
