import os
import json
import pickle
from pathlib import Path

from manager.abstract_manager import AbstractManager


def get_system_message(phase: str) -> str:
    if phase == "planner":
        return """
            You are a master Planner Agent. Your job is to generate a .md file containing a detailed, step-by-step plan to achieve the user's goal.
            The plan must be generic enough to support any task (writing, coding, research, data processing, etc.).
            All the plans must be numbered as 1 and sub plan must be 1.1 1.2 and so on. Each sub plan must have progress checkbox which can be check if completed by AI.

            The plan should contain:
            1. Title of the Project
            2. Required Context/Prerequisites
            3. Step-by-Step Execution Plan (Tabular format with checkboxes for progress)
            4. Final Validation/Review Step

            When you have finished outputting the plan, you must output the exact phrase: "PLANNER_COMPLETE".
        """

    elif phase == "imp":
        return """
            You are an expert Implementation Agent. Your job is to build, draft, or code the core deliverables specified in the markdown plan.

            Guidelines:
                1. Work step-by-step through the 'Implementation' portion of the plan.
                2. CRITICAL: Before calling ANY tool for a step, you MUST first output a text message explicitly stating which step you are working on, using exactly this format: 
                **[CURRENT TASK: Phase X - Step Y]**
                3. Use tools to create files, write code, or execute setup commands (use append_to_file for large documents).
                4. Evaluate tool results. If a file creation or command fails, attempt to fix it before moving on
                5. CRITICAL PROGRESS CHECKPOINT: Once all implementation steps are fully executed and verified, you MUST use the file tools to open your plan file (e.g., plan.md), update the progress checkboxes from empty [ ] to completed [x], and save it back out so progress is checkpointed.

                When the implementation and plan file updates are fully completed, you must output the exact phrase: "IMP_COMPLETE".
                """

    elif phase == "testing":
        return """
            You are an expert Testing Agent. Your job is to verify that the implementation works correctly and meets the goals of the plan.

            Guidelines:
            1. CRITICAL: Before testing a new section, explicitly state what you are testing using this format:
               **[CURRENT TEST: Section/Step Name]**
            2. Use tools to run tests, execute the code, or read the generated files to check for errors/line counts.
            3. If you find bugs, errors, or missing pieces, use the tools to fix them immediately and re-test.

            When all testing is successful and no further fixes are needed, you must output the exact phrase: "TESTING_COMPLETE".
        """

    elif phase == "reviewer":
        return """
            You are an expert Reviewer Agent. Your job is to perform a final audit of the entire project.

            Guidelines:
            1. Use tools to quickly inspect the final state of the files and outputs.
            2. Check for edge cases, missing requirements, or overall quality.
            3. Provide a final, polished summary report to the user detailing what was built and tested.

            When your review report is finished, you must output the exact phrase: "REVIEWER_COMPLETE".
        """

    return ""


def get_phase_trigger(phase: str, goal: str = "") -> str:
    """Provides the initial human prompt to kick off a specific phase."""
    if phase == "planner":
        return f"My goal is: {goal}\n\nPlease generate the detailed step-by-step markdown plan."
    elif phase == "imp":
        return "The plan is approved. Please begin the implementation phase step-by-step. Use tools to create the required files and build the project."
    elif phase == "testing":
        return "Implementation is complete. Please begin the testing phase. Run the necessary commands or scripts to verify everything works, and fix any bugs you find."
    elif phase == "reviewer":
        return "Testing is complete. Please review the final output against the original plan to ensure the goal was met. Provide a final summary report."
    return "Please continue."


