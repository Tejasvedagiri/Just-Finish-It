import sys
import threading
import queue
from typing import Dict, Any

from prompt_toolkit import prompt, print_formatted_text, PromptSession
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.patch_stdout import patch_stdout

from manager.abstract_manager import AbstractManager


class PromptToolkitConsoleManager(AbstractManager):
    """
    A clean, minimalist terminal UI manager using prompt_toolkit.
    Guarantees background prints never collide with the prompt line and won't crash on raw file contents.
    """

    def __init__(self):
        super().__init__()
        self.kb = self._get_keybindings()
        self.session = PromptSession(
            key_bindings=self.kb,
            multiline=False
        )

    def display_user(self, text: str) -> None:
        print_formatted_text(HTML(f"<b><ansicyan>🧑 User: {text}</ansicyan></b>"))

    def display_assistant(self, text: str) -> None:
        print_formatted_text(HTML(f"<b><ansigreen>🤖 Assistant: {text}</ansigreen></b>"))

    def display_system(self, text: str) -> None:
        # Fixed: Plain string output to prevent XML parsing errors when reading code/markdown files
        print_formatted_text(f" ⚙️  [System]  {text}")

    def _get_keybindings(self) -> KeyBindings:
        kb = KeyBindings()

        @kb.add("enter")  # Standard Enter submits immediately
        def _(event): event.current_buffer.validate_and_handle()

        return kb

    def get_user_input(self, prompt_label: str = "You", multiline: bool = True) -> str:
        """Gathers one-off user input before the background thread starts."""
        print_formatted_text(HTML(f"\n<b><ansicyan>{prompt_label}</ansicyan></b>"))
        try:
            return prompt("> ", key_bindings=self.kb).strip()
        except (KeyboardInterrupt, EOFError):
            return ""

    def start_input_loop(self, user_queue: queue.Queue, shutdown_event: threading.Event) -> None:
        """
        Runs the persistent prompt loop with patch_stdout enabled.
        """
        self.display_system("⌨️  (Input Queue Active: Type your feedback and press Enter, or type 'exit' to quit)")

        with patch_stdout(raw=True):
            while not shutdown_event.is_set():
                try:
                    text = self.session.prompt(HTML("<b><ansigreen>></ansigreen></b> "))

                    if text.strip():
                        user_queue.put(text.strip())
                        if text.strip().lower() in ['exit', 'quit', 'done']:
                            shutdown_event.set()
                            break
                except KeyboardInterrupt:
                    self.display_system("\n ⚠️ Execution stopped by user via Ctrl+C.")
                    shutdown_event.set()
                    break
                except EOFError:
                    shutdown_event.set()
                    break

    def print_agent_response(self, agent_response: Any) -> Dict[str, Any]:
        """Consumes the LLM stream, rendering text safely above the bottom input line."""
        full_text = ""
        tool_calls_dict = {}

        print_formatted_text("--------------------------------------------------")
        print_formatted_text(HTML("<b><ansigreen>🤖 Assistant:</ansigreen></b> "), end="")
        sys.stdout.flush()

        for chunk in agent_response:
            delta = chunk.choices[0].delta

            if delta.content is not None:
                full_text += delta.content
                sys.stdout.write(delta.content)
                sys.stdout.flush()

            if getattr(delta, "tool_calls", None):
                for tc in delta.tool_calls:
                    idx = tc.index

                    if idx not in tool_calls_dict:
                        tool_calls_dict[idx] = {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": getattr(tc.function, "name", ""),
                                "arguments": ""
                            }
                        }
                        if tc.function and tc.function.name:
                            print_formatted_text(f" 🛠️  AI is preparing to call: {tc.function.name}")

                    if tc.function and getattr(tc.function, "arguments", None):
                        tool_calls_dict[idx]["function"]["arguments"] += tc.function.arguments

        print_formatted_text("--------------------------------------------------")

        tool_calls_list = list(tool_calls_dict.values()) if tool_calls_dict else None

        return {
            "content": full_text.strip() if full_text else None,
            "tool_calls": tool_calls_list
        }