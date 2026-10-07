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
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional

from JFI.episode.brief import ScopeAnchor, build_system_message
from JFI.episode.budget import episode_token_budget
from JFI.episode.engine import make_finish, run_episode
from JFI.episode.parallel_console import ParallelConsole
from JFI.episode.roles import ROLE_ENV_PREFIXES, ROLE_FINISH_TOOL
from JFI.episode.tools import EpisodeTools
from sqlmodel import select

from JFI.llm.parallel import effective_parallel
from JFI.models import DesignEntry, Leaf, PlanEvent, PlannerVerdict, RunbookEntry, get_session
from JFI.planner.judge import CHECKPOINT, JudgeNode
from JFI.planner.nodes import (
    BREAKDOWN, GOOD, MAX_LEAF_DEPTH, NO_CASE_KINDS, REDO, repo_path, children_of, depth_of, load_nodes,
    make_node_tools, path_of,
)
from JFI.planner.prompts import ROLE_PROMPTS
from JFI.tool.code_tools import make_code_tools
from JFI.tool.design_tools import design_index, make_design_tools, references_text
from JFI.tool.evidence_tools import list_cases, make_evidence_tools, read_evidence, sync_evidence_names
from JFI.tool.note_tools import add_reviewer_note
from JFI.tool.plan_db_tools import plan_status_fields
from JFI.tool.runbook_tools import make_runbook_tools, runbook_index

LEVELS = ("architect", "lead", "task")
NEXT_ROLE = {"architect": "lead", "lead": "task", "task": "task"}
DEFAULT_REDO_CAP = 2
DEFAULT_MAX_PLANNER_EPISODES = 200
ARCHITECT_CONTINUATIONS = 2
# The project's layout lives in the runbook too (src_dir, test_dir, test_naming):
# on the QA machine the Lead prompt's colocated example put chart-bar.test.js
# beside its source while the Architect's test_one ran __tests__/{test_id}.test.js,
# so those tests could never be run through test_one. `script` runs scratch code
# from a file instead of inline python -c / node -e.
# `entry` is the file the app starts from (src/main.js for Vite, main.py for a
# CLI). Observed on the QA machine's stui run: index.html imported
# /src/main.js, but no node owned it -- nothing wired the shell and views
# together and Vite failed mid-imp. The Architect's finish now refuses until a
# plan node lists the entry file.
REQUIRED_RUNBOOK = ("setup", "run", "test", "test_one", "build", "e2e", "script", "entry", "src_dir", "test_dir",
                    "test_naming")
_PATH = re.compile(r"[\w./\\-]+\.\w+")
# What says a goal comes with a ground truth to match (docs/old_new.md). The
# Architect's finish then wants a reference entry -- or an assumption saying
# there is none, so a false hit costs one line, not a stuck plan.
_THING = r"(https?://\S+|[\w./-]+\.(html?|png|jpe?g|svg|pdf|csv|json|sh|py|js|sql)\b|the (old|original|existing|legacy|current)\b|database\b)"
# Each phrase only counts when it points at something concrete: the benchmark
# goals' own prose ("make it look like a real pricing page", "the expected
# values" of a return dict) must not read as a ground truth.
_GROUND_TRUTH_WORDS = re.compile(
    r"\b(compare|check|verify|validate|test)\b[^.\n]{0,40}\b(with|against)\b|\bground[- ]truth\b|\bgolden\b|"
    r"\bpixel[- ]perfect\b|figma\.com/|\b(looks?|match(es)?|same as)\b[^\n]{0,20}?" + _THING + "|"
    r"\b(convert|turn|rebuild|migrate|rewrite|reimplement|re-implement|port)\b[^\n]{0,60}?" + _THING, re.I)
_REFERENCE_FILE = re.compile(r"\.(png|jpe?g|svg|pdf|webp|gif)$|expected|golden", re.I)
GOAL_MAX_CHARS = 16_000  # ~4k tokens, a small share of an episode on a 32k window
FEEDBACK_MAX_CHARS = 12_000


