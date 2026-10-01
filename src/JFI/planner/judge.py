"""The planner's judge: gives every plan node a status -- GOOD or BREAKDOWN --
from two scores (laya_plan.md §5). REDO comes from elsewhere (an escalation,
the redo cap), never from the judge:

- **the rule** (`fallback_status`): deterministic, no model -- components
  and files break down, function-level nodes are accepted, never REDO;
- **Laya**: its published `english` checkpoint, asked one yes/no question per
  node -- can this task be solved within 20k tokens? -- given only the task
  and its description (the node's notes). P(yes) >= LAYA_YES_THRESHOLD is
  GOOD, otherwise BREAKDOWN.

They agree: that's the verdict. They disagree and Laya is confident
(answer_confidence >= LAYA_MIN_CONFIDENCE, default 0.75): one short LLM call
breaks the tie between the two answers. They disagree and Laya isn't
confident: the rule decides. Every score is kept on the Verdict and stored as
a PlannerVerdict row, so the dashboard shows them side by side.

Why this question (benchmark/laya_poc.py, docs/laya_poc.md): the first state
-- goal, level, path, node, done_when, files, "is this clean enough?" A/B/C --
gave Laya labels it can't interpret, its confidence never passed 0.55, and it
ranked nodes barely better than chance (AUC 0.59). Asked whether the task and
its description can be solved within 20k tokens, it separates the nodes Dev
finished in one episode from the ones that had to be broken down (AUC 0.76
over 67 nodes from 4 runs; median P(yes) 0.56 vs 0.18). `multilingual` stayed
at 0.92-0.999 on everything; the budget number itself barely mattered.

Why not a fine-tuned Laya: fine-tunes on this repo's plan nodes scored well on
their own held-out data but overfitted to the node's level -- on a live run
(stui run 14) the checkpoint answered BREAKDOWN at 0.98 for every Architect
and Lead node and GOOD at 0.98 for every Task node, whatever the text said.

Laya is loaded once per session on the first judge step and kept loaded
(D21). UNLOAD_LLM_BEFORE_LAYA=1 swaps LM Studio's models out around each
judge step (JFI.llm.lmstudio_control); Laya then runs in a child process,
since only a process exit returns its memory to the OS.
"""

import multiprocessing as mp
import os
import re
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence

GOOD, BREAKDOWN, REDO = "GOOD", "BREAKDOWN", "REDO"
LEVELS = ("architect", "lead", "task")
CHECKPOINT = "english"
SIZING_QUESTION = "Can this be solved with 20k tokens?"
# P(yes) at or above this is GOOD. On the POC's 67 nodes (20k): 0.4 gives 73%
# accuracy and gets 88% of the broken-down nodes right -- wrongly skipping a
# breakdown costs more than an extra split. 0.5 gave 67%.
LAYA_YES_THRESHOLD = 0.4
QUESTIONS = {"solvable": {"type": "noul", "instructions": SIZING_QUESTION}}
GOAL_MAX_CHARS = 300  # the goal as the LLM tie-break sees it
INPUT_MAX_CHARS = 2000  # the task + description; English Laya reads 512 tokens
CHILD_TIMEOUT_SECONDS = 900  # includes loading the checkpoint from disk

# How the final status was reached, stored as PlannerVerdict.decided_by.
AGREE, RULE, LLM, RULE_NO_LAYA = "agree", "rule", "llm", "rule (Laya unavailable)"


@dataclass
class JudgeNode:
    node_id: int
    level: str
    description: str
    done_when: str = ""
    files: Sequence[str] = ()
    path: Sequence[str] = ()  # ancestor descriptions, root first
    notes: str = ""  # what to implement and how: Laya reads it as the task's description


@dataclass
class Verdict:
    node_id: int
    status: str
    decided_by: str
    rule_status: str
    laya_status: Optional[str] = None
    confidence: Optional[float] = None
    tiebreak_status: Optional[str] = None
    redo_reason: Optional[str] = None
    p_yes: Optional[float] = None  # Laya's P(solvable within the budget)
    answers: Dict[str, dict] = field(default_factory=dict)  # Laya's raw answers


_FUNCTION = re.compile(r"\b([A-Za-z_]\w*)\(")


def names_one_function(description: str, files: Sequence[str] = ()) -> bool:
    """True when a node is already one function's worth of work: it names
    exactly one `name(...)` and at most one non-test file."""
    sources = [f for f in files if "test" not in f.lower()]
    return len(set(_FUNCTION.findall(description or ""))) == 1 and len(sources) <= 1


