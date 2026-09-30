"""The per-episode token budget (laya_plan.md §0, D20).

Every episode must fit in CONTEXT_SIZE x CONTEXT_COMPRESSION_RATIO tokens
(per role via its env-prefix chain): system prompt, brief, tool schemas, tool
results and the model's own turns; the rest of the window is left for the
reply. There used to be a separate EPISODE_TOKEN_BUDGET (20,000) under that;
the user dropped it -- "I know I can use that size" -- so the context the
server really holds is the only knob.
"""

import json
import os
from typing import Any, List, Optional

from JFI.llm.base_llm_stream import phase_env

DEFAULT_CONTEXT_SIZE = 32_768  # same defaults as SimpleSessionManager
DEFAULT_CONTEXT_RATIO = 0.7
DEFAULT_MAX_EPISODE_TURNS = 25
CHARS_PER_TOKEN = 4


def episode_token_budget(prefixes=()) -> int:
    try:
        window = int(phase_env(prefixes, "CONTEXT_SIZE", str(DEFAULT_CONTEXT_SIZE)))
    except ValueError:
        window = DEFAULT_CONTEXT_SIZE
    try:
        ratio = float(os.environ.get("CONTEXT_COMPRESSION_RATIO", DEFAULT_CONTEXT_RATIO))
    except ValueError:
        ratio = DEFAULT_CONTEXT_RATIO
    return max(1_000, int(window * ratio))


def max_episode_turns() -> int:
    try:
        return max(1, int(os.environ.get("MAX_EPISODE_TURNS", DEFAULT_MAX_EPISODE_TURNS)))
    except ValueError:
        return DEFAULT_MAX_EPISODE_TURNS


def estimate_tokens(messages: List[dict], tools: Optional[List[dict]] = None) -> int:
    """The chars/4 estimate of one request -- used only when the server
    didn't report usage (laya_plan.md G14)."""
    chars = len(json.dumps(messages, ensure_ascii=False, default=str))
    if tools:
        chars += len(json.dumps(tools, ensure_ascii=False))
    return chars // CHARS_PER_TOKEN


def usage_tokens(usage: Optional[dict[str, Any]]) -> Optional[int]:
    if not usage:
        return None
    return (usage.get("prompt_tokens") or 0) + (usage.get("completion_tokens") or 0)
