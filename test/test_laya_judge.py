"""The planner's two-score judge (JFI.planner.judge): the rule and Laya's
sizing answer (can this task be solved within 20k tokens?), with an LLM
tie-break. Laya is faked at its Router boundary and the LLM at
its stream boundary -- CI has neither -- but every decision here is the real
code: status mapping, agree / tie-break / rule-wins, the missing-Laya path and
the UNLOAD_LLM_BEFORE_LAYA ordering around LM Studio."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from JFI.planner import judge as judge_mod
from JFI.planner.judge import (
    AGREE, BREAKDOWN, GOOD, LLM, RULE, RULE_NO_LAYA, JudgeNode, LayaJudge, build_state, fallback_status,
)


def _answer(p_yes, confidence=None):
    """Laya's noul answer: P(yes) and its calibrated confidence."""
    confidence = max(p_yes, 1 - p_yes) if confidence is None else confidence
    return {"answers": {"solvable": {"type": "noul", "noul": p_yes, "answer_confidence": confidence}}}


class FakeRouter:
    """Scripted per node description; records every batch."""

    def __init__(self, script, events=None):
        self.script = script
        self.batches = []
        self.events = events

    def predict_batch(self, requests):
        if self.events is not None:
            self.events.append("predict")
        self.batches.append(requests)
        return [self.script[r["state"]["input"].split("\n")[0].removeprefix("Task: ")] for r in requests]


class FakeLLM:
    """Streams `reply` the way an OpenAI-compatible server does; records prompts."""

    def __init__(self, reply):
        self.reply = reply
        self.prompts = []

    def send_message(self, messages, tools=None):
        self.prompts.append(messages[-1]["content"])
        delta = SimpleNamespace(content=self.reply)
        return [SimpleNamespace(usage=None, choices=[SimpleNamespace(delta=delta)])]


def _judge(router, llm=None, **kw):
    return LayaJudge("a portfolio dashboard", llm=llm, router_factory=lambda: router, log=lambda _: None, **kw)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in ("LAYA_MIN_CONFIDENCE", "UNLOAD_LLM_BEFORE_LAYA", "LAYA", "LAYA_DEVICE"):
        monkeypatch.delenv(name, raising=False)


def test_the_rule_never_redoes():
    assert fallback_status("architect") == BREAKDOWN
    assert fallback_status("lead") == BREAKDOWN
    assert fallback_status("task") == GOOD
    with pytest.raises(ValueError):
        fallback_status("dev")


def test_one_laya_batch_per_judge_step_on_the_english_checkpoint():
    nodes = [JudgeNode(1, "task", "implement a()"), JudgeNode(2, "task", "implement b()")]
    router = FakeRouter({"implement a()": _answer(0.9), "implement b()": _answer(0.9)})
    made = []
    judge = LayaJudge("goal", router_factory=lambda: made.append(1) or router, log=lambda _: None)

    judge.judge(nodes)
    judge.judge(nodes)

    assert len(made) == 1, "the Router is built once per session and kept loaded"
    assert len(router.batches) == 2, "one predict_batch per judge step"
    assert [r["model"] for r in router.batches[0]] == ["english", "english"]
    assert router.batches[0][0]["questions"] == {
        "solvable": {"type": "noul", "instructions": "Can this be solved with 20k tokens?"}}


def test_agreement_decides_without_an_llm_call():
    llm = FakeLLM("1: GOOD")
    router = FakeRouter({"build the news view": _answer(0.3, 0.6)})
    [v] = _judge(router, llm).judge([JudgeNode(1, "architect", "build the news view")])

    assert (v.status, v.decided_by, v.rule_status, v.laya_status) == (BREAKDOWN, AGREE, BREAKDOWN, BREAKDOWN)
    assert llm.prompts == []


