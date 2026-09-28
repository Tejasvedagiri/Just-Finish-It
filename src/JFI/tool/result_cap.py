"""Caps what a read tool returns, so one careless read can't eat a v2
episode's 20k-token budget (laya_plan.md §0, §5.3).

Every v2 read tool passes its result through cap_result() with a hint saying
how to read more precisely (a line range, one symbol, one key). A truncated
result says so and repeats the hint -- it is never silently cut.
"""

import os

DEFAULT_TOOL_RESULT_MAX_TOKENS = 3000
CHARS_PER_TOKEN = 4  # the same estimate the session manager uses when the server sends no usage


def tool_result_max_chars() -> int:
    try:
        tokens = int(os.environ.get("TOOL_RESULT_MAX_TOKENS", DEFAULT_TOOL_RESULT_MAX_TOKENS))
    except ValueError:
        tokens = DEFAULT_TOOL_RESULT_MAX_TOKENS
    return max(200, tokens) * CHARS_PER_TOKEN


def cap_result(text: str, hint: str) -> str:
    limit = tool_result_max_chars()
    if len(text) <= limit:
        return text
    shown = text[:limit].rsplit("\n", 1)[0] or text[:limit]
    return (f"{shown}\n[… truncated: showing {len(shown):,} of {len(text):,} characters "
            f"(TOOL_RESULT_MAX_TOKENS). {hint}]")
