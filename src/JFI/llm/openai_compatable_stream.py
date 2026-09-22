from JFI.llm.base_llm_stream import BaseLLMStream, phase_env
from openai import OpenAI

# The openai SDK's own default read timeout is 600s (10 minutes) with no
# progress feedback in between -- so a connection that goes stale (a local
# server that unloaded its model after idling, a dead localhost socket, ...)
# looks indistinguishable from a genuine hang for up to ten minutes, and
# Ctrl+C can't interrupt a blocking read already in flight on the worker
# thread (pt_console_manager's "c-c" binding only sets a cooperative stop
# flag). A much shorter default here means a stall surfaces quickly through
# the existing Retry/Stop menu (runner.py::_format_llm_error) instead of
# hanging silently. Override via LLM_REQUEST_TIMEOUT (seconds, per-phase
# prefixable like MODEL/OPENAI_URL) if a slower server genuinely needs more.
DEFAULT_REQUEST_TIMEOUT = 120.0


def initial_service(prefix: str = "") -> OpenAI:
    base_url = phase_env(prefix, "OPENAI_URL")
    api_key = phase_env(prefix, "OPENAI_API_KEY")
    missing = [name for name, value in (("OPENAI_URL", base_url), ("OPENAI_API_KEY", api_key)) if not value]
    if missing:
        hint = f" (or {prefix}_{missing[0]}, for the {prefix} phase)" if prefix else ""
        raise KeyError(f"Missing required .env setting(s): {', '.join(missing)}{hint}")
    try:
        timeout = float(phase_env(prefix, "LLM_REQUEST_TIMEOUT", str(DEFAULT_REQUEST_TIMEOUT)))
    except ValueError:
        timeout = DEFAULT_REQUEST_TIMEOUT
    return OpenAI(base_url=base_url, api_key=api_key, timeout=timeout)


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
            # .env_bk; these used to be hardcoded, so changing .env_bk did nothing.
            "model": self.model,
            "messages": message,
            "temperature": float(self.temperature),
            "frequency_penalty": float(self.frequency_penalty),
            "stream": True
        }

        # Only attach tools if they are provided
        if tools:
            kwargs["tools"] = tools

        return self.stream_service.chat.completions.create(**kwargs)
