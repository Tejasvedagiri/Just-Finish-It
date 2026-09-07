"""Reproduction script for the prompt_toolkit redraw IndexError in
_scroll_when_linewrapping, triggered by rich prints ending mid-line."""
import sys
from manager.rich_console_manager import RichConsoleManager

console = RichConsoleManager()

# 1. Print a system message (rich)
console.display_system("Welcome to Just Finish it")

# 2. Stream 'assistant' text with end='' — leaves cursor mid-line, no trailing newline
console.console.print("\n[system_theme]╭──────────────────────────────────────────────────╮[/]")
console.console.print("[assistant_theme]🤖 Assistant:[/assistant_theme] ", end="")
for i in range(20):
    console.console.print(f"chunk {i} ", end="")

# 3. Enter prompt_toolkit input — this is where the redraw crash happens
from prompt_toolkit import prompt
from prompt_toolkit.key_binding import KeyBindings

kb = KeyBindings()


@kb.add("enter")
def _(event):
    event.current_buffer.validate_and_handle()


try:
    result = prompt("> ", multiline=True, key_bindings=kb)
    print(f"RESULT: {repr(result)}")
except Exception as e:
    import traceback

    traceback.print_exc()
    print(f"EXCEPTION: {type(e).__name__}: {e}")
    sys.exit(1)
