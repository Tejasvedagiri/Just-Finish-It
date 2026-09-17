"""Deferred tool schemas — every request used to pay for all ~13 tools'
full JSON schemas (~2700 tokens) on EVERY turn, whether or not that turn
(or that session) ever needed most of them. Only load_tool is a CORE_TOOL
(always sent); everything else, including write_file/execute_command/
read_file, is a DEFERRED_TOOL withheld until the model calls
load_tool(name) to unlock it for the rest of the session — unlocking
persists (SimpleSessionManager.unlock_tool), so each tool costs its schema
exactly once per session, ever, no matter how many times it's used after
that. This covers the split itself, the unlock bookkeeping, load_tool's
own handler, and runner._tools_for_session actually honoring it.
"""
from JFI.tool.schemas import (
    CORE_TOOLS, DEFERRED_TOOLS, DEFERRED_TOOL_NAMES, AVAILABLE_TOOLS, deferred_tools_rules,
)
from JFI.tool.deferred_tools import load_tool, make_load_tool


def _names(tools):
    return {t["function"]["name"] for t in tools}


def test_core_and_deferred_tools_partition_available_tools():
    assert _names(CORE_TOOLS) | _names(DEFERRED_TOOLS) == _names(AVAILABLE_TOOLS)
    assert not (_names(CORE_TOOLS) & _names(DEFERRED_TOOLS))


def test_load_tool_itself_is_a_core_tool_so_it_can_always_be_called():
    assert "load_tool" in _names(CORE_TOOLS)


def test_deferred_tool_names_matches_deferred_tools_list():
    assert DEFERRED_TOOL_NAMES == _names(DEFERRED_TOOLS)


def test_only_load_tool_is_core_everything_else_is_deferred():
    """The aggressive split: even the highest-frequency tools
    (write_file/execute_command/read_file/...) are deferred, since
    unlocking is a one-time cost per session while the per-turn schema
    savings compound for the rest of it — see the module docstring."""
    assert _names(CORE_TOOLS) == {"load_tool"}
    for name in ("browse_webpage", "capture_screenshot", "view_image",
                 "fetch_webpage_images", "extract_video_frames",
                 "write_file", "read_file", "execute_command", "replace_in_file",
                 "append_to_file", "context_save", "context_lookup", "ask_llm"):
        assert name in DEFERRED_TOOL_NAMES


# ---------------------------------------------------------------------------
# SimpleSessionManager.unlock_tool / unlocked_tools
# ---------------------------------------------------------------------------

def test_unlocked_tools_starts_empty(manager):
    assert manager.unlocked_tools() == []


def test_unlock_tool_accepts_a_real_deferred_tool(manager):
    assert manager.unlock_tool("browse_webpage") is True
    assert manager.unlocked_tools() == ["browse_webpage"]


def test_unlock_tool_rejects_an_unknown_name(manager):
    assert manager.unlock_tool("not_a_real_tool") is False
    assert manager.unlocked_tools() == []


def test_unlock_tool_rejects_load_tool_itself(manager):
    """load_tool is the one CORE_TOOL — already always available, so
    "unlocking" it would just be confusing state, not a real action."""
    assert manager.unlock_tool("load_tool") is False


def test_unlock_tool_accepts_a_high_frequency_tool_too(manager):
    """Even write_file/execute_command/read_file are DEFERRED now (only
    load_tool is core) — see the module docstring for why that's still a
    net win despite these being the most-used tools."""
    assert manager.unlock_tool("write_file") is True
    assert manager.unlock_tool("execute_command") is True
    assert set(manager.unlocked_tools()) == {"write_file", "execute_command"}


def test_unlock_tool_is_idempotent(manager):
    manager.unlock_tool("browse_webpage")
    manager.unlock_tool("browse_webpage")
    assert manager.unlocked_tools() == ["browse_webpage"]


def test_unlock_tool_persists_across_a_resumed_session(make_manager):
    ssm = make_manager("unlock-persists")
    ssm.unlock_tool("capture_screenshot")

    resumed = make_manager("unlock-persists")

    assert resumed.unlocked_tools() == ["capture_screenshot"]


# ---------------------------------------------------------------------------
# _tools_schema_tokens / context_budget grow with what's actually unlocked
# ---------------------------------------------------------------------------

def test_context_budget_reserves_more_once_a_tool_is_unlocked(manager):
    before = manager.context_budget()
    manager.unlock_tool("browse_webpage")
    after = manager.context_budget()

    # Unlocking a tool means MORE schema bytes go out with every request, so
    # the usable budget for actual conversation history must shrink to
    # compensate, not silently overflow the real context window.
    assert after < before


def test_estimate_request_tokens_grows_once_a_tool_is_unlocked(manager):
    messages = [{"role": "user", "content": "hi"}]
    before = manager.estimate_request_tokens(messages)
    manager.unlock_tool("browse_webpage")
    after = manager.estimate_request_tokens(messages)

    assert after > before


# ---------------------------------------------------------------------------
# runner._tools_for_session
# ---------------------------------------------------------------------------

