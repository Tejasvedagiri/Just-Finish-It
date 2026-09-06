import os
from typing import Dict, Any

from rich.console import Console
from rich.markdown import Markdown
from rich.prompt import Prompt
from rich.theme import Theme
from prompt_toolkit import prompt
from prompt_toolkit.key_binding import KeyBindings

from manager.abstract_manager import AbstractManager


def _check_light_env() -> bool:
    """Helper to catch common environment hints for light-themed terminals."""
    colorfgbg = os.environ.get("COLORFGBG", "")
    if colorfgbg and ";" in colorfgbg:
        # COLORFGBG format is often "fg;bg". If bg is high number, it's light.
        try:
            bg = int(colorfgbg.split(";")[-1])
            return bg >= 7
        except ValueError:
            pass
    return "light" in os.environ.get("THEME", "").lower()


def _get_adaptive_palette(is_dark: bool) -> Dict[str, str]:
    """Returns colors optimized specifically for contrast against the background."""
    if is_dark:
        return {
            "user_theme": "bold cyan",
            "assistant_theme": "bold green",
            "system_theme": "bold bright_black"  # Clean, minimal gray text on dark screens
        }
    else:
        return {
            "user_theme": "bold blue",
            "assistant_theme": "bold dark_green",
            "system_theme": "dim black"  # Subdued minimal text on light screens
        }


class RichConsoleManager(AbstractManager):
    """
    Terminal UI manager that detects background brightness
    to prevent unreadable text combinations.
    Now thread-safe and compatible with prompt_toolkit.patch_stdout.
    """

    def __init__(self):
        # Detect environment or fallback safely to dark theme defaults
        probe_console = Console()
        is_dark_mode = getattr(probe_console, "is_terminal", True) and not _check_light_env()

        # Build dynamic color mapping based on palette detection
        palette = _get_adaptive_palette(is_dark_mode)
        self.theme = Theme(palette)

        # CRITICAL FIX: Force pure ANSI rendering and disable legacy OS fallbacks
        self.console = Console(
            theme=self.theme,
            force_terminal=True,
            force_interactive=True,
            legacy_windows=False,  # Prevents \x1b from turning into ?
            color_system="truecolor"  # Forces standard modern ANSI escape codes
        )
    def display_user(self, text: str) -> None:
        self.console.print(f"[user_theme]🧑 User: {text}")

    def display_assistant(self, text: str) -> None:
        self.console.print(f"[assistant_theme]🤖 Assistant: {text}")

    def display_system(self, text: str) -> None:
        self.console.print(f"[system_theme] ⚙️  [System]  {text}")

    def get_user_input(self, prompt_label: str = "You", multiline: bool = True) -> str:
        """
        Gathers user input before the background thread starts.
        - Enter: Submits immediately.
        - Alt+Enter / Shift+Enter: Adds a new line.
        - Paste: Bracketed paste safely handles multi-line blocks without submitting.
        """
        styled_prompt = f"\n[user_theme]{prompt_label}[/]"

        if not multiline:
            return Prompt.ask(styled_prompt, console=self.console)

        self.console.print(
            f"{styled_prompt} [dim](Enter to submit, Shift+Enter or Alt+Enter for new line. Pasting blocks is safe)[/]:"
        )

        kb = KeyBindings()

        # 1. Alt+Enter (Meta+Enter) adds a new line
        @kb.add("escape", "enter")
        def _(event):
            event.current_buffer.insert_text("\n")

        # 2. Shift+Enter support (Modern terminals using CSI-u protocols)
        @kb.add("escape", "[", "1", "3", ";", "2", "u")
        def _(event):
            event.current_buffer.insert_text("\n")

        # 3. Shift+Enter support (Older terminals that send Ctrl+J / Line Feed)
        @kb.add("c-j")
        def _(event):
            event.current_buffer.insert_text("\n")

        # 4. Standard Enter submits the prompt
        @kb.add("enter")
        def _(event):
            event.current_buffer.validate_and_handle()

        try:
            # multiline=True automatically enables Bracketed Paste Mode for safe pasting
            user_text = prompt("> ", multiline=True, key_bindings=kb)
            return user_text.strip()
        except (KeyboardInterrupt, EOFError):
            return ""

    def print_agent_response(self, agent_response: Any) -> Dict[str, Any]:
        """
        Consumes the LLM stream, rendering text to the terminal sequentially with top/bottom borders.
        Simultaneously listens for and stitches together streaming tool calls.
        Returns a dictionary containing the full text and any tool calls made.
        """
        full_text = ""
        tool_calls_dict = {}  # Store tool calls by index to handle multiple tools in one stream

        # --- DRAW TOP BORDER FOR AI ---
        self.console.print("\n[system_theme]╭──────────────────────────────────────────────────╮[/]")
        self.console.print("[assistant_theme]🤖 Assistant:[/assistant_theme] ", end="")

        for chunk in agent_response:
            delta = chunk.choices[0].delta

            # 1. Handle standard text chunks
            if delta.content is not None:
                full_text += delta.content
                # Stream the text cleanly above the prompt bar
                self.console.print(delta.content, end="")

            # 2. Handle tool call chunks
            if getattr(delta, "tool_calls", None):
                for tc in delta.tool_calls:
                    idx = tc.index

                    # Initialize a new tool call if we haven't seen this index yet
                    if idx not in tool_calls_dict:
                        tool_calls_dict[idx] = {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": getattr(tc.function, "name", ""),
                                "arguments": ""
                            }
                        }
                        # Display to the user that a tool is starting (on a new line)
                        if tc.function and tc.function.name:
                            self.console.print(
                                f"\n[system_theme] 🛠️  [System] AI is preparing to call: {tc.function.name}[/]")

                    # Append argument fragments (JSON chunks)
                    if tc.function and getattr(tc.function, "arguments", None):
                        tool_calls_dict[idx]["function"]["arguments"] += tc.function.arguments

        # --- DRAW BOTTOM BORDER FOR AI ---
        self.console.print("\n[system_theme]╰──────────────────────────────────────────────────╯[/]")

        # Convert the dictionary of tool calls into a clean list
        tool_calls_list = list(tool_calls_dict.values()) if tool_calls_dict else None

        # Return both the text and the structured tool calls for the runner to process
        return {
            "content": full_text.strip() if full_text else None,
            "tool_calls": tool_calls_list
        }