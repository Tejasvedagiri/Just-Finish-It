"""The planner's judge: gives every plan node a status -- GOOD, BREAKDOWN or
REDO -- from two scores (laya_plan.md §5):

- **the rule** (`fallback_status`): deterministic, no model -- components
  and files break down, function-level nodes are accepted, never REDO;
- **Laya**: its published `english` checkpoint, asked one question per node.

They agree: that's the verdict. They disagree and Laya is confident
(answer_confidence >= LAYA_MIN_CONFIDENCE, default 0.75): one short LLM call
breaks the tie between the two answers. They disagree and Laya isn't
confident: the rule decides. Every score is kept on the Verdict and stored as
a PlannerVerdict row, so the dashboard shows them side by side.

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

# One short persona question per level: the role that RECEIVES the node asks
# whether it's clean enough to work on. Neutral A/B/C/D keys: Laya's README
# documents its checkpoints following yes/no/true/false label words instead
# of the text being judged.
#
# Budget: Laya packs "choice question: <instructions>" and every "[MASK] A: ..."
# option into head_max_len tokens (192 on `english`), options first, and
# silently truncates the instruction to whatever is left; each option is also
# cut at 48 tokens. test_judge_questions_fit_laya_head_budget checks every
# question here against those limits, so an edit that would be truncated
# fails loudly.
_VERDICT_OPTIONS = {
    "A": "clean: small and clear, ready to build as it is",
    "B": "break down: clear, but too big; split it into smaller tasks",
    "C": "redo: not real work; a command to run the app, or too vague",
}

QUESTIONS_BY_LEVEL = {
    "architect": {
        "type": "choice",
        "instructions": "You are a lead, tasked with breaking a component down into files. "
                        "Is this task clean enough?",
        "criteria": _VERDICT_OPTIONS,
    },
    "lead": {
        "type": "choice",
        "instructions": "You are a task planner, tasked with breaking a file down into functions. "
                        "Is this task clean enough?",
        "criteria": _VERDICT_OPTIONS,
    },
    "task": {
        "type": "choice",
        "instructions": "You are a developer, tasked with writing one function and its test. "
                        "Is this task clean enough?",
        "criteria": _VERDICT_OPTIONS,
    },
}

REDO_QUESTION = {
    "type": "choice",
    "instructions": "Why is this task not real work?",
    "criteria": {
        "A": "it is a command to install, run, stop, view or test the app",
        "B": "it is too vague to build",
        "C": "it repeats another task",
        "D": "it does not fit the design",
    },
}


def questions_for(level: str) -> dict:
    return {"verdict": QUESTIONS_BY_LEVEL[level], "redo_reason": REDO_QUESTION}


STATUS_BY_KEY = {"A": GOOD, "B": BREAKDOWN, "C": REDO}
_REDO_REASON_BY_KEY = {"A": "operational", "B": "vague", "C": "duplicate", "D": "design"}

# Laya's English checkpoint leaves ~320 tokens for the state after its option
# budget (laya_plan.md §3.2 rule 2); ~4 chars/token.
GOAL_MAX_CHARS = 300
STATE_MAX_CHARS = 1200
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


def build_state(goal: str, node: JudgeNode) -> dict:
    state = {
        "goal": goal[:GOAL_MAX_CHARS],
        "level": node.level,
        "path": " > ".join(node.path),
        "node": node.description,
        "done_when": node.done_when,
        "files": ", ".join(node.files),
    }
    # Trim the path first (least specific), then the goal, so the node's own
    # text -- the thing actually being judged -- survives intact.
    for key in ("path", "goal"):
        overflow = sum(len(v) for v in state.values()) - STATE_MAX_CHARS
        if overflow > 0:
            state[key] = state[key][: max(0, len(state[key]) - overflow)]
    return state


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
  should be split first;
- REDO: not real work -- an operational step (install, run, test, open) or too vague to build.

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

        requests = [{"state": build_state(self.goal, node), "questions": questions_for(node.level),
                     "model": CHECKPOINT} for node in nodes]
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
        verdict = answers["verdict"]
        laya = STATUS_BY_KEY[verdict["choice"]]
        confidence = verdict.get("answer_confidence")
        redo_reason = _REDO_REASON_BY_KEY.get(answers["redo_reason"]["choice"], "design") \
            if "redo_reason" in answers else "design"
        v = Verdict(node_id=node.node_id, status=rule, decided_by=RULE, rule_status=rule, laya_status=laya,
                    confidence=confidence, redo_reason=redo_reason, answers=answers)
        if laya == rule:
            v.decided_by = AGREE
        elif confidence is not None and confidence >= min_confidence():
            v.decided_by = LLM
        return v