class SimpleSessionManager:
    def __init__(self, console: AbstractManager, session_id: str):
        self.plan_content = ""
        self.console: AbstractManager = console
        self.session_id = session_id.lower().replace(" ", "_")

        # Path logic handled entirely inside the manager
        os_session_path = os.environ.get("SESSION_PATH", ".")
        self.session_path = Path(os_session_path) / ".just_finish_it" / self.session_id
        self.session_pickle_path = self.session_path / "history.pkl"

        # New: Metadata tracking path
        self.metadata_path = self.session_path / "metadata.json"

        # Check if history exists BEFORE loading it
        self.is_resuming = self.session_pickle_path.exists()

        self.history = self.load_history()
        self.metadata = self.load_metadata()

    def load_metadata(self):
        """Loads project metadata like tracked files."""
        if self.metadata_path.exists():
            try:
                with open(self.metadata_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                self.console.display_system(f"Error loading metadata: {e}")
        return {"implemented_files": []}

    def save_metadata(self):
        """Saves project metadata to disk."""
        with open(self.metadata_path, "w", encoding="utf-8") as f:
            json.dump(self.metadata, f, indent=4)

    def track_file(self, file_path: str):
        """Registers a newly created or modified file into the project state."""
        if file_path and file_path not in self.metadata["implemented_files"]:
            self.metadata["implemented_files"].append(file_path)
            self.save_metadata()

    def get_project_state_summary(self) -> str:
        """Returns a string listing all files currently tracked in the project."""
        files = self.metadata.get("implemented_files", [])
        if not files:
            return "No files have been tracked yet."

        summary = "CURRENT PROJECT FILES:\n"
        for f in files:
            summary += f"- {f}\n"
        summary += "\n(Tip for AI: If you need to change anything, use the read_file tool to inspect these files first, then use write_file or append_to_file to modify them.)"
        return summary

    def load_history(self):
        # Ensure the session folder exists
        self.session_path.mkdir(parents=True, exist_ok=True)

        # Check if history.pkl exists for this specific session
        if self.session_pickle_path.exists():
            try:
                with open(self.session_pickle_path, "rb") as f:
                    history = pickle.load(f)
                    self.console.display_system(
                        f"Resumed existing session '{self.session_id}' with {len(history)} past messages.")
                    return history
            except Exception as e:
                self.console.display_system(f"Error loading history: {e}. Starting fresh.")
        return []

    def save_history(self):
        """Dedicated method to write the history to disk."""
        with open(self.session_pickle_path, "wb") as f:
            pickle.dump(self.history, f)

    def add_message(self, role, message):
        """For simple text messages."""
        self.history.append({"role": role, "content": message})
        self.save_history()

    def append_raw(self, message_dict):
        """For complex dictionary messages (like tool calls) from the runner."""
        self.history.append(message_dict)
        self.save_history()

    def add_messages(self, messages):
        for message in messages:
            self.history.append(message)
        self.save_history()

    def get_messages(self, phase: str):
        message = [{"role": "system", "content": get_system_message(phase)}]
        message.extend(self.history)
        return message

    def generate_plan_markdown(self):
        # 1. Search backward through history to find the last assistant message
        for message in reversed(self.history):
            if message["role"] == "assistant" and message.get("content"):
                self.plan_content = message["content"]
                break

        if not self.plan_content:
            self.console.display_system("Warning: No plan found in history to save.")
            return

        # 2. Define the markdown file path using the session path and ID
        plan_file_path = self.session_path / "plan.md"

        # 3. Write the content to the file
        try:
            with open(plan_file_path, "w", encoding="utf-8") as f:
                f.write(self.plan_content)
            self.console.display_system(f"Plan successfully saved to: {plan_file_path}")
            # Also track the plan file!
            self.track_file(str(plan_file_path))
        except IOError as e:
            self.console.display_system(f"Failed to save plan markdown: {e}")

    def get_remaining_phases(self, all_phases: list[str]) -> list[str]:
        """
        Scans history to see which phases have already completed.
        Returns a sliced list starting from the first incomplete phase.
        """
        completed_phases = set()
        for msg in self.history:
            if msg.get("role") == "assistant" and msg.get("content"):
                content = msg["content"]
                for phase in all_phases:
                    if f"{phase.upper()}_COMPLETE" in content:
                        completed_phases.add(phase)

        # Find the first phase in sequence that hasn't completed yet
        for i, phase in enumerate(all_phases):
            if phase not in completed_phases:
                return all_phases[i:]

        # If all phases are already marked complete, return empty list
        return []