import re
import io
import contextlib
from dotenv import load_dotenv

from rich.console import Console
from rich.markdown import Markdown
from rich.prompt import Prompt, Confirm
from rich.panel import Panel
from rich.live import Live

# Import your custom LLM stream class
from llm.colibri_llm_stream import ColibriLLMStream

# Load environment variables
load_dotenv()

console = Console()
stream = ColibriLLMStream()


class OpenCodeOrchestrator:
    def __init__(self, system_prompt=None):
        self.system_prompt = system_prompt or (
            "You are an AI coding assistant. Write Python code to solve the user's request. "
            "Always wrap code in ```python ... ``` blocks. Always print the final variables."
        )
        self.history = [{"role": "system", "content": self.system_prompt}]
        self.execution_state = {}

    def add_message(self, role: str, content: str):
        self.history.append({"role": role, "content": content})

    def extract_python_code(self, text: str) -> list[str]:
        return [match.strip() for match in re.findall(r"```python\n(.*?)\n```", text, flags=re.DOTALL | re.IGNORECASE)]


def execute_code(code: str, state: dict) -> str:
    """Runs code and captures stdout/stderr while keeping variables in 'state'."""
    output = io.StringIO()
    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
        try:
            exec(code, state)
        except Exception as e:
            print(f"Execution Error: {e}", file=output)

    result = output.getvalue().strip()
    return result if result else "[Executed successfully with no printed output]"


def generate_llm_response_stream(history: list) -> str:
    """
    Calls the ColibriLLMStream and dynamically renders the markdown
    in the terminal as chunks arrive.
    """
    resp = stream.send_message(history)
    full_response = ""

    console.print("\n[bold purple]Assistant:[/bold purple]")

    # Live allows us to update the rendered markdown as new tokens arrive
    with Live(Markdown(""), console=console, refresh_per_second=15, transient=False) as live:
        for chunk in resp:
            if chunk.choices[0].delta.content is not None:
                content = chunk.choices[0].delta.content
                full_response += content
                # Update the display with the new full text
                live.update(Markdown(full_response))

    return full_response


def main():
    orchestrator = OpenCodeOrchestrator()
    console.print(Panel.fit("[bold blue]OpenCode CLI v1.0[/bold blue]\nType 'exit' or 'quit' to close."))

    while True:
        # 1. Get User Input
        user_input = Prompt.ask("\n[bold green]You")
        if user_input.lower() in ['exit', 'quit']:
            break

        orchestrator.add_message("user", user_input)

        # 2. Get LLM Response and Stream it
        response_text = generate_llm_response_stream(orchestrator.history)

        # Save the full response to history
        orchestrator.add_message("assistant", response_text)

        # 3. Extract and Execute Code
        code_blocks = orchestrator.extract_python_code(response_text)

        for code in code_blocks:
            if Confirm.ask("\n[bold yellow]Execute this code?"):
                console.print("[dim]Executing...[/dim]")

                result = execute_code(code, orchestrator.execution_state)

                console.print(Panel(result, title="Execution Output", border_style="green"))

                # Feed the result back into the context for the next turn
                orchestrator.add_message("user", f"Execution Output:\n```text\n{result}\n```")
            else:
                console.print("[dim]Execution skipped.[/dim]")
                orchestrator.add_message("user", "User skipped code execution.")


if __name__ == "__main__":
    main()