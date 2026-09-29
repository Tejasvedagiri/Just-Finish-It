"""The v2 planning loop (laya_plan.md §2): Architect -> Lead -> Task, every
node judged GOOD / BREAKDOWN / REDO, gated by level.

Each step takes the FIRST pending action in this order, re-reading the DB
every time (so resume is just "run again"):

    for level in architect, lead, task:
        judge  the level's unjudged nodes         (one batch, the judge)
        redo   the level's REDO nodes              (one episode each, by their creator)
        break down the level's BREAKDOWN nodes     (one episode each, by the next role)
             that have no children yet

Because a level's judging and redos come before its breakdowns, and each
level before the next, nothing goes to Lead while any Architect node is
unjudged or REDO, and nothing to Task while any Lead node is (the gates,
D15). A broken-down node becomes GOOD once it has children. Planning is
done when no action is left: every live node is GOOD (D4, D27).

Guards (§4.3, §5.3) keep it finite: the redo cap (PLANNER_REDO_CAP), the
depth cap, and the episode budget (MAX_PLANNER_EPISODES). An escalated
parent pauses its subtree until it's settled again; then the children are
re-judged.
"""

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional

from JFI.episode.brief import ScopeAnchor, build_system_message
from JFI.episode.budget import episode_token_budget
from JFI.episode.engine import make_finish, run_episode
from JFI.episode.roles import ROLE_ENV_PREFIXES, ROLE_FINISH_TOOL
from JFI.episode.tools import EpisodeTools
from JFI.models import Leaf, PlanEvent, PlannerVerdict, get_session
from JFI.planner.judge import JudgeNode
from JFI.planner.nodes import (
    BREAKDOWN, GOOD, MAX_LEAF_DEPTH, REDO, children_of, depth_of, load_nodes, make_node_tools, path_of,
)
from JFI.planner.prompts import ROLE_PROMPTS
from JFI.tool.code_tools import make_code_tools
from JFI.tool.design_tools import design_index, make_design_tools
from JFI.tool.note_tools import add_reviewer_note
from JFI.tool.runbook_tools import make_runbook_tools, runbook_index

LEVELS = ("architect", "lead", "task")
NEXT_ROLE = {"architect": "lead", "lead": "task", "task": "task"}
DEFAULT_REDO_CAP = 2
DEFAULT_MAX_PLANNER_EPISODES = 200


def redo_cap() -> int:
    try:
        return max(0, int(os.environ.get("PLANNER_REDO_CAP", DEFAULT_REDO_CAP)))
    except ValueError:
        return DEFAULT_REDO_CAP


def max_planner_episodes() -> int:
    try:
        return max(1, int(os.environ.get("MAX_PLANNER_EPISODES", DEFAULT_MAX_PLANNER_EPISODES)))
    except ValueError:
        return DEFAULT_MAX_PLANNER_EPISODES


@dataclass
class PlanResult:
    complete: bool
    episodes: int
    reason: str = ""


