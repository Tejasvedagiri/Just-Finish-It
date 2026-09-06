import re
import json


class BasicMessageOrchestrator:
    def __init__(self, system_prompt=None):
        # Default system prompt instructs the model on how to behave and format output
        default_system_prompt = (
            "You are an AI coding assistant with code execution capabilities. "
            "When you need to execute code to answer a user's request, write the Python code "
            "inside a markdown code block starting with ```python and ending with ```. "
            "Always print the final result or variables you want to observe."
        )
        self.system_prompt = system_prompt or default_system_prompt

        # Initialize conversation history with the system prompt
        self.history = [{"role": "system", "content": self.system_prompt}]

    def add_user_message(self, message: str):
        """Appends a user message to the history."""
        self.history.append({"role": "user", "content": message})

    def add_assistant_message(self, message: str):
        """Appends the LLM's raw text response to the history."""
        self.history.append({"role": "assistant", "content": message})

    def add_execution_result(self, result: str):
        """Appends the result of the code execution so the LLM can read it."""
        # We can format this as a 'user' or 'tool' message depending on the specific LLM API
        formatted_result = f"Execution Output:\n```text\n{result}\n```"
        self.history.append({"role": "user", "content": formatted_result})

    def add_message(self, role: str, content: str):
        self.history.append({"role": role, "content": content})

    def extract_python_code(self, text: str) -> list[str]:
        """
        Parses the LLM's response to extract all Python code blocks.
        Returns a list of code strings.
        """
        # Regex to match content between ```python and ```
        # re.DOTALL ensures the . matches newlines
        pattern = r"```python\n(.*?)\n```"
        matches = re.findall(pattern, text, flags=re.DOTALL | re.IGNORECASE)

        # Clean up whitespace and return
        return [match.strip() for match in matches]

    def get_context(self) -> list[dict]:
        """Returns the full conversation history for the LLM."""
        return self.history