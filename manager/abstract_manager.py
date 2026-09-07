from abc import ABC, abstractmethod
from typing import Any, Callable, Dict, List, Optional


class AbstractManager(ABC):
    """
    Abstract interface for handling chat UI outputs.
    Your service logic calls these methods without knowing *how* it's rendered.

    Everything below the abstract block is optional polish: managers that can
    do better (a live status bar, a queued input line, scrollback) override it,
    plain managers inherit a sane fallback.
    """

    @abstractmethod
    def display_user(self, text: str) -> None:
        """Render user message."""
        pass

    @abstractmethod
    def display_assistant(self, text: str) -> None:
        """Render assistant response."""
        pass

    @abstractmethod
    def display_system(self, text: str) -> None:
        """Render system or background notification."""
        pass

    @abstractmethod
    def get_user_input(self, prompt_label: str = "You") -> str:
        """
        Abstract method to prompt and capture user text input.
        Returns the captured string safely to the service layer.
        """
        pass

    @abstractmethod
    def print_agent_response(self, agent_response: Any) -> Dict[str, Any]:
        """Consume an LLM stream and return {"content": str|None, "tool_calls": list|None}."""
        pass

    # ------------------------------------------------------------- input

    def safe_get_user_input(self, prompt_label: str = "You", **kwargs) -> Optional[str]:
        """
        Calls :meth:`get_user_input`, converting any unexpected error (e.g. a
        terminal redraw glitch such as an ``IndexError`` inside prompt_toolkit's
        event loop) into a clean re-prompt instead of letting it kill the process.

        Extra keyword arguments are forwarded to :meth:`get_user_input` so callers can
        pass manager-specific options (e.g. ``multiline``) unchanged.

        Returns:
            The user's input, or ``None`` if no answer was ever obtained before this
            method returned (managers that want a timeout can override with their own
            version; the default here retries indefinitely).
        """
        while True:
            try:
                return self.get_user_input(prompt_label=prompt_label, **kwargs)
            except Exception as exc:  # noqa: BLE001 - any hiccup means "try again"
                try:
                    self.display_system(
                        f"⚠️ Input prompt interrupted ({exc.__class__.__name__}), re-prompting..."
                    )
                except Exception:
                    pass  # Never let the recovery print hide the loop.

    # ------------------------------------------------------------- rendering

    def display_error(self, text: str) -> None:
        self.display_system(f"ERROR: {text}")

    def display_rule(self, label: str = "") -> None:
        self.display_system("-" * 50 if not label else f"{'-' * 8} {label} {'-' * 8}")

    def display_tool_call(self, name: str, args: Optional[Dict[str, Any]] = None) -> None:
        self.display_system(f"Executing tool: {name}")

    def display_tool_result(self, text: str) -> None:
        self.display_system(f"Result: {text}")

    # ------------------------------------------------------- queued input

    def drain_forced_input(self) -> List[str]:
        """Lines the user pushed to the front of the queue, for the next AI turn."""
        return []

    def drain_queued_input(self) -> List[str]:
        """Lines the user queued, to be replayed once the pipeline comes around."""
        return []

    def wait_for_queued_input(self, poll: float = 0.2) -> List[str]:
        """Block until the user queues work. Returns [] when unsupported."""
        return []

    def pending_input_count(self) -> int:
        return 0

    def forced_input_count(self) -> int:
        return 0

    # ------------------------------------------------------------- status

    def set_status(self, session: Optional[str] = None, phase: Optional[str] = None,
                   state: Optional[str] = None, phases: Optional[List[str]] = None,
                   plan: Optional[tuple] = None) -> None:
        pass

    def mark_phase_done(self, phase: str) -> None:
        pass

    def start_iteration(self, number: int, phases: Optional[List[str]] = None) -> None:
        """Called at the top of every pipeline pass so the UI can reset progress."""
        pass

    # ----------------------------------------------------------- lifecycle

    def should_stop(self) -> bool:
        """True once the user has asked to stop; long loops should check this."""
        return False

    def request_stop(self) -> None:
        pass

    def run(self, worker: Callable[[], Any]) -> Any:
        """Run the agent pipeline. UI managers may drive it on a worker thread."""
        return worker()

    def dump_transcript(self) -> None:
        pass
