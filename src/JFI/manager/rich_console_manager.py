import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, Optional

from rich.console import Console
from rich.markdown import Markdown
from rich.prompt import Prompt
from rich.theme import Theme
from prompt_toolkit import prompt
from prompt_toolkit.key_binding import KeyBindings

from JFI.manager.abstract_manager import AbstractManager
from JFI.manager.key_bindings import add_shift_enter_newline
from JFI.manager.theme_env import resolve_explicit_theme, theme_label


# Named color presets. Each entry maps the three role colors; an explicit
# ``THEME`` env value selects one of these and skips auto-detection entirely.
THEME_PRESETS: Dict[str, Dict[str, str]] = {
    # --- dark backgrounds ---
    "dark-default": {
        "user_theme": "bold cyan",
        "assistant_theme": "bold green",
        "system_theme": "bold bright_black",  # Clean, minimal gray text on dark screens
    },
    "dark-ocean": {
        "user_theme": "bold sky_blue1",
        "assistant_theme": "bold turquoise4",
        "system_theme": "slate_blue3",  # Cool blue/teal palette for dark screens
    },
    "dark-mono": {
        "user_theme": "bold white",
        "assistant_theme": "bold grey85",
        "system_theme": "grey50",  # Grayscale only — no hue at all
    },
    # --- light backgrounds ---
    "light-default": {
        "user_theme": "bold blue",
        "assistant_theme": "bold dark_green",
        "system_theme": "dim black",  # Subdued minimal text on light screens
    },
    "light-sunrise": {
        "user_theme": "bold magenta3",
        "assistant_theme": "dark_red",
        "system_theme": "dark_olive_green3",  # Warm palette for light screens
    },
    "light-paper": {
        "user_theme": "dark_blue",
        "assistant_theme": "dark_sea_green4",
        "system_theme": "grey50",  # Ink-on-paper feel, low saturation
    },
}


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
    return False


def _get_adaptive_palette(is_dark: bool) -> Dict[str, str]:
    """Returns colors optimized specifically for contrast against the background."""
    if is_dark:
        return dict(THEME_PRESETS["dark-default"])
    return dict(THEME_PRESETS["light-default"])


def resolve_theme() -> tuple[Dict[str, str], Optional[str]]:
    """Picks the palette and reports how it was chosen.

    Returns ``(palette, source)`` where *source* is ``"env:THEME"``,
    ``"auto (dark)"``, or ``"auto (light)"`` — or ``None`` when running on a
    non-terminal with no explicit theme. An explicit ``THEME`` preset name
    always wins over auto-detection; an empty/unset value (or the literal
    ``auto``) falls back to terminal detection. Unknown names log a system
    hint and fall back too, so a typo can never crash startup.
    """
    explicit = resolve_explicit_theme(THEME_PRESETS)
    if explicit is not None:
        return explicit

    is_terminal = getattr(Console(), "is_terminal", True)
    if not is_terminal:
        return dict(THEME_PRESETS["dark-default"]), None

    is_dark_mode = not _check_light_env()
    source = f"auto ({'dark' if is_dark_mode else 'light'})"
    return _get_adaptive_palette(is_dark_mode), source