def test_a_confident_disagreement_goes_to_the_llm_in_one_call_for_the_whole_step():
    """The user's rule: both agree -> done; Laya at 0.75 or higher disagrees
    -> ask the LLM. Observed on stui run 12: a three-file scaffold node was
    one of several disagreements in the same Architect step -- one call
    settles them all, not one call per node."""
    nodes = [JudgeNode(1, "architect", "Scaffold Vite: package.json, index.html, .gitignore",
                       files=["package.json", "index.html", ".gitignore"]),
             JudgeNode(2, "architect", "theme toggle in src/theme.js"),
             JudgeNode(3, "architect", "overview view")]
    router = FakeRouter({nodes[0].description: _answer(0.95), nodes[1].description: _answer(0.9),
                         nodes[2].description: _answer(0.1)})
    llm = FakeLLM("1: BREAKDOWN\n2: GOOD")

    v1, v2, v3 = _judge(router, llm).judge(nodes)

    assert len(llm.prompts) == 1
    assert "package.json, index.html, .gitignore" in llm.prompts[0] and "choose: BREAKDOWN or GOOD" in llm.prompts[0]
    assert (v1.status, v1.decided_by, v1.tiebreak_status) == (BREAKDOWN, LLM, BREAKDOWN)
    assert (v2.status, v2.decided_by, v2.tiebreak_status) == (GOOD, LLM, GOOD)
    assert (v3.status, v3.decided_by) == (BREAKDOWN, AGREE)


def test_an_unsure_disagreement_keeps_the_rule(monkeypatch):
    llm = FakeLLM("1: GOOD")
    router = FakeRouter({"allocation view": _answer(0.74)})
    [v] = _judge(router, llm).judge([JudgeNode(1, "architect", "allocation view")])

    assert (v.status, v.decided_by, v.laya_status, v.confidence) == (BREAKDOWN, RULE, GOOD, 0.74)
    assert llm.prompts == []
    monkeypatch.setenv("LAYA_MIN_CONFIDENCE", "0.7")
    [v] = _judge(FakeRouter({"allocation view": _answer(0.74)}), llm).judge(
        [JudgeNode(1, "architect", "allocation view")])
    assert v.decided_by == LLM


def test_laya_says_good_from_p_yes_0_4():
    """The cut-off from the POC (docs/laya_poc.md): at 0.4 Laya gets 88% of the
    nodes that had to be broken down right; 0.5 gave 67% overall."""
    node = JudgeNode(1, "architect", "settings view")
    [below] = _judge(FakeRouter({node.description: _answer(0.39)})).judge([node])
    [at] = _judge(FakeRouter({node.description: _answer(0.40)})).judge([node])
    assert (below.laya_status, below.p_yes) == (BREAKDOWN, 0.39)
    assert at.laya_status == GOOD and at.redo_reason is None, "Laya answers GOOD or BREAKDOWN, never REDO"


def test_an_unreadable_or_missing_tie_break_keeps_the_rule():
    node = JudgeNode(1, "architect", "settings view")
    for llm in (FakeLLM("I think it's fine"), FakeLLM("1: REDO"), None):  # REDO wasn't one of the two options
        [v] = _judge(FakeRouter({node.description: _answer(0.9)}), llm).judge([node])
        assert (v.status, v.decided_by, v.tiebreak_status) == (BREAKDOWN, RULE, None)


def test_laya_missing_uses_the_rule_for_the_whole_session():
    logs = []

    def missing():
        raise ImportError("No module named 'laya'")

    judge = LayaJudge("goal", router_factory=missing, log=logs.append)
    nodes = [JudgeNode(1, "architect", "x"), JudgeNode(2, "task", "y")]
    first = judge.judge(nodes)
    judge.judge(nodes)

    assert [(v.status, v.decided_by) for v in first] == [(BREAKDOWN, RULE_NO_LAYA), (GOOD, RULE_NO_LAYA)]
    assert len(logs) == 1, "a missing extra is reported once, not every judge step"
    assert "uv sync --extra laya" in logs[0]


def test_a_failing_prediction_uses_the_rule_without_raising():
    class Boom:
        def predict_batch(self, requests):
            raise RuntimeError("out of memory")

    [v] = LayaJudge("goal", router_factory=Boom, log=lambda _: None).judge([JudgeNode(1, "lead", "x")])
    assert (v.status, v.decided_by) == (BREAKDOWN, RULE_NO_LAYA)


def test_laya_reads_the_task_and_its_description_not_the_plans_labels():
    """POC (docs/laya_poc.md): the old state -- goal, level, path, node,
    done_when, files -- gave Laya labels it can't interpret (AUC 0.59); the
    task and its description with the sizing question as the goal gave 0.76."""
    node = JudgeNode(1, "task", "implement search() in app/search.py", done_when="search('a') -> [a]",
                     files=["app/search.py"], path=["the search component"],
                     notes="Case-insensitive substring match over titles; empty query returns [].")
    assert build_state(node) == {
        "input": "Task: implement search() in app/search.py\n"
                 "Description: Case-insensitive substring match over titles; empty query returns [].",
        "goal": "Can this be solved with 20k tokens?"}
    assert build_state(JudgeNode(2, "task", "x"))["input"] == "Task: x"
    long = build_state(JudgeNode(3, "task", "y", notes="z" * 10_000))["input"]
    assert len(long) == judge_mod.INPUT_MAX_CHARS and long.startswith("Task: y\nDescription: zzz")