def test_tools_for_session_is_core_only_by_default(manager):
    from JFI.runner import _tools_for_session

    tools = _tools_for_session(manager)

    assert _names(tools) == _names(CORE_TOOLS)


def test_tools_for_session_adds_only_unlocked_deferred_tools(manager):
    from JFI.runner import _tools_for_session

    manager.unlock_tool("browse_webpage")
    tools = _tools_for_session(manager)

    assert _names(tools) == _names(CORE_TOOLS) | {"browse_webpage"}
    assert "capture_screenshot" not in _names(tools)  # never unlocked


# ---------------------------------------------------------------------------
# load_tool handler (the actual tool the model calls)
# ---------------------------------------------------------------------------

def test_load_tool_success_message_names_the_tool(manager):
    result = load_tool("browse_webpage", manager)

    assert "browse_webpage" in result
    assert "unlocked" in result.lower()
    assert manager.unlocked_tools() == ["browse_webpage"]


def test_load_tool_error_lists_valid_names(manager):
    result = load_tool("frobnicate_the_widget", manager)

    assert "Error" in result
    assert "browse_webpage" in result  # the valid-options list


def test_load_tool_rejects_empty_name(manager):
    result = load_tool("", manager)

    assert "Error" in result
    assert manager.unlocked_tools() == []


def test_make_load_tool_binds_to_one_session(make_manager):
    ssm_a = make_manager("bind-a")
    ssm_b = make_manager("bind-b")
    bound_a = make_load_tool(ssm_a)

    bound_a("view_image")

    assert ssm_a.unlocked_tools() == ["view_image"]
    assert ssm_b.unlocked_tools() == []


# ---------------------------------------------------------------------------
# AdaptiveSessionManager — this is the SessionManager real sessions actually
# use (SESSION_MANAGER=adaptive), not SimpleSessionManager directly. It
# doesn't override unlock_tool/unlocked_tools/context_budget/
# estimate_request_tokens (only __init__/_goal_text/task_type/
# _plan_format_rules — see adaptive_session_manager.py), so this is a
# direct check rather than trusting that inheritance alone.
# ---------------------------------------------------------------------------

def test_unlock_tool_works_on_adaptive_session_manager(make_adaptive_manager):
    ssm = make_adaptive_manager("adaptive-unlock")

    assert ssm.unlock_tool("browse_webpage") is True
    assert ssm.unlocked_tools() == ["browse_webpage"]
    assert ssm.unlock_tool("not_a_real_tool") is False


def test_unlock_tool_persists_across_a_resumed_adaptive_session(make_adaptive_manager):
    ssm = make_adaptive_manager("adaptive-unlock-persists")
    ssm.unlock_tool("capture_screenshot")

    resumed = make_adaptive_manager("adaptive-unlock-persists")

    assert resumed.unlocked_tools() == ["capture_screenshot"]


def test_tools_for_session_honors_adaptive_session_manager_unlocks(make_adaptive_manager):
    from JFI.runner import _tools_for_session

    ssm = make_adaptive_manager("adaptive-tools-for-session")
    assert _names(_tools_for_session(ssm)) == _names(CORE_TOOLS)

    ssm.unlock_tool("browse_webpage")
    assert _names(_tools_for_session(ssm)) == _names(CORE_TOOLS) | {"browse_webpage"}


def test_context_budget_reflects_unlocks_on_adaptive_session_manager(make_adaptive_manager):
    ssm = make_adaptive_manager("adaptive-context-budget")
    before = ssm.context_budget()
    ssm.unlock_tool("browse_webpage")
    after = ssm.context_budget()

    assert after < before


# ---------------------------------------------------------------------------
# deferred_tools_rules — the system-message TOOL ACCESS listing shrinks (and
# eventually disappears) as tools get unlocked, instead of repeating the
# full 13-tool catalog on every turn forever, including turns long after
# everything a session needs has already been unlocked.
# ---------------------------------------------------------------------------

def test_deferred_tools_rules_lists_everything_when_nothing_unlocked():
    rules = deferred_tools_rules(())

    for name in DEFERRED_TOOL_NAMES:
        assert name in rules


def test_deferred_tools_rules_omits_unlocked_tools():
    rules = deferred_tools_rules(["write_file", "execute_command", "read_file"])

    assert "write_file" not in rules
    assert "execute_command" not in rules
    assert "read_file" not in rules
    # Still-locked tools remain listed.
    assert "browse_webpage" in rules
    assert "replace_in_file" in rules


def test_deferred_tools_rules_is_empty_once_everything_is_unlocked():
    rules = deferred_tools_rules(DEFERRED_TOOL_NAMES)

    assert rules == ""


def test_phase_system_message_tool_listing_shrinks_as_the_session_unlocks_tools(manager):
    manager.add_message("user", "goal")

    before = manager.get_messages("imp")[0]["content"]
    assert "browse_webpage" in before

    for name in DEFERRED_TOOL_NAMES:
        manager.unlock_tool(name)

    after = manager.get_messages("imp")[0]["content"]
    assert "TOOL ACCESS" not in after
    assert "browse_webpage" not in after
    assert len(after) < len(before)