class RichConsoleManager(AbstractManager):
    """
    Terminal UI manager that detects background brightness
    to prevent unreadable text combinations.
    Now thread-safe and compatible with prompt_toolkit.patch_stdout.
    """

    def __init__(self):
        # Explicit THEME env preset wins; otherwise auto-detect the terminal.
        palette, source = resolve_theme()
        self.theme_source = source
        self.theme = Theme(palette)

        # Startup hint: make the resolved theme visible so a user can confirm
        # their .env value (or the auto-detection fallback) took effect.
        if source is not None and sys.stdout.isatty():
            print(theme_label(source))

        # CRITICAL FIX: Force pure ANSI rendering and disable legacy OS fallbacks
        self.console = Console(
            theme=self.theme,
            force_terminal=True,
            force_interactive=True,
            legacy_windows=False,  # Prevents \x1b from turning into ?
            color_system="truecolor"  # Forces standard modern ANSI escape codes
        )
        self._log_file = None
        self._log_console: Optional[Console] = None

    def _dual_print(self, *args, **kwargs) -> None:
        """Prints to the terminal and, if a session log is open, to it too.

        A second ``Console`` pointed at the log file handles this for free:
        Rich only emits ANSI/color codes for a file it detects as a tty, so
        the exact same markup that colors the terminal comes out as clean
        plain text in the log.
        """
        self.console.print(*args, **kwargs)
        if self._log_console is not None:
            self._log_console.print(*args, **kwargs)

    def display_user(self, text: str) -> None:
        self._dual_print(f"[user_theme]🧑 User: {text}")

    def display_assistant(self, text: str) -> None:
        self._dual_print(f"[assistant_theme]🤖 Assistant: {text}")

    def display_system(self, text: str) -> None:
        self._dual_print(f"[system_theme] ⚙️  [System]  {text}")

    def start_session_log(self, path) -> None:
        """Opens ``path`` and points a second, non-tty ``Console`` at it, so
        every :meth:`_dual_print` call also lands there as clean plain text
        (Rich strips markup/color for a file it detects as a non-terminal).
        Appends across resumes, with a timestamped banner marking each run.
        """
        path = Path(path)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._log_file = open(path, "a", encoding="utf-8")
        except OSError as exc:
            self.display_error(f"Could not open run log at {path}: {exc}")
            return
        self._log_console = Console(file=self._log_file, force_terminal=False, no_color=True)
        timestamp = datetime.now().isoformat(timespec="seconds")
        self._log_file.write(f"\n===== JFI run started {timestamp} =====\n")
        self._log_file.flush()

    def close_session_log(self) -> None:
        self._log_console = None
        if self._log_file is not None:
            try:
                self._log_file.close()
            except OSError:
                pass
            self._log_file = None

    def dump_transcript(self) -> None:
        """Nothing to replay (this manager prints directly, no in-memory
        buffer) — just close the run log opened by :meth:`start_session_log`."""
        self.close_session_log()

    def prepare_input(self) -> None:
        """
        Ends any rich output that is mid-line before prompt_toolkit takes over the terminal.

        Streaming assistant text is printed with ``end=""``, which leaves the cursor on a
        half-finished line. When prompt_toolkit later redraws its frame it counts the
        visible lines from scratch, and the dangling partial line throws off its wrap
        math (``IndexError`` in ``_scroll_when_linewrapping``). Printing one empty line
        here closes out the rich side of the terminal so both renderers start from a
        clean line boundary.
        """
        self.console.print()

    @staticmethod
    def _build_key_bindings() -> KeyBindings:
        """Builds a fresh key-binding set for every prompt session (no stale state)."""
        kb = KeyBindings()

        # Alt+Enter and Shift+Enter (both escape-code encodings, plus
        # Ctrl+Enter) insert a newline; shared with PromptToolkitConsoleManager
        # so "Shift+Enter" means the same thing in both UIs.
        add_shift_enter_newline(kb)

        # Standard Enter submits the prompt
        @kb.add("enter")
        def _(event):
            event.current_buffer.validate_and_handle()

        return kb

    def get_user_input(self, prompt_label: str = "You", multiline: bool = True) -> str:
        """
        Gathers user input before the background thread starts.
        - Enter: Submits immediately.
        - Alt+Enter / Shift+Enter: Adds a new line.
        - Paste: Bracketed paste safely handles multi-line blocks without submitting.

        All rich output is completed (see :meth:`prepare_input`) *before* prompt_toolkit
        enters raw mode, and nothing is printed while it owns the frame — the label is
        passed to ``prompt()`` itself so both renderers never interleave on one line.

        Prompt-toolkit redraws can still hit transient event-loop glitches (e.g. an
        ``IndexError`` in its linewrap math); callers that may loop forever should use
        :meth:`safe_get_user_input`, which converts such hiccups into a clean re-prompt.
        """
        if not multiline:
            self.prepare_input()
            return Prompt.ask(prompt_label, console=self.console)

        # Close out any mid-line rich output before entering prompt_toolkit's frame.
        self.prepare_input()

        label = (
            f"{prompt_label} "
            "(Enter to submit, Shift+Enter or Alt+Enter for new line. Pasting blocks is safe): > "
        )

        try:
            # multiline=True automatically enables Bracketed Paste Mode for safe pasting;
            # fresh key bindings per call so no stale state survives a prior session.
            user_text = prompt(
                label,
                multiline=True,
                key_bindings=self._build_key_bindings(),
            )
            return user_text.strip()
        except (KeyboardInterrupt, EOFError):
            # Leave the terminal on a clean line so the next renderer can start fresh.
            self.prepare_input()
            return ""

    def safe_get_user_input(
        self,
        prompt_label: str = "You",
        multiline: bool = True,
        timeout: Optional[float] = None,
    ) -> Optional[str]:
        """Loop :meth:`get_user_input` until it yields cleanly or the caller's timeout fires.

        Prompt-toolkit runs its own event loop; a redraw hiccup (``IndexError`` in
        ``_scroll_when_linewrapping`` and friends, usually triggered by interleaved
        rich output) can surface out of ``prompt()`` as an exception. Instead of letting
        it kill the process, we log it to the system line and re-prompt so the user's
        terminal is left in a usable state.

        Returns:
            The stripped user text, or ``None`` if *timeout* seconds elapsed without a
            clean result (callers treat that as "no input yet").

        Note:
            A timeout means *no exception and no submission* — the caller must not assume
            the terminal is idle. In interactive use the loop simply keeps waiting; only
            non-interactive drivers (tests, scripts) pass a finite ``timeout``.
        """
        start = time.monotonic()
        while True:
            try:
                return self.get_user_input(prompt_label=prompt_label, multiline=multiline)
            except Exception as exc:  # noqa: BLE001 - any redraw glitch means "try again"
                try:
                    self.console.print(
                        f"[system_theme]⚠️ Input prompt interrupted ({exc.__class__.__name__}), re-prompting...[/]"
                    )
                except Exception:
                    pass  # Never let the recovery print hide the loop.
            if timeout is not None and time.monotonic() - start > timeout:
                return None

    def print_agent_response(self, agent_response: Any, prompt_tokens_estimate: int = 0) -> Dict[str, Any]:
        """
        Consumes the LLM stream, rendering text to the terminal sequentially with top/bottom borders.
        Simultaneously listens for and stitches together streaming tool calls.
        Returns a dictionary containing the full text and any tool calls made.

        ``prompt_tokens_estimate`` is accepted for interface parity with
        :class:`PromptToolkitConsoleManager`; this manager has no persistent
        status line to show a running token counter on, so it's unused here.
        """
        full_text = ""
        tool_calls_dict = {}  # Store tool calls by index to handle multiple tools in one stream

        # --- DRAW TOP BORDER FOR AI ---
        self._dual_print("\n[system_theme]╭──────────────────────────────────────────────────╮[/]")
        self._dual_print("[assistant_theme]🤖 Assistant:[/assistant_theme] ", end="")

        for chunk in agent_response:
            delta = chunk.choices[0].delta

            # 1. Handle standard text chunks
            if delta.content is not None:
                full_text += delta.content
                # Stream the text cleanly above the prompt bar
                self._dual_print(delta.content, end="")

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
                            self._dual_print(
                                f"\n[system_theme] 🛠️  [System] AI is preparing to call: {tc.function.name}[/]")

                    # Append argument fragments (JSON chunks)
                    if tc.function and getattr(tc.function, "arguments", None):
                        tool_calls_dict[idx]["function"]["arguments"] += tc.function.arguments

        # --- DRAW BOTTOM BORDER FOR AI ---
        self._dual_print("\n[system_theme]╰──────────────────────────────────────────────────╯[/]")

        # Convert the dictionary of tool calls into a clean list
        tool_calls_list = list(tool_calls_dict.values()) if tool_calls_dict else None

        # Return both the text and the structured tool calls for the runner to process
        return {
            "content": full_text.strip() if full_text else None,
            "tool_calls": tool_calls_list
        }