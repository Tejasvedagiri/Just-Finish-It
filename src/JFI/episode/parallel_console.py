"""The console one of several concurrent episodes talks to (PARALLEL_LLM).

The console streams a reply token by token as it arrives; two episodes
streaming at once would interleave their tokens into one unreadable line.
So each episode's reply is collected first and drawn whole, labelled with
its node, one reply at a time. The collector stops where the console's own
output caps would stop the stream, so a runaway reply is still cut off (the
replay crosses the same cap and raises ResponseTooLongError as usual).

Questions to the user (the LLM-failure Retry/Stop menu) are asked one at a
time. Everything else goes straight to the real console.
"""

import os
import threading
from typing import Any, Dict

from JFI.manager.pt_console_manager import DEFAULT_REASONING_OUTPUT_CAP, DEFAULT_STREAM_OUTPUT_CAP


def _cap(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def _collect(response) -> list:
    stream_cap = _cap("STREAM_OUTPUT_CAP", DEFAULT_STREAM_OUTPUT_CAP)
    reasoning_cap = _cap("REASONING_OUTPUT_CAP", DEFAULT_REASONING_OUTPUT_CAP)
    chunks, answer_chars, reasoning_chars = [], 0, 0
    for chunk in response:
        chunks.append(chunk)
        for choice in getattr(chunk, "choices", None) or []:
            delta = choice.delta
            reasoning_chars += len(getattr(delta, "reasoning_content", None) or "")
            answer_chars += len(getattr(delta, "content", None) or "")
            for tc in getattr(delta, "tool_calls", None) or []:
                answer_chars += 1 + len(getattr(tc.function, "arguments", None) or "") if tc.function else 1
        if (reasoning_chars + answer_chars) // 4 > stream_cap or \
                (not answer_chars and reasoning_chars // 4 > reasoning_cap):
            break
    return chunks


class ParallelConsole:
    def __init__(self, console, label: str, render_lock: threading.RLock, ask_lock: threading.RLock):
        self._console, self._label = console, label
        self._render_lock, self._ask_lock = render_lock, ask_lock

    def __getattr__(self, name: str) -> Any:
        return getattr(self._console, name)

    def print_agent_response(self, agent_response: Any, prompt_tokens_estimate: int = 0) -> Dict[str, Any]:
        chunks = _collect(agent_response)
        with self._render_lock:
            self._console.display_system(f"── {self._label}")
            return self._console.print_agent_response(iter(chunks), prompt_tokens_estimate=prompt_tokens_estimate)

    def get_user_choice(self, prompt_label, options):
        with self._ask_lock:
            if self._console.should_stop():
                return "s"
            return self._console.get_user_choice(f"[{self._label}] {prompt_label}", options)

    def get_user_input(self, *args, **kwargs):
        with self._ask_lock:
            return self._console.get_user_input(*args, **kwargs)

    def safe_get_user_input(self, *args, **kwargs):
        with self._ask_lock:
            return self._console.safe_get_user_input(*args, **kwargs)
