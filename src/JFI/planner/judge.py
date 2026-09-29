"""The v2 planner's judge: gives every plan node a status -- GOOD, BREAKDOWN
or REDO -- using Laya (laya_plan.md §5), with a deterministic fallback that
needs no model and no LLM call.

Laya runs in process with two checkpoints, `english` and `typed-decisions`
(never `multilingual`), loaded once per session on the first judge step and
kept loaded (D21): measured at ~3.5 GiB resident, and an in-process
`unload()` frees none of it anyway (laya_plan.md §5.1.1). Every node is
asked on BOTH checkpoints and both answers are kept on the Verdict -- which
one should decide is an open tuning question (laya_plan.md §11), so
LAYA_JUDGE_MODEL picks the deciding one and the other is logged for
comparison.

UNLOAD_LLM_BEFORE_LAYA=1 swaps LM Studio's models out around each judge step
(see JFI.llm.lmstudio_control). In that mode Laya runs in a child process
instead: unloading the LLM only helps if Laya's memory is really gone again
before the LLM reloads, and only a process exit returns it to the OS.
"""

import multiprocessing as mp
import os
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence

GOOD, BREAKDOWN, REDO = "GOOD", "BREAKDOWN", "REDO"
LEVELS = ("architect", "lead", "task")
CHECKPOINTS = ("english", "typed-decisions")

# One short persona question per level: the role that RECEIVES the node asks
# whether it's clean enough to work on. Kept deliberately simple -- the
# fine-tuned checkpoint (laya-finetuning/) is trained on exactly these
# strings, so changing any of them means retraining; the base checkpoints
# scored at chance on every wording tried (laya-finetuning/README.md).
#
# Neutral A/B/C/D keys: Laya's README documents its checkpoints following
# yes/no/true/false label words instead of the text being judged.
#
# Budget: Laya packs "choice question: <instructions>" and every "[MASK] A: ..."
# option into head_max_len tokens (192 on `english`, 256 on
# `typed-decisions`), options first, and silently truncates the instruction
# to whatever is left; each option is also cut at 48 tokens.
# test_judge_questions_fit_laya_head_budget checks every question here
# against those limits, so an edit that would be truncated fails loudly.
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


_STATUS_BY_KEY = {"A": GOOD, "B": BREAKDOWN, "C": REDO}
_REDO_REASON_BY_KEY = {"A": "operational", "B": "vague", "C": "duplicate", "D": "design"}

# Laya's English checkpoint leaves ~320 tokens for the state after its option
# budget (laya_plan.md §3.2 rule 2); ~4 chars/token.
GOAL_MAX_CHARS = 300
STATE_MAX_CHARS = 1200
CHILD_TIMEOUT_SECONDS = 900  # includes loading both checkpoints from disk


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
    source: str  # "laya" or "fallback"
    redo_reason: Optional[str] = None
    confidence: Optional[float] = None
    fallback: Optional[str] = None  # None / "low_confidence" / "unavailable"
    model: Optional[str] = None
    answers: Dict[str, dict] = field(default_factory=dict)  # checkpoint -> Laya's raw answers


def fallback_status(level: str) -> str:
    """The safe choice when Laya is unsure or absent (laya_plan.md §5.2):
    components and files always need the next layer anyway; a function-level
    leaf is accepted, since splitting it further on a guess could loop and a
    genuinely oversized one still hits Dev's episode budget. Never REDO -- an
    unsure "badly designed" isn't worth an Architect call."""
    if level not in LEVELS:
        raise ValueError(f"unknown level {level!r}")
    return GOOD if level == "task" else BREAKDOWN


class FallbackJudge:
    """The deterministic judge: fallback_status() for every node, no model.
    Used until Laya is wired in (phase 8) and whenever it's unavailable --
    same interface as LayaJudge."""

    def judge(self, nodes: Sequence[JudgeNode]) -> List[Verdict]:
        return [Verdict(node_id=n.node_id, status=fallback_status(n.level), source="fallback",
                        fallback="conservative") for n in nodes]


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


