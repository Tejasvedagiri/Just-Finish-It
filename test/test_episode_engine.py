"""Phase 3 of the v2 rewrite: the episode engine (laya_plan.md §0, G1, G5,
G7, G14, G15). Real SQLite; the LLM is scripted at the console boundary
(print_agent_response), the same way test_tiered_planner.py drives run_phase."""
from __future__ import annotations

import json

import pytest

from JFI.episode import engine as engine_mod
from JFI.episode.brief import ScopeAnchor, build_system_message
from JFI.episode.budget import episode_token_budget, estimate_tokens
from JFI.episode.directives import add_directive
from JFI.episode.engine import make_finish, run_episode
from JFI.episode.roles import OPTIONAL_POOL, ROLE_CORE_TOOLS, ROLE_ENV_PREFIXES, ROLES
from JFI.episode.tools import EpisodeTools
from JFI.llm.base_llm_stream import phase_env
from JFI.manager.abstract_manager import AbstractManager
from JFI.models import Episode, SessionRecord, get_engine, get_session
from JFI.session.history_store import load_episode_messages


class Console(AbstractManager):
    def __init__(self, responses, stop_after=None):
        self.responses = list(responses)
        self.turns = 0
        self.stop_after = stop_after

    def should_stop(self):
        return self.stop_after is not None and self.turns >= self.stop_after

    def wait_while_paused(self):
        pass

    def print_agent_response(self, response, prompt_tokens_estimate=0):
        self.turns += 1
        item = self.responses.pop(0) if self.responses else {"content": "thinking", "tool_calls": None}
        return item() if callable(item) else item

    def display_tool_call(self, *a, **k):
        pass

    def display_tool_result(self, *a, **k):
        pass

    def display_error(self, *a, **k):
        pass

    def display_system(self, *a, **k):
        pass

    def display_rule(self, *a, **k):
        pass

    def display_user(self, *a, **k):
        pass

    def display_assistant(self, *a, **k):
        pass

    def get_user_input(self, *a, **k):
        return ""

    def get_user_choice(self, *a, **k):
        return "s"

    def set_status(self, **k):
        pass


class LLM:
    def __init__(self):
        self.requests = []

    def send_message(self, messages, tools=None):
        self.requests.append({"messages": [dict(m) for m in messages], "tools": tools})
        return object()