class Planner:
    def __init__(self, console, engine, session_id: str, goal: str, root: Path,
                 llm_for_role: Callable[[str], object], judge, feedback: str = ""):
        self.console, self.engine, self.session_id = console, engine, session_id
        self.goal, self.root, self.feedback = goal, Path(root), feedback
        self.llm_for_role, self.judge = llm_for_role, judge
        self.episodes = 0

    # ---------------------------------------------------------------- run

    def run(self) -> PlanResult:
        if not load_nodes(self.engine, self.session_id):
            if not self._episode("architect", "create", None) and not load_nodes(self.engine, self.session_id):
                return PlanResult(False, self.episodes, "the Architect produced no plan")
        elif self.feedback:
            self._episode("architect", "extend", None)

        while True:
            if self.console.should_stop():
                return PlanResult(False, self.episodes, "stopped")
            if self.episodes >= max_planner_episodes():
                self._settle_everything("the planner episode budget (MAX_PLANNER_EPISODES) ran out")
                return PlanResult(True, self.episodes, "episode budget reached")
            action = self._next_action()
            if action is None:
                return PlanResult(True, self.episodes)
            kind, level, nodes = action
            if kind == "judge":
                self._judge(nodes)
            elif kind == "redo":
                self._redo(nodes[0])
            else:
                self._breakdown(nodes[0])

    def _next_action(self):
        nodes = load_nodes(self.engine, self.session_id)
        live = [n for n in nodes if not n.paused and n.status.value != "done"]
        for level in LEVELS:
            at = [n for n in live if n.level == level]
            unjudged = [n for n in at if n.plan_status is None]
            if unjudged:
                return "judge", level, unjudged
            redo = [n for n in at if n.plan_status == REDO]
            if redo:
                return "redo", level, redo
            unsplit = [n for n in at if n.plan_status == BREAKDOWN and not children_of(nodes, n.id)]
            if unsplit:
                return "breakdown", level, unsplit
        return None

    # ---------------------------------------------------------------- judging

    def _judge(self, nodes: List[Leaf]) -> None:
        all_nodes = load_nodes(self.engine, self.session_id)
        by_id = {n.id: n for n in all_nodes}
        verdicts = self.judge.judge([
            JudgeNode(n.id, n.level, n.description, done_when=n.done_when or "", files=n.files or [],
                      path=path_of(by_id, n)) for n in nodes])
        for v in verdicts:
            node = by_id[v.node_id]
            status = v.status
            if status == BREAKDOWN and children_of(all_nodes, node.id):
                status = GOOD  # already broken down (e.g. re-judged after an escalation)
            if status == BREAKDOWN and depth_of(by_id, node) >= MAX_LEAF_DEPTH:
                status = GOOD
                self._note(node, f"node {node.id} hit the depth cap; accepted as GOOD")
            if status == REDO and node.redo_count >= redo_cap():
                status = GOOD
                self._event(node.id, "cap_reached", f"redo cap ({redo_cap()}) reached; accepted as GOOD")
                self._note(node, f"node {node.id} was judged badly designed ({v.redo_reason}) after "
                                 f"{node.redo_count} redo(s); accepted as-is")
            self._set_status(node.id, status, v.redo_reason if status == REDO else None,
                             bump_redo=status == REDO)
            self._record_verdict(node, v, status)
            if node.escalation_count and status != REDO:
                self._unpause_children(node.id)

    def _set_status(self, node_id: int, status: str, reason: Optional[str], bump_redo: bool = False) -> None:
        with get_session(self.engine) as db:
            row = db.get(Leaf, node_id)
            row.plan_status = status
            row.redo_reason = reason
            if bump_redo:
                row.redo_count += 1
            db.add(row)
            db.commit()

    def _record_verdict(self, node: Leaf, v, final_status: str) -> None:
        answers = v.answers.get(v.model, {}) if v.model else {}
        verdict = answers.get("verdict", {}) if answers else {}
        with get_session(self.engine) as db:
            db.add(PlannerVerdict(
                session_id=self.session_id, node_id=node.id, level=node.level, laya_model=v.model,
                laya_verdict=v.status if v.source == "laya" else verdict.get("choice"),
                laya_redo_reason=v.redo_reason, probabilities=verdict.get("probabilities"),
                answer_confidence=v.confidence, fallback=v.fallback or "none", final_status=final_status))
            db.commit()

    def _unpause_children(self, node_id: int) -> None:
        nodes = load_nodes(self.engine, self.session_id)
        with get_session(self.engine) as db:
            frontier = [node_id]
            while frontier:
                parent = frontier.pop()
                for child in children_of(nodes, parent):
                    row = db.get(Leaf, child.id)
                    if row.paused:
                        row.paused, row.plan_status = False, None  # re-judged against the redone parent
                        db.add(row)
                    frontier.append(child.id)
            db.commit()

    # ---------------------------------------------------------------- redo / breakdown

    def _redo(self, node: Leaf) -> None:
        self._event(node.id, "redo", node.redo_reason or "")
        mode = "redo"
        self._episode(node.level, mode, node, reason=node.redo_reason or "")
        after = next((n for n in load_nodes(self.engine, self.session_id) if n.id == node.id), None)
        if after is not None and after.plan_status == REDO:
            # The role didn't rewrite it: count the attempt; the cap stops it looping.
            if after.redo_count >= redo_cap():
                self._set_status(node.id, GOOD, None)
                self._note(after, f"node {node.id} still badly designed after {after.redo_count} redo(s); "
                                  "accepted as-is")
            else:
                self._set_status(node.id, REDO, after.redo_reason, bump_redo=True)

    def _breakdown(self, node: Leaf) -> None:
        role = NEXT_ROLE[node.level]
        mode = "split" if node.level == "task" else "breakdown"
        self._event(node.id, "breakdown", f"{role} {mode}")
        result = self._episode(role, mode, node)
        nodes = load_nodes(self.engine, self.session_id)
        if next(n for n in nodes if n.id == node.id).plan_status == REDO:
            return  # the next role escalated it back to its creator during the episode
        if children_of(nodes, node.id):
            self._set_status(node.id, GOOD, None)
        elif result is not None and result.end_reason in ("budget", "turn_cap"):
            # Too big for one pass of the next layer: back to its creator (§5.3).
            self._event(node.id, "overflow", f"{role} {mode} ended on {result.end_reason}")
            self._set_status(node.id, REDO, "too_big", bump_redo=True)
        else:
            self._set_status(node.id, GOOD, None)
            self._note(node, f"the {role} added nothing under node {node.id}; treated as small enough")

    # ---------------------------------------------------------------- episodes

    def _episode(self, role: str, mode: str, node: Optional[Leaf], reason: str = ""):
        if self.console.should_stop():
            return None
        self.episodes += 1
        nodes = load_nodes(self.engine, self.session_id)
        by_id = {n.id: n for n in nodes}
        if node is None:
            anchor = ScopeAnchor(role=role, node_id=None, node=f"the goal: {self.goal[:600]}",
                                 finish=f"{ROLE_FINISH_TOOL[role]}(0, summary)",
                                 reason=self.feedback[:600] if mode == "extend" else "")
        else:
            anchor = ScopeAnchor(role=role, node_id=node.id, node=node.description, done_when=node.done_when or "",
                                 files=node.files or [], path=path_of(by_id, node),
                                 finish=f"{ROLE_FINISH_TOOL[role]}({node.id}, summary)", reason=reason)
        scope_id = node.id if node is not None else None
        impl = {**make_node_tools(self.engine, self.session_id, role, scope_id),
                **make_runbook_tools(self.engine, self.session_id, role),
                **make_design_tools(self.engine, self.session_id, role),
                **make_code_tools(self.root),
                "finish": make_finish(anchor)}
        system = build_system_message(anchor, ROLE_PROMPTS[(role, mode)],
                                      [runbook_index(self.engine, self.session_id),
                                       design_index(self.engine, self.session_id)])
        self.console.set_status(stage=role.capitalize())
        self.console.display_rule(f"PLANNER · {role.upper()} {mode}"
                                  + (f" — node {node.id}" if node is not None else ""))
        return run_episode(self.llm_for_role(role), self.console, self.engine, self.session_id,
                           role=role, mode=mode, anchor=anchor, system_message=system,
                           tools=EpisodeTools(role, impl),
                           budget=episode_token_budget(ROLE_ENV_PREFIXES[role]))

    # ---------------------------------------------------------------- guards

    def _settle_everything(self, why: str) -> None:
        nodes = load_nodes(self.engine, self.session_id)
        for n in nodes:
            if n.plan_status != GOOD and not n.paused:
                self._set_status(n.id, GOOD, None)
        self.console.display_error(f"Planning stopped early: {why}. Remaining nodes were accepted as-is.")
        add_reviewer_note(self.engine, self.session_id, f"Planner: {why}; unfinished nodes were accepted as-is.")

    def _note(self, node: Leaf, text: str) -> None:
        add_reviewer_note(self.engine, self.session_id, f"Planner: {text}.")

    def _event(self, node_id: Optional[int], kind: str, detail: str) -> None:
        with get_session(self.engine) as db:
            db.add(PlanEvent(session_id=self.session_id, node_id=node_id, type=kind, detail=detail))
            db.commit()
