from abc import ABC, abstractmethod

class AbstractManager(ABC):
    """
    Abstract interface for handling chat UI outputs.
    Your service logic calls these methods without knowing *how* it's rendered.
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