def fallback_status(level: str, description: str = "", files: Sequence[str] = ()) -> str:
    """The rule (laya_plan.md §5.2): components and files need the next
    layer; a function-level leaf is accepted, since splitting it further on a
    guess could loop and a genuinely oversized one still hits Dev's episode
    budget. Never REDO -- an unsure "badly designed" isn't worth an Architect
    call.

    A node above the task level that already names a single function is
    accepted too (D31, G9): on the first real v2 run, "Implement parse(line)
    in calculator.py" was sent down another layer and came back as the same
    sentence, costing an episode per level for nothing."""
    if level not in LEVELS:
        raise ValueError(f"unknown level {level!r}")
    if level == "task" or (level == "lead" and names_one_function(description, files)):
        return GOOD
    # Never for an Architect component: on the stui run "Create
    # src/data/dashboardData.js exporting all static data ... getTickerData(sym)"
    # mentions one function but is a 400-line component, and accepting it
    # skipped Lead and Task entirely.
    return BREAKDOWN


class FallbackJudge:
    """The rule alone, no model (the default; LAYA=1 adds Laya) -- same
    interface as LayaJudge."""

    def judge(self, nodes: Sequence[JudgeNode]) -> List[Verdict]:
        return [_rule_verdict(n, RULE) for n in nodes]


def _rule_verdict(node: JudgeNode, decided_by: str) -> Verdict:
    status = fallback_status(node.level, node.description, node.files)
    return Verdict(node_id=node.node_id, status=status, decided_by=decided_by, rule_status=status)


def build_state(node: JudgeNode) -> dict:
    """What Laya reads: the task and its description, and the question as the
    goal -- plain text it can judge, not the plan's labels."""
    text = f"Task: {node.description}"
    if node.notes:
        text += f"\nDescription: {node.notes}"
    return {"input": text[:INPUT_MAX_CHARS], "goal": SIZING_QUESTION}


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def min_confidence() -> float:
    """LAYA_MIN_CONFIDENCE (default 0.75): how sure Laya must be for a
    disagreement with the rule to go to the LLM tie-break instead of the
    rule simply winning."""
    try:
        return float(os.environ.get("LAYA_MIN_CONFIDENCE", "0.75"))
    except ValueError:
        return 0.75


def laya_device() -> str:
    """LAYA_DEVICE: cpu (default) or cuda. CPU by default because the planner's
    LLM usually fills the GPU: with a 27B model loaded, ~2.8 GB of VRAM is
    left, and Laya's auto device pick would take CUDA and compete for it. The
    judge scores one small batch per step, so CPU speed is fine."""
    device = os.environ.get("LAYA_DEVICE", "").strip().lower() or "cpu"
    if device not in ("cpu", "cuda"):
        raise ValueError(f"LAYA_DEVICE must be cpu or cuda, got {device!r}")
    return device


def _make_router():
    from laya import Router
    router = Router(max_loaded=1, device=laya_device())
    router.preload([CHECKPOINT])
    return router


def _child_predict(requests: list, queue) -> None:
    try:
        queue.put(("ok", _make_router().predict_batch(requests)))
    except Exception as e:  # reported to the parent, which falls back
        queue.put(("error", f"{type(e).__name__}: {e}"))


def _predict_in_child(requests: list) -> list:
    ctx = mp.get_context("spawn")
    queue = ctx.Queue()
    proc = ctx.Process(target=_child_predict, args=(requests, queue))
    proc.start()
    try:
        kind, payload = queue.get(timeout=CHILD_TIMEOUT_SECONDS)
    finally:
        proc.join(timeout=30)
        if proc.is_alive():
            proc.kill()
    if kind != "ok":
        raise RuntimeError(payload)
    return payload


TIEBREAK_PROMPT = """You settle disagreements about a software plan. The project goal:
{goal}

Each item below is a plan node and two opinions about it. For each item, pick ONE of the two
opinions offered:
- GOOD: one small, clear piece of work, ready to build as it is (one function, one config file,
  one contiguous copy);
- BREAKDOWN: several separate pieces of work (several files, functions, sections or steps) that
  should be split first.

{items}

Reply with one line per item and nothing else, in the form:
1: GOOD"""


def _tiebreak(llm, goal: str, disputed: List[tuple]) -> Dict[int, str]:
    """One LLM call for every disagreement of a judge step (not one per
    node): returns {node_id: status} for the answers it could read, each one
    of the two statuses that item offered."""
    from JFI.tool.llm_tools import ask_llm

    lines = []
    for i, (node, a, b) in enumerate(disputed, 1):
        files = f" | files: {', '.join(node.files)}" if node.files else ""
        done = f" | done when: {node.done_when}" if node.done_when else ""
        lines.append(f"{i}. [{node.level}] {node.description}{files}{done}\n   choose: {a} or {b}")
    reply = ask_llm(TIEBREAK_PROMPT.format(goal=goal[:GOAL_MAX_CHARS], items="\n".join(lines)), llm)
    picked = {}
    for m in re.finditer(r"^\s*(\d+)\s*[:.)-]\s*(GOOD|BREAKDOWN|REDO)\b", reply, re.M | re.I):
        i, status = int(m.group(1)), m.group(2).upper()
        if 1 <= i <= len(disputed) and status in disputed[i - 1][1:]:
            picked[disputed[i - 1][0].node_id] = status
    return picked


