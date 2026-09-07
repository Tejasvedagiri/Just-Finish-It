from JFI.llm.base_llm_stream import BaseLLMStream
import os
from openai import OpenAI


def initial_service():
    return OpenAI(
        base_url=os.environ["OPENAI_URL"],
        api_key=os.environ["OPENAI_API_KEY"],
    )


class OpenAICompatableStream(BaseLLMStream):
    def __init__(self):
        super().__init__()
        self.stream_service = initial_service()

    def close(self):
        self.stream_service.close()

    def send_message(self, message, tools=None):
        # Build the keyword arguments dynamically
        kwargs = {
            # self.model / self.temperature come from MODEL and TEMPERATURE in
            # .env; these used to be hardcoded, so changing .env did nothing.
            "model": self.model,
            "messages": message,
            "temperature": float(self.temperature),
            "stream": True
        }

        # Only attach tools if they are provided
        if tools:
            kwargs["tools"] = tools

        return self.stream_service.chat.completions.create(**kwargs)
