from abc import ABC, abstractmethod
import os


def phase_env(prefix: str, key: str, fallback: str = "") -> str:
    """
    Resolves one .env setting with an optional per-phase override.

    `{prefix}_{key}` wins when `prefix` is given and that variable is set
    and non-empty (e.g. PLANNER_MODEL) — otherwise falls back to the shared
    `{key}` (e.g. MODEL), then to `fallback`. This is what lets .env give
    each phase (planner/imp/testing/reviewer) its own model and endpoint
    without requiring it: with no per-phase vars set, every phase resolves
    to the same shared default, exactly like before this existed.
    """
    if prefix:
        value = os.environ.get(f"{prefix}_{key}")
        if value:
            return value
    return os.environ.get(key, fallback)


class BaseLLMStream(ABC):
    def __init__(self, prefix: str = ""):
        # prefix is the phase's env-var prefix (e.g. "PLANNER"); empty means
        # "always use the shared, unprefixed settings" — used for anything
        # that isn't one of the four phases (e.g. legacy orchestrator use).
        self.prefix = prefix
        self.model = phase_env(prefix, "MODEL", "glm-5.3-flash-colibri")
        self.temperature = phase_env(prefix, "TEMPERATURE", "0.7")
        # 0.0 is the OpenAI API's own default (a no-op) -- unset means
        # unchanged behavior. A model prone to falling into verbatim
        # repetition loops (seen in practice on smaller/quantized models)
        # benefits from something like 0.3-0.5 here; stronger models
        # generally don't need it.
        self.frequency_penalty = phase_env(prefix, "FREQUENCY_PENALTY", "0.0")
        self.stream = True
        self.stream_service = None

    @abstractmethod
    def send_message(self, message):
        pass

    @abstractmethod
    def close(self):
        pass

    def check_user_approval(self, user_input: str) -> bool:
        # A strict system prompt forces the LLM to output only YES or NO
        eval_messages = [
            {
                "role": "system",
                "content": "You are an intent classifier. Evaluate if the user is approving the proposed plan or indicating they are ready to proceed. Reply with exactly 'YES' if they are approving, or 'NO' if they want changes/more planning. Say nothing else."
            },
            {"role": "user", "content": user_input}
        ]

        response_stream = self.send_message(eval_messages)

        # Consume the stream silently (no console updates)
        evaluation = ""
        for chunk in response_stream:
            if chunk.choices[0].delta.content is not None:
                evaluation += chunk.choices[0].delta.content

        # Return True if the LLM said YES
        return "YES" in evaluation.strip().upper()