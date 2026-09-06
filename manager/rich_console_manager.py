from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.prompt import Prompt
from rich.theme import Theme
from typing import Dict, Any

from manager.abstract_manager import AbstractManager


def _check_light_env() -> bool:
    """Helper to catch common environment hints for light-themed terminals."""
    import os
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
    """

    def __init__(self):
        # Initialize a basic console first to probe terminal features
        probe_console = Console()

        # Detect environment or fallback safely to dark theme defaults
        is_dark_mode = getattr(probe_console, "is_terminal", True) and not _check_light_env()

        # Build dynamic color mapping based on palette detection
        palette = _get_adaptive_palette(is_dark_mode)

        self.theme = Theme(palette)
        self.console = Console(theme=self.theme)

    def display_user(self, text: str) -> None:
        self.console.print(f"[user_theme]🧑 User: {text}")

    def display_assistant(self, text: str) -> None:
        self.console.print(f"[assistant_theme]🤖 Assistant: {text}")

    def display_system(self, text: str) -> None:
        self.console.print(f"[system_theme] ⚙️  [System]  {text}")

    def get_user_input(self, prompt_label: str = "You") -> str:
        """
        Implements terminal input gathering.
        Uses adaptive 'user_theme' markup dynamically instead of hardcoded strings.
        """
        # Formats the adaptive style cleanly before executing Prompt.ask
        styled_prompt = f"\n[user_theme]{prompt_label}[/]"

        # Capture input utilizing your specialized console instance configuration
        return Prompt.ask(styled_prompt, console=self.console)

    def print_agent_response(self, agent_response: Any) -> Dict[str, Any]:
        """
        Consumes the LLM stream, rendering text to the terminal in real-time.
        Simultaneously listens for and stitches together streaming tool calls.
        Returns a dictionary containing the full text and any tool calls made.
        """
        full_text = ""
        tool_calls_dict = {}  # Store tool calls by index to handle multiple tools in one stream

        with Live(Markdown(""), console=self.console, refresh_per_second=15, transient=False) as live:
            for chunk in agent_response:
                delta = chunk.choices[0].delta

                # 1. Handle standard text chunks
                if delta.content is not None:
                    full_text += delta.content
                    live.update(Markdown(full_text), refresh=True)

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
                            # Optionally display to the user that a tool is starting
                            if tc.function and tc.function.name:
                                self.console.print(
                                    f"\n[system_theme] 🛠️  [System] AI is preparing to call: {tc.function.name}[/]")

                        # Append argument fragments (JSON chunks)
                        if tc.function and getattr(tc.function, "arguments", None):
                            tool_calls_dict[idx]["function"]["arguments"] += tc.function.arguments

        # Convert the dictionary of tool calls into a clean list
        tool_calls_list = list(tool_calls_dict.values()) if tool_calls_dict else None

        # Return both the text and the structured tool calls for the runner to process
        return {
            "content": full_text.strip() if full_text else None,
            "tool_calls": tool_calls_list
        }