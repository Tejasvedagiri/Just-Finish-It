"""The v2 planner's Laya judge (JFI.planner.judge). Laya itself is faked at
its Router boundary -- CI has no model weights -- but every decision rule
here is the real code: status mapping, the confidence gate, the fallback
rule, and the UNLOAD_LLM_BEFORE_LAYA ordering around LM Studio."""
from __future__ import annotations

import pytest

from JFI.planner import judge as judge_mod
from JFI.planner.judge import (
    BREAKDOWN, CHECKPOINTS, GOOD, REDO, JudgeNode, LayaJudge, build_state, fallback_status,
)


def _answer(choice, confidence, redo="D"):
    return {"answers": {
        "verdict": {"choice": choice, "answer_confidence": confidence},
        "redo_reason": {"choice": redo, "answer_confidence": 0.5},
    }}


class FakeRouter:
    """Scripted per (node description, checkpoint); records every batch."""

    def __init__(self, script, events=None):
        self.script = script
        self.batches = []
        self.events = events

    def predict_batch(self, requests):
        if self.events is not None:
            self.events.append("predict")
        self.batches.append(requests)
        return [self.script[(r["state"]["node"], r["model"])] for r in requests]


def _both(desc, choice, confidence, redo="D", other=("B", 0.9)):
    return {(desc, "english"): _answer(choice, confidence, redo),
            (desc, "typed-decisions"): _answer(*other)}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in ("LAYA_MIN_CONFIDENCE", "LAYA_JUDGE_MODEL", "UNLOAD_LLM_BEFORE_LAYA"):
        monkeypatch.delenv(name, raising=False)


def test_fallback_rule_never_redoes():
    assert fallback_status("architect") == BREAKDOWN
    assert fallback_status("lead") == BREAKDOWN
    assert fallback_status("task") == GOOD
    with pytest.raises(ValueError):
        fallback_status("dev")


def test_one_batch_per_judge_step_covering_both_checkpoints():
    nodes = [JudgeNode(1, "task", "implement a()"), JudgeNode(2, "task", "implement b()")]
    router = FakeRouter({**_both("implement a()", "A", 0.9), **_both("implement b()", "A", 0.9)})
    made = []
    judge = LayaJudge("goal", router_factory=lambda: made.append(1) or router, log=lambda _: None)

    judge.judge(nodes)
    judge.judge(nodes)

    assert len(made) == 1, "the Router is built once per session and kept loaded"
    assert len(router.batches) == 2, "one predict_batch per judge step"
    assert [r["model"] for r in router.batches[0]] == ["english", "typed-decisions"] * 2


def test_status_mapping_and_both_answers_kept():
    node = JudgeNode(7, "lead", "scaffold app/db/session.py")
    router = FakeRouter(_both(node.description, "C", 0.8, redo="A", other=("A", 0.7)))
    [v] = LayaJudge("goal", router_factory=lambda: router, log=lambda _: None).judge([node])

    assert (v.status, v.redo_reason, v.source, v.model) == (REDO, "operational", "laya", "english")
    assert set(v.answers) == set(CHECKPOINTS)
    assert v.answers["typed-decisions"]["verdict"]["choice"] == "A"


def test_low_confidence_uses_the_fallback_but_keeps_layas_answers():
    """Observed on the first real run (laya_plan.md §5.1.1): zero-shot
    confidences were 0.41-0.59, so the gate is what does most judging."""
    node = JudgeNode(3, "architect", "design persistence component")
    router = FakeRouter(_both(node.description, "C", 0.52))
    [v] = LayaJudge("goal", router_factory=lambda: router, log=lambda _: None).judge([node])

    assert (v.status, v.source, v.fallback) == (BREAKDOWN, "fallback", "low_confidence")
    assert v.confidence == 0.52 and v.answers


def test_bad_judge_model_is_a_config_error(monkeypatch):
    monkeypatch.setenv("LAYA_JUDGE_MODEL", "multilingual")
    with pytest.raises(ValueError):
        LayaJudge("goal", router_factory=lambda: None, log=lambda _: None).judge([JudgeNode(1, "task", "x")])


def test_judge_model_selects_the_deciding_checkpoint(monkeypatch):
    monkeypatch.setenv("LAYA_JUDGE_MODEL", "typed-decisions")
    node = JudgeNode(4, "task", "implement c()")
    router = FakeRouter(_both(node.description, "C", 0.9, other=("A", 0.95)))
    [v] = LayaJudge("goal", router_factory=lambda: router, log=lambda _: None).judge([node])
    assert (v.status, v.model) == (GOOD, "typed-decisions")


