"""load_tool: the model's own mechanism for unlocking a DEFERRED_TOOLS
schema (see tool/schemas.py) for the rest of its session, instead of every
request paying for schemas it may never use this run.

Bound to one session's own SessionManager the same way
context_tools.make_context_save/make_context_lookup are bound to one
session's context.json -- see runner._run_session where all three get
rebound from their import-time placeholders once a real session exists.
"""
from typing import Callable

from JFI.tool.schemas import DEFERRED_TOOL_NAMES


def load_tool(name: str, ssm) -> str:
    name = (name or "").strip()
    if not name:
        return "Error: load_tool needs a non-empty tool name."
    if not hasattr(ssm, "unlock_tool"):
        # A SessionManager implementation that never added this optional
        # method (see abstract_session_manager.py's docstring on optional
        # hooks like set_llm_streams) -- nothing to unlock, say so plainly
        # rather than silently pretending it worked.
        return "Error: this session's manager doesn't support load_tool."
    if not ssm.unlock_tool(name):
        return (
            f"Error: '{name}' isn't a deferred tool name. Available: "
            f"{', '.join(sorted(DEFERRED_TOOL_NAMES))}."
        )
    return f"'{name}' is now unlocked — call it directly on your NEXT turn onward."


def make_load_tool(ssm) -> Callable[[str], str]:
    """Binds load_tool to one session's own SessionManager -- mirrors
    context_tools.make_context_save's per-session binding pattern."""
    def bound(name: str = "") -> str:
        return load_tool(name, ssm)
    return bound