class FakeLMStudio:
    def __init__(self, events):
        self.events = events

    def models_unloaded(self, log):
        events = self.events

        class _Ctx:
            def __enter__(self):
                events.append("unload")

            def __exit__(self, *exc):
                events.append("reload")
                return False

        return _Ctx()


def test_unload_flag_wraps_prediction_in_lmstudio_unload_and_reload(monkeypatch):
    monkeypatch.setenv("UNLOAD_LLM_BEFORE_LAYA", "true")
    events = []
    node = JudgeNode(1, "task", "implement a()")
    router = FakeRouter({node.description: _answer(0.9)}, events=events)

    [v] = _judge(router, lmstudio=FakeLMStudio(events)).judge([node])
    assert events == ["unload", "predict", "reload"]
    assert v.status == GOOD


def test_unload_flag_reloads_even_when_laya_fails(monkeypatch):
    monkeypatch.setenv("UNLOAD_LLM_BEFORE_LAYA", "1")
    events = []

    class Boom:
        def predict_batch(self, requests):
            events.append("predict")
            raise RuntimeError("crash")

    judge = LayaJudge("goal", router_factory=Boom, lmstudio=FakeLMStudio(events), log=lambda _: None)
    [v] = judge.judge([JudgeNode(1, "architect", "x")])
    assert events == ["unload", "predict", "reload"]
    assert v.decided_by == RULE_NO_LAYA


def test_the_sizing_question_fits_layas_head_budget():
    """Laya silently truncates a question that doesn't fit its head_max_len.
    Checked with the English checkpoint's real tokenizer when it's in the
    local Hugging Face cache (skipped otherwise, e.g. in CI)."""
    import glob
    import json
    import os
    transformers = pytest.importorskip("transformers")
    snaps = glob.glob(os.path.expanduser(
        "~/.cache/huggingface/hub/models--convaiinnovations--laya/snapshots/*/"))
    if not snaps or not os.path.exists(os.path.join(snaps[0], "rl_agent_config.json")):
        pytest.skip("Laya's English checkpoint isn't in the local Hugging Face cache")
    head_max_len = json.load(open(os.path.join(snaps[0], "rl_agent_config.json")))["head_max_len"]
    tok = transformers.AutoTokenizer.from_pretrained(os.path.join(snaps[0], "tokenizer"))
    for question in judge_mod.QUESTIONS.values():
        used = len(tok.encode(f"{question['type']} question: {question['instructions']}", add_special_tokens=False))
        assert used + 1 <= head_max_len, (question["instructions"], used, head_max_len)


def test_every_level_gets_the_same_sizing_question():
    nodes = [JudgeNode(1, "architect", "a"), JudgeNode(2, "lead", "b"), JudgeNode(3, "task", "c")]
    router = FakeRouter({"a": _answer(0.1), "b": _answer(0.2), "c": _answer(0.8)})
    verdicts = _judge(router).judge(nodes)
    assert len({str(r["questions"]) for r in router.batches[0]}) == 1
    assert [(v.status, v.decided_by) for v in verdicts] == [(BREAKDOWN, AGREE), (BREAKDOWN, AGREE), (GOOD, AGREE)]


def test_laya_runs_only_when_the_flag_is_set(monkeypatch):
    """The user: "if .env LAYA=1 then run it through Laya also, else leave it
    only to the judge" -- the rule alone by default."""
    from JFI.planner.judge import FallbackJudge, laya_device, make_judge
    assert isinstance(make_judge("g", log=lambda m: None), FallbackJudge)
    monkeypatch.setenv("LAYA", "0")
    assert isinstance(make_judge("g", log=lambda m: None), FallbackJudge)
    monkeypatch.setenv("LAYA", "1")
    logged = []
    assert isinstance(make_judge("g", log=logged.append), LayaJudge)
    assert "cpu" in logged[0]  # CPU by default: the LLM usually holds the GPU
    monkeypatch.setenv("LAYA_DEVICE", "cuda")
    assert laya_device() == "cuda"
    monkeypatch.setenv("LAYA_DEVICE", "tpu")
    with pytest.raises(ValueError):
        laya_device()
