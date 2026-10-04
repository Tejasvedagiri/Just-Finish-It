"""PARALLEL_LLM: Lead and Task breakdowns of one level run side by side, but
only as many at once as the role's model server serves (JFI.llm.parallel).
Real SQLite and real node tools; the LLM answers per node, since parallel
episodes can't share one ordered script."""
from __future__ import annotations

import json
import re
import subprocess
import threading
import time
from types import SimpleNamespace

import pytest

from JFI.llm import parallel
from JFI.llm.lmstudio_control import LMStudioControl
from JFI.models import HistoryMessage, SessionRecord, get_engine, get_session
from JFI.planner import loop
from JFI.planner.judge import BREAKDOWN, GOOD, FallbackJudge
from JFI.planner.loop import Planner
from JFI.planner.nodes import add_node, load_nodes
from sqlmodel import select

from test.test_planner import Console

PS_JSON = [{"type": "llm", "modelKey": "qwen/qwen3.8-27b", "identifier": "qwen/qwen3.8-27b",
            "contextLength": 40192, "parallel": 4}]


def _lms(ps=PS_JSON):
    return LMStudioControl(lms_path="lms", runner=lambda args: subprocess.CompletedProcess(args, 0, json.dumps(ps), ""))


def _llm(prefix="LEAD", model="qwen/qwen3.8-27b"):
    return SimpleNamespace(prefix=(prefix, "PLANNER"), model=model)


@pytest.fixture
def engine(tmp_path):
    engine = get_engine(tmp_path / ".jfi")
    with get_session(engine) as db:
        db.add(SessionRecord(session_id="s", repo_path=str(tmp_path), pipeline_version="v2"))
        db.commit()
    return engine


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in ("PARALLEL_LLM", "LLM_BACKEND", "OPENAI_URL", "LEAD_LLM_BACKEND", "PLANNER_LLM_BACKEND",
                 "CONTEXT_SIZE", "LEAD_CONTEXT_SIZE", "PLANNER_CONTEXT_SIZE"):
        monkeypatch.delenv(name, raising=False)


# ---------------------------------------------------------------- the setting

@pytest.mark.parametrize("value, expected", [(None, 1), ("", 1), ("abc", 1), ("0", 1), ("4", 4), ("10", 10),
                                             ("25", 10)])
def test_parallel_llm_is_one_to_ten(monkeypatch, value, expected):
    if value is not None:
        monkeypatch.setenv("PARALLEL_LLM", value)
    assert parallel.requested_parallel() == expected