def _min_confidence() -> float:
    try:
        return float(os.environ.get("LAYA_MIN_CONFIDENCE", "0.6"))
    except ValueError:
        return 0.6


def _judge_model() -> str:
    model = os.environ.get("LAYA_JUDGE_MODEL", "english").strip() or "english"
    if model not in CHECKPOINTS:
        raise ValueError(f"LAYA_JUDGE_MODEL must be one of {CHECKPOINTS}, got {model!r}")
    return model


def _checkpoint_overrides() -> Dict[str, str]:
    """LAYA_ENGLISH_PATH / LAYA_TYPED_DECISIONS_PATH point a checkpoint at a
    local folder, e.g. one produced by laya-finetuning/finetune.py; unset
    means the published Hugging Face checkpoint."""
    overrides = {}
    for name, var in (("english", "LAYA_ENGLISH_PATH"), ("typed-decisions", "LAYA_TYPED_DECISIONS_PATH")):
        path = os.environ.get(var, "").strip()
        if path:
            overrides[name] = path
    return overrides


def _make_router():
    from laya import Router
    router = Router(max_loaded=len(CHECKPOINTS), models=_checkpoint_overrides() or None)
    router.preload(list(CHECKPOINTS))
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


class LayaJudge:
    """One per session. `judge()` is called once per judge step with every
    unjudged node of that step (one `predict_batch`, not a call per node)."""

    def __init__(self, goal: str, router_factory: Callable = _make_router,
                 lmstudio=None, log: Callable[[str], None] = print):
        self.goal = goal
        self._router_factory = router_factory
        self._router = None
        self._lmstudio = lmstudio
        self._log = log
        self._import_error: Optional[str] = None

    def judge(self, nodes: Sequence[JudgeNode]) -> List[Verdict]:
        if not nodes:
            return []
        model = _judge_model()  # a bad LAYA_JUDGE_MODEL is a config error: raise before any work
        if self._import_error:
            return [self._fallback(n, "unavailable") for n in nodes]

        requests = [
            {"state": build_state(self.goal, node), "questions": questions_for(node.level), "model": model}
            for node in nodes for model in CHECKPOINTS
        ]
        try:
            raw = self._predict(requests)
        except ImportError as e:
            self._import_error = str(e)
            self._log(f"Laya isn't installed ({e}); judging with the fallback rule. "
                      f"Install it with: uv sync --extra laya")
            return [self._fallback(n, "unavailable") for n in nodes]
        except Exception as e:
            self._log(f"Laya failed this judge step ({type(e).__name__}: {e}); using the fallback rule.")
            return [self._fallback(n, "unavailable") for n in nodes]

        verdicts = []
        for i, node in enumerate(nodes):
            answers = {
                model: raw[i * len(CHECKPOINTS) + j]["answers"]
                for j, model in enumerate(CHECKPOINTS)
            }
            verdicts.append(self._decide(node, answers, model))
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

    def _decide(self, node: JudgeNode, answers: Dict[str, dict], model: str) -> Verdict:
        verdict = answers[model]["verdict"]
        confidence = verdict.get("answer_confidence")
        if confidence is None or confidence < _min_confidence():
            result = self._fallback(node, "low_confidence")
            result.confidence, result.model, result.answers = confidence, model, answers
            return result

        status = _STATUS_BY_KEY[verdict["choice"]]
        redo_reason = None
        if status == REDO:
            redo_reason = _REDO_REASON_BY_KEY.get(answers[model]["redo_reason"]["choice"], "design")
        return Verdict(node_id=node.node_id, status=status, source="laya", redo_reason=redo_reason,
                       confidence=confidence, model=model, answers=answers)

    @staticmethod
    def _fallback(node: JudgeNode, why: str) -> Verdict:
        return Verdict(node_id=node.node_id, status=fallback_status(node.level),
                       source="fallback", fallback=why)
