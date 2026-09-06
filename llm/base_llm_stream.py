from abc import ABC, abstractmethod
import os
from typing import Iterable
from rich.live import Live
from rich.markdown import Markdown
from openai.types.chat import ChatCompletionMessageParam


class BaseLLMStream(ABC):
    def __init__(self):
        self.model = os.environ.get("MODEL", "glm-5.3-flash-colibri")
        self.temperature = os.environ.get("TEMPERATURE", "0.7")
        self.stream = True
        self.stream_service = None

    @abstractmethod
    def send_message(self, message):
        pass

    @abstractmethod
    def close(self):
        pass

    def generate_llm_response_stream(self, console, history: list) -> str:
        resp = self.stream.send_message(history)
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
