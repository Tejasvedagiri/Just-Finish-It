from dotenv import load_dotenv
from openai.types.containers import file_list_response

from llm.colibri_llm_stream import ColibriLLMStream
from manager.rich_console_manager import RichConsoleManager
from session.simple_session_manager import SimpleSessionManager
from inputimeout import inputimeout, TimeoutOccurred

# Load environment variables
load_dotenv()
llm = ColibriLLMStream()
console = RichConsoleManager()

console.display_system("Welcome to Just Finish it")
session_name = console.get_user_input("Please enter a session name to begin: ")

ssm = SimpleSessionManager(console, session_name)
ssm.add_message("assistant", "What do you want to do?")

while True:
    try:
        # Since inputimeout doesn't use Rich formatting, we print the prompt with your console first
        console.display_system("\nWhat do you want to do? (You have 120 seconds): ")

        # Wait for input for exactly 120 seconds
        user_input = inputimeout(prompt="> ", timeout=120)

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
        console.display_system("\nProvide feedback, type 'done' to finish, or wait 120s to auto-continue: ")
        user_input = inputimeout(prompt="> ", timeout=120)

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