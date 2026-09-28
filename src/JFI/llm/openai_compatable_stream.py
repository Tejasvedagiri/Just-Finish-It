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


def _rejects_stream_options(error: Exception) -> bool:
    """A 400 that names stream_options: the server doesn't know the option
    (some older OpenAI-compatible servers validate unknown fields strictly)."""
    status = getattr(error, "status_code", None)
    return status == 400 and "stream_options" in str(error)


class OpenAICompatableStream(BaseLLMStream):
    def __init__(self, prefix: str = ""):
        super().__init__(prefix)
        self.stream_service = initial_service(prefix)
        # Real token counts, not the chars/4 estimate, for the header and the
        # v2 episode budget: most servers only include `usage` in a stream
        # when asked. Turned off for the rest of the run the first time a
        # server rejects the option, so asking never breaks a working setup.
        self._ask_for_usage = True

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
            "frequency_penalty": float(self.frequency_penalty),
            "stream": True
        }

        # Only attach tools if they are provided
        if tools:
            kwargs["tools"] = tools

        if self._ask_for_usage:
            try:
                return self.stream_service.chat.completions.create(
                    **kwargs, stream_options={"include_usage": True})
            except Exception as e:
                if not _rejects_stream_options(e):
                    raise
                self._ask_for_usage = False
        return self.stream_service.chat.completions.create(**kwargs)