def test_off_never_probes_the_server(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("probed")
    monkeypatch.setattr(parallel, "server_parallel", boom)
    assert parallel.effective_parallel(_llm())[0] == 1


def test_lm_studio_caps_it_at_the_loaded_models_parallel(monkeypatch):
    monkeypatch.setenv("CONTEXT_SIZE", "8192")
    monkeypatch.setenv("PARALLEL_LLM", "8")
    monkeypatch.setenv("LLM_BACKEND", "lmstudio")
    workers, why = parallel.effective_parallel(_llm(), _lms())
    assert workers == 4 and "parallel=4" in why
    monkeypatch.setenv("PARALLEL_LLM", "2")
    assert parallel.effective_parallel(_llm(), _lms())[0] == 2


def test_lm_studio_runs_only_as_many_as_its_context_holds_whole(monkeypatch):
    """Observed on LM Studio (qwen3.8-27b, -c 40960 --parallel 4): the slots
    share one context. One or two 11.5k-token requests ran; four at once
    failed with "Context size has been exceeded". Each episode may grow to
    CONTEXT_SIZE, so four episodes need four of them."""
    monkeypatch.setenv("PARALLEL_LLM", "4")
    monkeypatch.setenv("LLM_BACKEND", "lmstudio")
    loaded = [{**PS_JSON[0], "contextLength": 40960}]
    monkeypatch.setenv("CONTEXT_SIZE", "40960")
    workers, why = parallel.effective_parallel(_llm(), _lms(loaded))
    assert workers == 1 and "context of 163840" in why
    monkeypatch.setenv("CONTEXT_SIZE", "20480")
    assert parallel.effective_parallel(_llm(), _lms(loaded))[0] == 2
    monkeypatch.setenv("CONTEXT_SIZE", "10240")
    assert parallel.effective_parallel(_llm(), _lms(loaded))[0] == 4


def test_lm_studio_with_the_model_not_loaded_runs_one_at_a_time(monkeypatch):
    """LM Studio loads a model on its first request; until then there's no
    parallel setting to read, and asking for more would only queue."""
    monkeypatch.setenv("PARALLEL_LLM", "4")
    monkeypatch.setenv("LLM_BACKEND", "lmstudio")
    workers, why = parallel.effective_parallel(_llm(model="other/model"), _lms())
    assert workers == 1 and "isn't loaded" in why
    assert parallel.effective_parallel(_llm(), _lms(ps=[{**PS_JSON[0], "parallel": None}]))[0] == 1
    assert parallel.effective_parallel(_llm(), LMStudioControl(lms_path=None, runner=None))[0] == 1 \
        if not LMStudioControl().available() else True


def test_the_roles_own_backend_decides(monkeypatch):
    monkeypatch.setenv("PARALLEL_LLM", "3")
    monkeypatch.setenv("LLM_BACKEND", "ollama")
    monkeypatch.setenv("LEAD_LLM_BACKEND", "anthropic")
    assert parallel.effective_parallel(_llm("LEAD"))[0] == 3
    assert parallel.effective_parallel(_llm("TASK"))[0] == 1


def test_hosted_and_local_openai_compatible_servers(monkeypatch):
    monkeypatch.setenv("PARALLEL_LLM", "5")
    monkeypatch.setattr(parallel, "_llamacpp_slots", lambda url, prefix: None)
    monkeypatch.setenv("OPENAI_URL", "https://api.openai.com/v1")
    assert parallel.effective_parallel(_llm())[0] == 5
    for url in ("http://127.0.0.1:8000/v1", "http://192.168.1.20:8000/v1", "http://gpu-box:8000/v1"):
        monkeypatch.setenv("OPENAI_URL", url)
        assert parallel.effective_parallel(_llm())[0] == 1, url
    monkeypatch.setattr(parallel, "_llamacpp_slots", lambda url, prefix: (2, "2 slots"))
    assert parallel.effective_parallel(_llm())[0] == 2


def test_llama_cpp_slots_are_cut_to_what_its_context_holds(monkeypatch):
    monkeypatch.setenv("CONTEXT_SIZE", "32768")
    props = {"total_slots": 4, "default_generation_settings": {"n_ctx": 65536}}
    monkeypatch.setattr(parallel.httpx, "get", lambda url, timeout: SimpleNamespace(
        status_code=200, json=lambda: props))
    assert parallel._llamacpp_slots("http://127.0.0.1:8080/v1")[0] == 2


# ---------------------------------------------------------------- the planner

class PerNodeLLM:
    """Each Lead adds one single-function node under its component (GOOD by
    the rule), then finishes. Records how many requests overlap."""

    def __init__(self, shared_file=False):
        self.shared_file = shared_file
        self.active = self.peak = 0
        self.turns = {}
        self.lock = threading.Lock()

    def send_message(self, messages, tools=None):
        with self.lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
        time.sleep(0.15)
        with self.lock:
            self.active -= 1
        node_id = int(re.search(r"finish\((\d+), summary\)", messages[1]["content"]).group(1))
        n = self.turns.get(node_id, 0)
        self.turns[node_id] = n + 1
        path = "src/shared.py" if self.shared_file else f"src/mod{node_id}.py"
        args = {"description": f"implement f{node_id}(x) in {path}: return x", "files": [path],
                "done_when": f"f{node_id}(1) == 1"} if n == 0 else {"node_id": node_id, "summary": "done"}
        name = "add_node" if n == 0 else "finish"
        return [SimpleNamespace(choices=[], turn={"content": None, "usage": None, "tool_calls": [
            {"id": f"c{node_id}-{n}", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]})]


class StreamConsole(Console):
    def __init__(self):
        super().__init__([])
        self.parallel = []

    def set_status(self, **k):
        if "parallel" in k:
            self.parallel.append(k["parallel"])

    def print_agent_response(self, response, prompt_tokens_estimate=0):
        return next(iter(response)).turn


def _components(engine, count):
    for i in range(count):
        add_node(engine, "s", "architect", None, f"build component {i}", done_when="works")
    with get_session(engine) as db:
        for node in load_nodes(engine, "s"):
            node.plan_status = BREAKDOWN
            db.add(node)
        db.commit()


def _run(engine, tmp_path, llm, monkeypatch, workers, console=None):
    monkeypatch.setattr(loop, "effective_parallel", lambda _llm: (workers, "test"))
    return Planner(console or StreamConsole(), engine, "s", "goal", tmp_path, lambda role: llm, FallbackJudge()).run()


def test_lead_breakdowns_run_side_by_side(engine, tmp_path, monkeypatch):
    _components(engine, 4)
    llm = PerNodeLLM()
    result = _run(engine, tmp_path, llm, monkeypatch, workers=3)
    assert result.complete
    assert llm.peak == 3
    nodes = load_nodes(engine, "s")
    assert all(n.plan_status == GOOD for n in nodes)
    assert sorted(n.parent_id for n in nodes if n.level == "lead") == [1, 2, 3, 4]
    with get_session(engine) as db:
        seqs = [m.seq for m in db.exec(select(HistoryMessage).where(HistoryMessage.session_id == "s"))]
    assert len(seqs) == len(set(seqs)), "two parallel episodes took the same history seq"


def test_dashboards_see_every_running_episode_then_none(engine, tmp_path, monkeypatch):
    """The status bar's one `task` can only name one of the side-by-side
    episodes; the snapshot's `parallel` lists them all for jfi-web and the
    fleet dashboard, and is cleared when the batch ends."""
    _components(engine, 3)
    console = StreamConsole()
    _run(engine, tmp_path, PerNodeLLM(), monkeypatch, workers=3, console=console)
    busiest = max(console.parallel, key=lambda p: len(p.get("running", [])))
    assert busiest["role"] == "lead" and busiest["workers"] == 3
    assert sorted(r["node_id"] for r in busiest["running"]) == [1, 2, 3]
    assert busiest["running"][0]["task"].startswith("build component")
    assert console.parallel[-1] == {}


def test_one_at_a_time_when_the_server_serves_one(engine, tmp_path, monkeypatch):
    _components(engine, 3)
    llm = PerNodeLLM()
    assert _run(engine, tmp_path, llm, monkeypatch, workers=1).complete
    assert llm.peak == 1


def test_two_parallel_leads_cannot_both_claim_one_file(engine, tmp_path, monkeypatch):
    """The file-owner check reads the tree, then writes: without a lock across
    episodes both Leads would pass it and own src/shared.py twice. The check
    is slowed down so the window is wide enough to hit every time."""
    from JFI.planner import nodes
    real_owner = nodes._file_owner

    def slow_owner(*args):
        owner = real_owner(*args)
        time.sleep(0.1)
        return owner
    monkeypatch.setattr(nodes, "_file_owner", slow_owner)
    _components(engine, 4)
    _run(engine, tmp_path, PerNodeLLM(shared_file=True), monkeypatch, workers=4)
    owners = [n for n in load_nodes(engine, "s") if n.level == "lead" and "src/shared.py" in (n.files or [])]
    assert len(owners) == 1


def test_a_runaway_reply_is_cut_off_while_it_is_collected(monkeypatch):
    """Parallel replies are collected before they're drawn; the console's own
    cap only runs on the replay, so without this a model stuck in a loop
    would be collected forever."""
    from JFI.episode.parallel_console import _collect
    monkeypatch.setenv("REASONING_OUTPUT_CAP", "100")

    def endless():
        while True:
            yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(
                reasoning_content="think " * 10, content=None, tool_calls=None))])
    chunks = _collect(endless())
    assert 0 < len(chunks) < 20


def _parallel_panel_app():
    import time as _time

    from JFI.web.dashboard import _render_parallel
    _render_parallel({"role": "lead", "workers": 2, "why": "LM Studio serves m with parallel=4",
                      "running": [{"node_id": 3, "task": "build the nav", "started_at": _time.time() - 42},
                                  {"node_id": 4, "task": "build the footer", "started_at": _time.time() - 5}]})
    _render_parallel({})
    _render_parallel(None)


def test_jfi_web_lists_the_side_by_side_episodes():
    pytest.importorskip("streamlit")
    from streamlit.testing.v1 import AppTest
    app = AppTest.from_function(_parallel_panel_app).run()
    assert not app.exception
    assert [e.label for e in app.expander] == ["Lead episodes in parallel: 2 of 2"]
    captions = [c.value for c in app.caption]
    assert captions[0] == "LM Studio serves m with parallel=4"
    assert captions[1].startswith("**node 3** · build the nav · running 4")
    assert captions[2].startswith("**node 4** · build the footer")