def call(name, args, cid="c1"):
    return {"id": cid, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


def turn(*calls, content=None, usage=None):
    return {"content": content, "tool_calls": list(calls) or None, "usage": usage}


@pytest.fixture
def engine(tmp_path):
    engine = get_engine(tmp_path)
    with get_session(engine) as db:
        db.add(SessionRecord(session_id="s", repo_path="."))
        db.commit()
    return engine


def _anchor(node_id=7, role="lead"):
    return ScopeAnchor(role=role, node_id=node_id, node="scaffold app/db/session.py: session handling",
                       finish=f"finish({node_id}, summary)", done_when="stubs exist", files=["app/db/session.py"],
                       path=["persistence"])


def _tools(anchor, role="lead", extra=None):
    impl = {"finish": make_finish(anchor), "get_node": lambda node_id: f"node {node_id}",
            "design_get": lambda kind=None, key=None: "Design: empty.",
            "context_lookup": lambda keyword=None: "nothing saved", "execute_command": lambda command: "ok"}
    impl.update(extra or {})
    return EpisodeTools(role, impl)


def _run(engine, console, llm, anchor=None, budget=20_000, max_turns=None, role="lead", tools=None):
    anchor = anchor or _anchor(role=role)
    tools = tools or _tools(anchor, role)
    system = build_system_message(anchor, "You are the Lead.", ["Runbook: empty.", "Design: empty."])
    return run_episode(llm, console, engine, "s", role=role, mode="breakdown", anchor=anchor,
                       system_message=system, tools=tools, budget=budget, max_turns=max_turns)


def test_finish_ends_the_episode_and_records_it(engine):
    llm = LLM()
    console = Console([turn(call("get_node", {"node_id": 7})),
                       turn(call("finish", {"node_id": 7, "summary": "two files scaffolded"}))])
    result = _run(engine, console, llm)
    assert (result.end_reason, result.turns, result.summary) == ("finish", 2, "two files scaffolded")
    assert result.tools_used == ["get_node", "finish"]
    with get_session(engine) as db:
        row = db.get(Episode, result.episode_id)
    assert (row.role, row.mode, row.node_id, row.end_reason, row.turns) == ("lead", "breakdown", 7, "finish", 2)
    assert row.ended_at is not None


def test_the_scope_anchor_is_in_every_request(engine):
    llm = LLM()
    console = Console([turn(call("get_node", {"node_id": 7}, "a")), turn(call("get_node", {"node_id": 7}, "b")),
                       turn(call("finish", {"node_id": 7, "summary": "done"}))])
    _run(engine, console, llm)
    assert len(llm.requests) == 3
    for request in llm.requests:
        system = request["messages"][0]
        assert system["role"] == "system" and system["content"].startswith("SCOPE (fixed")
        assert "[id=7] scaffold app/db/session.py" in system["content"]
        assert "must not:" in system["content"] and "finish:    finish(7, summary)" in system["content"]


def test_finish_for_another_node_is_refused(engine):
    console = Console([turn(call("finish", {"node_id": 99, "summary": "x"})),
                       turn(call("finish", {"node_id": 7, "summary": "ok"}))])
    result = _run(engine, console, LLM())
    assert (result.end_reason, result.turns) == ("finish", 2)


def test_episodes_are_isolated_from_each_other(engine):
    first = _run(engine, Console([turn(call("finish", {"node_id": 7, "summary": "first"}), content="SECRET-1")]),
                 LLM())
    llm = LLM()
    second = _run(engine, Console([turn(call("finish", {"node_id": 8, "summary": "second"}))]), llm,
                  anchor=_anchor(node_id=8))
    sent = json.dumps(llm.requests[0]["messages"])
    assert "SECRET-1" not in sent
    assert all("SECRET-1" not in json.dumps(m) for m in load_episode_messages(engine, "s", second.episode_id))
    assert any("SECRET-1" in json.dumps(m) for m in load_episode_messages(engine, "s", first.episode_id))


def test_turn_cap(engine):
    result = _run(engine, Console([]), LLM(), max_turns=3)
    assert (result.end_reason, result.turns) == ("turn_cap", 3)


def test_budget_stops_before_a_request_that_would_not_fit(engine):
    llm = LLM()
    result = _run(engine, Console([]), llm, budget=50)
    assert result.end_reason == "budget"
    assert llm.requests == [], "nothing is sent once the request is already over budget"


def test_server_reported_usage_counts_toward_the_episode(engine):
    usage = {"prompt_tokens": 17_000, "completion_tokens": 1_000}
    result = _run(engine, Console([turn(call("finish", {"node_id": 7, "summary": "x"}), usage=usage)]), LLM())
    assert result.tokens == 18_000


def test_no_tool_call_gets_a_nudge_naming_the_finish_tool(engine):
    llm = LLM()
    _run(engine, Console([turn(content="let me think"), turn(call("finish", {"node_id": 7, "summary": "x"}))]), llm)
    nudge = llm.requests[1]["messages"][-1]
    assert nudge["role"] == "user" and "call finish(7, summary)" in nudge["content"]


def test_stop_ends_the_episode(engine):
    result = _run(engine, Console([turn(call("get_node", {"node_id": 7}))] * 5, stop_after=1), LLM())
    assert result.end_reason == "stopped"


def test_llm_failures_end_with_error(engine, monkeypatch):
    monkeypatch.setattr(engine_mod, "LLM_RETRY_DELAY_SECONDS", 0)

    class Broken(LLM):
        def send_message(self, messages, tools=None):
            raise RuntimeError("connection refused")

    assert _run(engine, Console([]), Broken()).end_reason == "error"


# ------------------------------------------------------------------ tools

def test_core_tools_only_plus_load_tool_for_the_pool(engine):
    anchor = _anchor()
    tools = _tools(anchor)
    names = [t["function"]["name"] for t in tools.schemas()]
    assert "context_lookup" not in names and "load_tool" in names
    assert set(names) - {"load_tool"} <= set(ROLE_CORE_TOOLS["lead"])


def test_load_tool_adds_a_pool_tool_for_this_episode_only(engine):
    llm = LLM()
    anchor = _anchor()
    console = Console([turn(call("context_lookup", {}, "a")),
                       turn(call("load_tool", {"name": "context_lookup"}, "b")),
                       turn(call("context_lookup", {}, "c")),
                       turn(call("load_tool", {"name": "execute_command"}, "d")),
                       turn(call("finish", {"node_id": 7, "summary": "x"}, "e"))])
    result = _run(engine, console, llm, anchor=anchor)
    tool_results = [m["content"] for m in load_episode_messages(engine, "s", result.episode_id) if m["role"] == "tool"]
    assert tool_results[0].startswith("Error") and "load_tool('context_lookup')" in tool_results[0]
    assert tool_results[2] == "nothing saved"
    assert tool_results[3].startswith("Error"), "execute_command isn't in lead's pool"
    names_before = [t["function"]["name"] for t in llm.requests[1]["tools"]]
    names_after = [t["function"]["name"] for t in llm.requests[2]["tools"]]
    assert "context_lookup" not in names_before and "context_lookup" in names_after
    assert result.tools_loaded == ["context_lookup"]
    fresh = _tools(anchor)
    assert "context_lookup" not in fresh.active, "a load lasts one episode"


def test_picks_only_add_pool_tools(engine):
    anchor = _anchor()
    tools = EpisodeTools("lead", {"finish": make_finish(anchor), "context_lookup": lambda keyword=None: "x",
                                  "execute_command": lambda command: "ok"},
                         picked=["context_lookup", "execute_command"])
    assert "context_lookup" in tools.active
    assert "execute_command" not in tools.active, "a pick outside the pool is ignored"
    assert "finish" in tools.active


def test_role_tool_sets_reference_known_roles_and_a_finish_tool():
    assert set(ROLE_CORE_TOOLS) == set(ROLES) == set(ROLE_ENV_PREFIXES)
    for role, names in ROLE_CORE_TOOLS.items():
        assert ("mark_leaf_done" if role == "dev" else "finish") in names
        assert not set(names) & set(OPTIONAL_POOL) - {"start_background_process", "stop_background_process"}


def test_core_tool_schemas_stay_small():
    """G1: v1 sessions could end up sending every unlocked schema -- ~5,900
    tokens for the 32 deferred tools. A role's core set must stay well under
    that; ROLE_OVERHEAD_MAX_TOKENS adds the role prompt in phase 5."""
    impl = {name: (lambda **k: "") for names in ROLE_CORE_TOOLS.values() for name in names}
    for role in ROLES:
        schemas = EpisodeTools(role, impl).schemas()
        assert estimate_tokens([], schemas) <= 3_000, role


# ------------------------------------------------------------------ directives

def test_directive_reaches_its_node_once(engine):
    add_directive(engine, "s", "use httpx, not requests", node_id=7)
    add_directive(engine, "s", "only for node 8", node_id=8)
    llm = LLM()
    _run(engine, Console([turn(call("finish", {"node_id": 7, "summary": "x"}))]), llm)
    kickoff = llm.requests[0]["messages"][1]["content"]
    assert "use httpx, not requests" in kickoff and "only for node 8" not in kickoff
    llm2 = LLM()
    _run(engine, Console([turn(call("finish", {"node_id": 7, "summary": "x"}))]), llm2)
    assert "use httpx" not in llm2.requests[0]["messages"][1]["content"], "delivered exactly once"


def test_untargeted_directive_goes_to_the_next_episode(engine):
    add_directive(engine, "s", "prefer small functions")
    llm = LLM()
    _run(engine, Console([turn(call("finish", {"node_id": 8, "summary": "x"}))]), llm, anchor=_anchor(node_id=8))
    assert "prefer small functions" in llm.requests[0]["messages"][1]["content"]


# ------------------------------------------------------------------ budget + models

def test_budget_is_capped_by_the_models_window(monkeypatch):
    monkeypatch.delenv("EPISODE_TOKEN_BUDGET", raising=False)
    monkeypatch.setenv("CONTEXT_SIZE", "40000")
    assert episode_token_budget() == 20_000
    monkeypatch.setenv("CONTEXT_SIZE", "16000")
    assert episode_token_budget() == int(16000 * 0.7)
    monkeypatch.setenv("LEAD_CONTEXT_SIZE", "8000")
    assert episode_token_budget(ROLE_ENV_PREFIXES["lead"]) == int(8000 * 0.7)


def test_role_model_settings_fall_back_through_the_chain(monkeypatch):
    for var in ("ARCHITECT_MODEL", "PLANNER_MODEL", "MODEL"):
        monkeypatch.delenv(var, raising=False)
    chain = ROLE_ENV_PREFIXES["architect"]
    monkeypatch.setenv("MODEL", "shared")
    assert phase_env(chain, "MODEL") == "shared"
    monkeypatch.setenv("PLANNER_MODEL", "planner")
    assert phase_env(chain, "MODEL") == "planner"
    monkeypatch.setenv("ARCHITECT_MODEL", "architect")
    assert phase_env(chain, "MODEL") == "architect"
    assert phase_env("PLANNER", "MODEL") == "planner", "single-prefix callers are unchanged"
