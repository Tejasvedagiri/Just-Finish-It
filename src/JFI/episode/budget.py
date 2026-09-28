"""The per-episode token budget (laya_plan.md §0, D20).

Every v2 episode must fit in EPISODE_TOKEN_BUDGET tokens (default 20,000):
system prompt, brief, tool schemas, tool results and the model's own turns.
JFI's models usually have a 32k-40k window; the budget is capped at
CONTEXT_SIZE x CONTEXT_COMPRESSION_RATIO (the same two settings v1 uses, per
role via its env-prefix chain) so it can never exceed what the model holds.
"""

import json
import os
from typing import Any, List, Optional

from JFI.llm.base_llm_stream import phase_env

DEFAULT_EPISODE_TOKEN_BUDGET = 20_000
DEFAULT_CONTEXT_SIZE = 32_768  # same defaults as SimpleSessionManager
DEFAULT_CONTEXT_RATIO = 0.7
DEFAULT_MAX_EPISODE_TURNS = 15
CHARS_PER_TOKEN = 4


def episode_token_budget(prefixes=()) -> int:
    try:
        budget = int(os.environ.get("EPISODE_TOKEN_BUDGET", DEFAULT_EPISODE_TOKEN_BUDGET))
    except ValueError:
        budget = DEFAULT_EPISODE_TOKEN_BUDGET
    try:
        window = int(phase_env(prefixes, "CONTEXT_SIZE", str(DEFAULT_CONTEXT_SIZE)))
    except ValueError:
        window = DEFAULT_CONTEXT_SIZE
    try:
        ratio = float(os.environ.get("CONTEXT_COMPRESSION_RATIO", DEFAULT_CONTEXT_RATIO))
    except ValueError:
        ratio = DEFAULT_CONTEXT_RATIO
    return max(1_000, min(budget, int(window * ratio)))


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