def make_judge(goal: str, llm=None, log: Callable[[str], None] = print):
    """The planner's judge: the rule alone, or with LAYA=1 in .env the rule
    and Laya, tie-broken by `llm`. Laya is the optional `laya` extra; without
    it (a source install without `uv sync --extra laya`) the rule
    decides, and the judge says so once."""
    if not _env_flag("LAYA"):
        log("Planner judge: the rule (LAYA=1 in .env adds Laya).")
        return FallbackJudge()
    log(f"Planner judge: the rule + Laya ({CHECKPOINT}, on {laya_device()}); when they disagree and Laya is "
        f"at least {min_confidence()} sure, the LLM breaks the tie, otherwise the rule decides.")
    return LayaJudge(goal, llm=llm, log=log)


class LayaJudge:
    """One per session. `judge()` is called once per judge step with every
    unjudged node of that step: one `predict_batch` for Laya, then at most one
    tie-break LLM call."""

    def __init__(self, goal: str, llm=None, router_factory: Callable = _make_router,
                 lmstudio=None, log: Callable[[str], None] = print):
        self.goal = goal
        self.llm = llm
        self._router_factory = router_factory
        self._router = None
        self._lmstudio = lmstudio
        self._log = log
        self._import_error: Optional[str] = None

    def judge(self, nodes: Sequence[JudgeNode]) -> List[Verdict]:
        if not nodes:
            return []
        if self._import_error:
            return [_rule_verdict(n, RULE_NO_LAYA) for n in nodes]

        requests = [{"state": build_state(node), "questions": QUESTIONS, "model": CHECKPOINT} for node in nodes]
        try:
            raw = self._predict(requests)
        except ImportError as e:
            self._import_error = str(e)
            self._log(f"Laya isn't installed ({e}); judging with the rule alone. "
                      f"Install it with: uv sync --extra laya")
            return [_rule_verdict(n, RULE_NO_LAYA) for n in nodes]
        except Exception as e:
            self._log(f"Laya failed this judge step ({type(e).__name__}: {e}); judging with the rule alone.")
            return [_rule_verdict(n, RULE_NO_LAYA) for n in nodes]

        verdicts = [self._score(node, raw[i]["answers"]) for i, node in enumerate(nodes)]
        disputed = [(n, v.rule_status, v.laya_status) for n, v in zip(nodes, verdicts)
                    if v.decided_by == LLM]
        if disputed:
            picked = _tiebreak(self.llm, self.goal, disputed) if self.llm is not None else {}
            for v in verdicts:
                if v.decided_by != LLM:
                    continue
                v.tiebreak_status = picked.get(v.node_id)
                if v.tiebreak_status is None:
                    v.decided_by, v.status = RULE, v.rule_status  # no usable answer: the rule stands
                else:
                    v.status = v.tiebreak_status
        for v in verdicts:
            if v.status != REDO:
                v.redo_reason = None
        return verdicts

    def _predict(self, requests: list) -> list:
        if _env_flag("UNLOAD_LLM_BEFORE_LAYA"):
            from JFI.llm.lmstudio_control import LMStudioControl
            control = self._lmstudio or LMStudioControl()
            with control.models_unloaded(self._log):
                if self._router_factory is _make_router:
                    return _predict_in_child(requests)
                return self._router_factory().predict_batch(requests)
        if self._router is None:
            self._router = self._router_factory()
        return self._router.predict_batch(requests)

    @staticmethod
    def _score(node: JudgeNode, answers: dict) -> Verdict:
        """Both scores for one node; decided_by is LLM when a tie-break is
        still needed (resolved in judge(), once for the whole step)."""
        rule = fallback_status(node.level, node.description, node.files)
        answer = answers["solvable"]
        p_yes = answer["noul"]
        laya = GOOD if p_yes >= LAYA_YES_THRESHOLD else BREAKDOWN
        confidence = answer.get("answer_confidence")
        v = Verdict(node_id=node.node_id, status=rule, decided_by=RULE, rule_status=rule, laya_status=laya,
                    confidence=confidence, p_yes=p_yes, answers=answers)
        if laya == rule:
            v.decided_by = AGREE
        elif confidence is not None and confidence >= min_confidence():
            v.decided_by = LLM
        return v
