from typing import Iterable

from openai.types.chat import ChatCompletionMessageParam

from llm.base_llm_stream import BaseLLMStream
import os
from openai import OpenAI


def initial_service():
    return OpenAI(
        base_url=os.environ["OPENAI_URL"],
        api_key=os.environ["OPENAI_API_KEY"],
    )


class ColibriLLMStream(BaseLLMStream):
    def __init__(self):
        super().__init__()
        self.stream_service = initial_service()

    def close(self):
        self.stream_service.close()

    def send_message(self, message):
        print(message)
        print(self.model)
        print(self.stream)
        return self.stream_service.chat.completions.create(
            model="glm-5.3-flash-colibri",  # Pass the model identifier used by your custom backend
            messages=message,
            temperature=0.7,
            stream=True  # Enables streaming chunks as they are generated
        )