def ground_truth_hint(goal: str, root: Path) -> Optional[str]:
    """Why the goal looks like it names something the build must match, or None."""
    match = _GROUND_TRUTH_WORDS.search(goal or "")
    if match:
        return f'the goal says "{match.group(0).strip()}"'
    for path in _PATH.findall(goal or ""):
        rel = repo_path(path)
        if _REFERENCE_FILE.search(rel) and (Path(root) / rel).is_file():
            return f"the goal names {rel}"
    return None


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
                 llm_for_role: Callable[[str], object], judge, feedback: str = "",
                 pool_tools: Optional[Callable[[str], dict]] = None):
        """`pool_tools(role)`: the optional tools (roles.OPTIONAL_POOL) an
        episode can load_tool, bound to the session and that role's model."""
        self.console, self.engine, self.session_id = console, engine, session_id
        self.goal, self.root, self.feedback = goal, Path(root), feedback
        self.llm_for_role, self.judge = llm_for_role, judge
        self.pool_tools = pool_tools or (lambda role: {})
        self.episodes = 0
        self.failed: Optional[str] = None
        # PARALLEL_LLM: Lead / Task breakdowns of one level run side by side.
        # The plan tools read the tree, check it (duplicates, file owners,
        # depends_on) and then write, so they run one at a time across
        # episodes; otherwise two Leads could both claim the same file.
        self._write_lock = threading.RLock()
        self._render_lock, self._ask_lock = threading.RLock(), threading.RLock()
        self._parallel_notices: dict = {}

    # ---------------------------------------------------------------- run

    def run(self) -> PlanResult:
        if not load_nodes(self.engine, self.session_id):
            # Checked on node count, not on the episode's return: on the stui
            # run the Architect ran out of budget with no nodes, the episode
            # still "returned", and an empty plan was reported complete --
            # imp then "finished" nothing and the run went on to review.
            result = self._architect("create")
            if not load_nodes(self.engine, self.session_id):
                why = result.end_reason if result is not None else "stopped"
                return PlanResult(False, self.episodes, f"the Architect produced no plan (its episode ended: {why})")
        elif self.feedback:
            self._architect("extend")

        while True:
            if self.console.should_stop():
                return PlanResult(False, self.episodes, "stopped")
            if self.failed:
                return PlanResult(False, self.episodes, self.failed)
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
                self._breakdowns(nodes)

    def _architect(self, mode: str):
        """One Architect conversation, continued (up to ARCHITECT_CONTINUATIONS
        more) while it ends on the turn cap or budget instead of `finish`.
        Observed on the stui run: the Architect spent all 15 turns recording
        an excellent design, one design_set per turn, and added a single
        node -- the scaffold -- so the CSS, data, nav and all ten views were
        never planned. Its work is saved, so a fresh conversation can pick up
        from it instead of redoing it."""
        result = self._episode("architect", mode, None)
        for _ in range(ARCHITECT_CONTINUATIONS):
            if result is None or result.end_reason not in ("turn_cap", "budget"):
                break
            self._event(None, "overflow", f"architect {mode} ended on {result.end_reason}; continuing")
            result = self._episode("architect", "continue", None,
                                   reason=f"your previous conversation ended ({result.end_reason}) before you "
                                          "called finish")
        return result

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
        self.console.set_status(stage="Judge", task=f"{len(nodes)} {nodes[0].level} node(s)")
        all_nodes = load_nodes(self.engine, self.session_id)
        by_id = {n.id: n for n in all_nodes}
        verdicts = self.judge.judge([
            JudgeNode(n.id, n.level, n.description, done_when=n.done_when or "", files=n.files or [],
                      path=path_of(by_id, n), notes=n.notes or "") for n in nodes])
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
        self.console.set_status(**plan_status_fields(self.engine, self.session_id))

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
        with get_session(self.engine) as db:
            db.add(PlannerVerdict(
                session_id=self.session_id, node_id=node.id, level=node.level,
                laya_model=CHECKPOINT if v.laya_status else None, laya_verdict=v.laya_status,
                laya_redo_reason=v.redo_reason,
                probabilities={"yes": v.p_yes} if v.p_yes is not None else None,
                answer_confidence=v.confidence, rule_verdict=v.rule_status, tiebreak_verdict=v.tiebreak_status,
                decided_by=v.decided_by, final_status=final_status))
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

    def _parallel_for(self, role: str):
        """Probed at every batch, not once: LM Studio loads a model on its
        first request, so before the Architect has run nothing is loaded and
        there's no parallel setting to read."""
        workers, why = effective_parallel(self.llm_for_role(role))
        notice = f"{role.capitalize()} episodes: {workers} at a time ({why})."
        if self._parallel_notices.get(role) != notice:
            self._parallel_notices[role] = notice
            self.console.display_system(notice)
        return workers, why

    def _breakdowns(self, nodes: List[Leaf]) -> None:
        """The level's unsplit nodes: the first one alone, or with
        PARALLEL_LLM, all of them `workers` at a time. Each node's episodes
        only add under that node and only escalate that node, so siblings
        don't step on each other; the loop re-reads the DB after the batch."""
        role = NEXT_ROLE[nodes[0].level]
        workers, why = self._parallel_for(role) if role in ("lead", "task") and len(nodes) > 1 else (1, "")
        if workers <= 1:
            self._breakdown(nodes[0])
            return
        running: dict = {}

        def publish() -> None:
            with self._write_lock:
                rows = sorted(running.values(), key=lambda r: r["started_at"])
            self.console.set_status(parallel={"role": role, "workers": workers, "why": why, "running": rows})

        def one(node: Leaf) -> None:
            if self.failed or self.console.should_stop() or self.episodes >= max_planner_episodes():
                return
            with self._write_lock:
                running[node.id] = {"node_id": node.id, "task": node.description, "started_at": time.time()}
            publish()
            try:
                self._breakdown(node, ParallelConsole(self.console, f"{role} · node {node.id}",
                                                      self._render_lock, self._ask_lock))
            finally:
                with self._write_lock:
                    running.pop(node.id, None)
                publish()

        try:
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix=f"jfi-{role}") as pool:
                for future in [pool.submit(one, n) for n in nodes]:
                    future.result()
        finally:
            self.console.set_status(parallel={})

    def _breakdown(self, node: Leaf, console=None) -> None:
        role = NEXT_ROLE[node.level]
        mode = "split" if node.level == "task" else "breakdown"
        self._event(node.id, "breakdown", f"{role} {mode}")
        result = self._episode(role, mode, node, console=console)
        if result is not None and result.end_reason == "turn_cap":
            # Running out of turns isn't proof the node is too big -- only
            # running out of budget is -- and it isn't proof the node is fully
            # broken down either. Observed on the stui runs: Lead spent its 15
            # turns on a tiny view fighting scaffold_file's formatting and added
            # no node, and the view went back to the Architect as "too big";
            # and Task episodes that hit the cap after adding SOME functions
            # were accepted as complete, so the nav, dividends and news files
            # lost leaves. Either way the conversation is continued once.
            done = children_of(load_nodes(self.engine, self.session_id), node.id)
            self._event(node.id, "overflow", f"{role} {mode} ended on turn_cap with {len(done)} node(s); continuing")
            added = ("You had added these nodes under it:\n" + "\n".join(f"- {c.id}: {c.description}" for c in done)
                     + "\nKeep them; add only what's still missing, then finish.") if done else \
                "You hadn't added any nodes yet. Add them, then finish."
            result = self._episode(role, mode, node, console=console, reason=(
                "your previous conversation on this node hit its turn limit before it finished. What it did is "
                "saved: files it scaffolded are on disk (outline_file / list_dir them); don't redo them. " + added))
        if result is not None and result.end_reason == "error" and \
                not children_of(load_nodes(self.engine, self.session_id), node.id):
            self._event(node.id, "error", f"{role} {mode} ended on an LLM error; retrying once")
            result = self._episode(role, mode, node, console=console, reason=(
                "your previous conversation on this node was cut off by a model-server error. What it did is "
                "saved: files it scaffolded are on disk (outline_file / list_dir them). Add the nodes, then finish."))
        nodes = load_nodes(self.engine, self.session_id)
        if next(n for n in nodes if n.id == node.id).plan_status == REDO:
            return  # the next role escalated it back to its creator during the episode
        if children_of(nodes, node.id):
            self._set_status(node.id, GOOD, None)
        elif result is None or result.end_reason in ("error", "stopped"):
            # The episode never got to decide anything. Observed on stui run
            # 12: LM Studio returned "failed to decode" mid-episode on three
            # views, and each was then accepted as GOOD ("added nothing") and
            # would have gone to Dev whole. It stays BREAKDOWN; the run stops.
            why = "stopped" if result is None else result.end_reason
            self.failed = f"the {role} on node {node.id} ended on {why} twice without adding nodes"
        elif result.end_reason in ("budget", "turn_cap"):
            # Too big for one pass of the next layer: back to its creator (§5.3).
            self._event(node.id, "overflow", f"{role} {mode} ended on {result.end_reason}")
            self._set_status(node.id, REDO, "too_big", bump_redo=True)
        else:
            self._set_status(node.id, GOOD, None)
            self._note(node, f"the {role} added nothing under node {node.id}; treated as small enough")

    # ---------------------------------------------------------------- episodes

    def _episode(self, role: str, mode: str, node: Optional[Leaf], reason: str = "", console=None):
        console = console or self.console
        if console.should_stop():
            return None
        with self._write_lock:
            self.episodes += 1
        nodes = load_nodes(self.engine, self.session_id)
        by_id = {n.id: n for n in nodes}
        if node is None:
            # The goal and the review feedback are the Architect's whole brief,
            # so they're capped only against the episode budget. They used to
            # be cut at 600 characters: on the calc benchmark that dropped the
            # goal's last sentence ("place them under tests/") and the second
            # of two review issues, so both were never planned.
            anchor = ScopeAnchor(role=role, node_id=None, node=f"the goal: {self.goal[:GOAL_MAX_CHARS]}",
                                 finish=f"{ROLE_FINISH_TOOL[role]}(0, summary)",
                                 reason="\n".join(p for p in (reason, self.feedback[:FEEDBACK_MAX_CHARS]
                                                              if mode in ("extend", "continue") else "") if p))
        else:
            anchor = ScopeAnchor(role=role, node_id=node.id, node=node.description, done_when=node.done_when or "",
                                 files=node.files or [], path=path_of(by_id, node),
                                 finish=f"{ROLE_FINISH_TOOL[role]}({node.id}, summary)", reason=reason,
                                 notes=node.notes or "",
                                 references=references_text(self.engine, self.session_id, node.references),
                                 cases=node.cases or [])
        scope_id = node.id if node is not None else None
        impl = {**self.pool_tools(role),
                **self._locked(make_node_tools(self.engine, self.session_id, role, scope_id, self.root)),
                **self._locked(make_runbook_tools(self.engine, self.session_id, role)),
                **self._locked(make_design_tools(self.engine, self.session_id, role)),
                **make_code_tools(self.root),
                **make_evidence_tools(self.engine, self.session_id, self.root, role),
                "finish": self._finish(anchor, role, mode, node)}
        system = build_system_message(anchor, ROLE_PROMPTS[(role, mode)],
                                      [runbook_index(self.engine, self.session_id),
                                       design_index(self.engine, self.session_id)])
        console.set_status(stage=role.capitalize(), task=anchor.node[:140])
        console.display_rule(f"PLANNER · {role.upper()} {mode}" + (f" — node {node.id}" if node is not None else ""))
        result = run_episode(self.llm_for_role(role), console, self.engine, self.session_id,
                             role=role, mode=mode, anchor=anchor, system_message=system,
                             tools=EpisodeTools(role, impl),
                             budget=episode_token_budget(ROLE_ENV_PREFIXES[role]))
        # Evidence files are named by task number, which this episode's nodes
        # may have just set or shifted.
        with self._write_lock:
            sync_evidence_names(self.engine, self.session_id, self.root)
        return result

    def _locked(self, tools: dict) -> dict:
        def guard(fn):
            def call(*args, **kwargs):
                with self._write_lock:
                    return fn(*args, **kwargs)
            return call
        return {name: guard(fn) for name, fn in tools.items()}

    def _finish(self, anchor: ScopeAnchor, role: str, mode: str, node: Optional[Leaf]):
        if role == "architect" and node is None:
            return self._architect_finish(anchor)
        if mode in ("breakdown", "split") and role in ("lead", "task") and self._has_ground_truth():
            return self._cases_finish(anchor, node)
        return make_finish(anchor)

    def _has_ground_truth(self) -> bool:
        """The design has a reference: then every node has its own evidence,
        not only those under a component citing it. Observed on the
        portfolio-dashboard run: the Architect cited the reference on the
        view components only, so the scaffold and the CSV data extracted
        from the original (component 2 and its files) got no evidence."""
        with get_session(self.engine) as db:
            return db.exec(select(DesignEntry).where(DesignEntry.session_id == self.session_id,
                                                     DesignEntry.kind == "reference")).first() is not None

    def _case_problems(self, nodes: List[Leaf]) -> List[str]:
        """Every node of a session with a ground truth has its own case(s)
        with evidence captured: Dev compares each node with them when it's
        done (docs/old_new.md). A deletion has nothing to compare."""
        problems = []
        bare = [n for n in nodes if not n.cases and n.kind not in NO_CASE_KINDS]
        if bare:
            problems.append("a case on node(s) " + ", ".join(str(n.id) for n in bare) + " (cases=[...] with "
                            "update_node, each captured with capture_evidence): every node is compared with its "
                            "own evidence when it's done")
        missing = [c for n in nodes for c in (n.cases or []) if read_evidence(self.root, self.session_id, c) is None]
        if missing:
            problems.append(f"evidence for {', '.join(missing)} (capture_evidence each one; it runs the ground "
                            f"truth for you)")
        return problems

    def _unowned_cases(self) -> List[str]:
        """Evidence captured but on no node: it would never be compared.
        Observed on the first real run: load_holdings.txt sat in the evidence
        folder with no task number."""
        owned = {c for n in load_nodes(self.engine, self.session_id) for c in (n.cases or [])}
        stray = [c for c in list_cases(self.root, self.session_id) if c not in owned]
        if not stray:
            return []
        return [f"a node for the evidence of {', '.join(stray)} (update_node cases=[...] on the node it checks)"]

    def _cases_finish(self, anchor: ScopeAnchor, parent: Leaf):
        """A breakdown in a session with a ground truth leaves every new node
        with its own evidence."""
        plain = make_finish(anchor)

        def finish(node_id: int = 0, summary: str = "") -> str:
            parts = children_of(load_nodes(self.engine, self.session_id), parent.id)
            missing = self._case_problems(parts) + self._unowned_cases()
            if missing:
                return "Error: not finished yet. Still missing: " + "; ".join(missing) + "."
            return plain(node_id, summary)
        return finish

    def _reference_problems(self, nodes: List[Leaf], runbook: dict) -> List[str]:
        """The Architect's part of the ground truth (docs/old_new.md)."""
        with get_session(self.engine) as db:
            design = list(db.exec(select(DesignEntry).where(DesignEntry.session_id == self.session_id,
                                                            DesignEntry.kind.in_(("reference", "assumption")))))
        references = [d for d in design if d.kind == "reference"]
        hint = ground_truth_hint(self.goal, self.root)
        if hint and not references and not any(d.key == "no_ground_truth" for d in design):
            return [f'the ground truth ({hint}): design_set("reference", "<key>", "visual: <the page, mockup or '
                    f'image>; must match / may differ" or "behavioural: <the command, docs, expected output or old '
                    f'program>; must match / may differ"), or, if the goal has none, design_set("assumption", '
                    f'"no_ground_truth", "<why>")']
        missing = []
        cited = {str(r) for n in nodes if n.level == "architect" for r in (n.references or [])}
        if references:
            missing += self._case_problems([n for n in nodes if n.level == "architect" and n.parent_id is None])
            missing += self._unowned_cases()
        for ref in references:
            if not re.match(r"\s*(visual|behaviou?ral)\b", ref.text, re.I):
                missing.append(f'reference {ref.key}\'s text to start with "visual:" or "behavioural:"')
            if f"reference:{ref.key}" not in cited:
                missing.append(f"a component citing reference:{ref.key} in its references (and saying in its notes "
                               f"which part of it that component rebuilds)")
        if any(re.match(r"\s*behaviou?ral\b", r.text, re.I) for r in references):
            for name, what in (("evidence_one", "how to get the ground truth's answer for one input"),
                               ("compare_one", "how to run the NEW code on one input")):
                if name not in runbook:
                    missing.append(f"runbook entry {name}: {what}, with {{input}} or {{input_file}}")
                elif "{input" not in runbook[name]:
                    missing.append(f"an {{input}} or {{input_file}} placeholder in {name}'s command")
        return missing

    def _architect_finish(self, anchor: ScopeAnchor):
        """The Architect's finish refuses until the base it owns is complete:
        at least one node, the runbook entries later roles run, and a test
        framework in the stack. Observed on the stui run: after two
        continuations the plan had its components but the runbook was only
        setup/dev/build/stop (no test, test_one or e2e, which Dev's gate and
        the reviewer need) and the stack ruled out unit tests."""
        plain = make_finish(anchor)

        def finish(node_id: int = 0, summary: str = "") -> str:
            missing = []
            if not load_nodes(self.engine, self.session_id):
                missing.append("the plan has no nodes yet (add_node every component)")
            with get_session(self.engine) as db:
                entries = list(db.exec(select(RunbookEntry).where(RunbookEntry.session_id == self.session_id)))
                runbook = {r.name: r.command for r in entries}
                notes = {r.name: r.notes for r in entries}
                stack = db.exec(select(DesignEntry).where(DesignEntry.session_id == self.session_id,
                                                          DesignEntry.kind == "stack")).first()
                contracts = db.exec(select(DesignEntry).where(DesignEntry.session_id == self.session_id,
                                                              DesignEntry.kind == "contract")).first()
                outline = db.exec(select(DesignEntry).where(DesignEntry.session_id == self.session_id,
                                                            DesignEntry.kind == "outline")).first()
            if outline is not None:
                # A document goal (G4): no code, so no runbook, test framework
                # or contracts -- the outline is what Lead, Task and the
                # reviewer work from.
                if missing:
                    return "Error: not finished yet. Still missing: " + "; ".join(missing) + ". Add them, then finish."
                return plain(node_id, summary)
            absent = [name for name in REQUIRED_RUNBOOK if name not in runbook]
            if absent:
                missing.append(f"runbook entries {', '.join(absent)} (runbook_set)")
            if "test_one" in runbook and "{test_id}" not in runbook["test_one"]:
                missing.append("a {test_id} placeholder in test_one's command")
            elif "test_one" in runbook and not notes.get("test_one"):
                missing.append("test_one's notes with one example test id (Dev passes only the id)")
            if "script" in runbook and "{file}" not in runbook["script"]:
                missing.append("a {file} placeholder in script's command")
            nodes = load_nodes(self.engine, self.session_id)
            if "entry" in runbook:
                missing += self._entry_problems(runbook["entry"], nodes)
            missing += self._reference_problems(nodes, runbook)
            top_level = [n for n in nodes if n.parent_id is None]
            if len(top_level) >= 3 and not contracts:
                # Observed on the stui run: 16 components and no contract, so
                # each Lead (who sees one node) had no way to know the others.
                missing.append('the contracts between components (design_set("contract", "a->b", ...))')
            if stack is None:
                missing.append('the stack (design_set("stack", "stack", ...))')
            elif re.search(r"\bno (unit[- ]?)?test", stack.text, re.I):
                missing.append("a test framework in the stack: every Dev item is one function plus its unit test")
            if missing:
                return "Error: not finished yet. Still missing: " + "; ".join(missing) + ". Add them, then finish."
            return plain(node_id, summary)
        return finish

    def _entry_problems(self, entry: str, nodes: List[Leaf]) -> List[str]:
        """The runbook's entry names the file(s) the app starts from; each must
        be built by some plan node, or already be in the project (extending an
        existing app whose entry point stays as it is)."""
        files = [repo_path(p) for p in _PATH.findall(entry)]
        if not files:
            return ["the entry file in entry's command (the file the app starts from, e.g. src/main.js)"]
        owned = {repo_path(f) for n in nodes for f in (n.files or [])}
        unowned = [f for f in files if f not in owned
                   and not ((self.root / f).is_file() and (self.root / f).stat().st_size > 0)]
        if not unowned:
            return []
        return [f"a plan node that builds the entry point {', '.join(unowned)}: add_node it as its own component "
                f"-- wire the components together and start the app -- with files=[{', '.join(unowned)}] and "
                f"depends_on the components it imports"]

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
