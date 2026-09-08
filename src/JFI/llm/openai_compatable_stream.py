from JFI.llm.base_llm_stream import BaseLLMStream, phase_env
from openai import OpenAI


def initial_service(prefix: str = "") -> OpenAI:
    base_url = phase_env(prefix, "OPENAI_URL")
    api_key = phase_env(prefix, "OPENAI_API_KEY")
    missing = [name for name, value in (("OPENAI_URL", base_url), ("OPENAI_API_KEY", api_key)) if not value]
    if missing:
        hint = f" (or {prefix}_{missing[0]}, for the {prefix} phase)" if prefix else ""
        raise KeyError(f"Missing required .env setting(s): {', '.join(missing)}{hint}")
    return OpenAI(base_url=base_url, api_key=api_key)


class OpenAICompatableStream(BaseLLMStream):
    def __init__(self, prefix: str = ""):
        super().__init__(prefix)
        self.stream_service = initial_service(prefix)

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