def test_laya_missing_falls_back_for_the_whole_session():
    logs = []

    def missing():
        raise ImportError("No module named 'laya'")

    judge = LayaJudge("goal", router_factory=missing, log=logs.append)
    nodes = [JudgeNode(1, "architect", "x"), JudgeNode(2, "task", "y")]
    first = judge.judge(nodes)
    judge.judge(nodes)

    assert [(v.status, v.fallback) for v in first] == [(BREAKDOWN, "unavailable"), (GOOD, "unavailable")]
    assert len(logs) == 1, "a missing extra is reported once, not every judge step"
    assert "uv sync --extra laya" in logs[0]


def test_a_failing_prediction_falls_back_without_raising():
    class Boom:
        def predict_batch(self, requests):
            raise RuntimeError("out of memory")

    [v] = LayaJudge("goal", router_factory=Boom, log=lambda _: None).judge([JudgeNode(1, "lead", "x")])
    assert (v.status, v.fallback) == (BREAKDOWN, "unavailable")


def test_state_keeps_the_node_text_when_trimming():
    node = JudgeNode(1, "task", "implement search() in app/search.py", done_when="search('a') -> [a]",
                     path=["p" * 2000])
    state = build_state("g" * 5000, node)
    assert state["node"] == node.description and state["done_when"] == node.done_when
    assert sum(len(v) for v in state.values()) <= judge_mod.STATE_MAX_CHARS


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
    router = FakeRouter(_both(node.description, "A", 0.9), events=events)
    judge = LayaJudge("goal", router_factory=lambda: router, lmstudio=FakeLMStudio(events),
                      log=lambda _: None)

    [v] = judge.judge([node])
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
    assert v.fallback == "unavailable"


def _head_tokens(tok, question):
    """Mirrors laya.common.build_sequence's head layout: the instruction line,
    then one [MASK] + "<key>: <text>" per option, each option capped at 48."""
    instruction = len(tok.encode(f"{question['type']} question: {question['instructions']}",
                                 add_special_tokens=False))
    option_lengths = [len(tok.encode(f" {k}: {v}", add_special_tokens=False))
                      for k, v in question["criteria"].items()]
    return instruction, option_lengths


def test_judge_questions_fit_laya_head_budget():
    """Laya silently truncates an instruction that doesn't fit next to its
    options in head_max_len -- a longer, clearer question would quietly lose
    its end. Checked with the checkpoints' real tokenizers when they're in
    the local Hugging Face cache (skipped otherwise, e.g. in CI)."""
    import glob
    import json
    import os
    transformers = pytest.importorskip("transformers")
    snaps = glob.glob(os.path.expanduser(
        "~/.cache/huggingface/hub/models--convaiinnovations--laya/snapshots/*/"))
    if not snaps:
        pytest.skip("Laya checkpoints aren't in the local Hugging Face cache")
    questions = [*judge_mod.QUESTIONS_BY_LEVEL.values(), judge_mod.REDO_QUESTION]
    for sub in (".", "typed-decisions"):
        cfg_path = os.path.join(snaps[0], sub, "rl_agent_config.json")
        if not os.path.exists(cfg_path):
            continue
        head_max_len = json.load(open(cfg_path))["head_max_len"]
        tok = transformers.AutoTokenizer.from_pretrained(os.path.join(snaps[0], sub, "tokenizer"))
        for question in questions:
            instruction, options = _head_tokens(tok, question)
            assert max(options) <= 48, (sub, question["instructions"][:40], max(options))
            room = head_max_len - sum(1 + n for n in options)
            assert instruction <= room, (sub, question["instructions"][:40], instruction, room)


def test_each_level_gets_its_own_verdict_question():
    nodes = [JudgeNode(1, "architect", "a"), JudgeNode(2, "lead", "b"), JudgeNode(3, "task", "c")]
    router = FakeRouter({**_both("a", "B", 0.9), **_both("b", "B", 0.9), **_both("c", "A", 0.9)})
    LayaJudge("goal", router_factory=lambda: router, log=lambda _: None).judge(nodes)
    sent = {r["state"]["level"]: r["questions"]["verdict"]["instructions"] for r in router.batches[0]}
    assert sent["architect"].startswith("You are a lead")
    assert sent["lead"].startswith("You are a task planner")
    assert sent["task"].startswith("You are a developer")


def test_checkpoint_paths_override_only_the_named_checkpoints(monkeypatch):
    """A fine-tuned judge (laya-finetuning/) is loaded from a local folder;
    the other checkpoint keeps coming from the Hub."""
    monkeypatch.delenv("LAYA_TYPED_DECISIONS_PATH", raising=False)
    monkeypatch.setenv("LAYA_ENGLISH_PATH", "laya-finetuning/checkpoints/jfi-judge-english")
    assert judge_mod._checkpoint_overrides() == {"english": "laya-finetuning/checkpoints/jfi-judge-english"}
    monkeypatch.delenv("LAYA_ENGLISH_PATH")
    assert judge_mod._checkpoint_overrides() == {}
