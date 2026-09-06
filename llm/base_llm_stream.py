from abc import ABC, abstractmethod
import os
from typing import Iterable

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
