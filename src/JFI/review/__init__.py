"""The reviewer and cleanup as scoped episodes (laya_plan.md §7, G8, G12, G19).

    reviewer   one episode: run the runbook's e2e, re-check the Dev notes and
               leftover markers, then one verdict --
                 pass     finish re-runs the e2e itself (never a claimed pass);
                          every leaf's review_status becomes "passed" and the
                          runbook entries it ran are marked verified;
                 fix      reopen_leaf(leaf_id, fix_note) on the leaf that owns
                          the bug: Dev fixes only that leaf, then the reviewer
                          runs again (the G8 fix path, no re-planning);
                 missing  write_review_report: the run's next iteration sends
                          it to the Architect in extend mode.
    cleanup    one episode: tidy the working directory.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional

from sqlmodel import select

from JFI.episode.brief import ScopeAnchor, build_system_message
from JFI.episode.budget import episode_token_budget
from JFI.episode.engine import run_episode
from JFI.episode.roles import ROLE_ENV_PREFIXES
from JFI.episode.tools import EpisodeTools
from JFI.imp.dev import run_command
from JFI.imp.queue import dev_leaves
from JFI.models import DesignEntry, Leaf, PlanEvent, RunbookEntry, get_session
from JFI.models.enums import LeafStatus
from JFI.planner.nodes import load_nodes
from JFI.review.prompts import CLEANUP, REVIEW_CONTINUE, REVIEWER
from JFI.tool.checkpoint_tools import make_checkpoint_tools, revert_leaf
from JFI.tool.code_tools import list_dir, read_file_range, scan_markers, search_code
from JFI.tool.evidence_tools import compare_cases, list_cases, make_evidence_tools, read_evidence, sync_evidence_names
from JFI.tool.note_tools import REVIEW_REPORT, get_note
from JFI.tool.plan_db_tools import plan_status_fields
from JFI.tool.result_cap import cap_result
from JFI.tool.runbook_tools import make_runbook_tools, runbook_index, runbook_set

REOPEN_LEAF_SCHEMA = {"type": "function", "function": {
    "name": "reopen_leaf",
    "description": ("Send one finished leaf back to Dev with a fix note: for a bug in code that was built. Dev fixes "
                    "only that leaf, then the review runs again."),
    "parameters": {"type": "object", "properties": {
        "leaf_id": {"type": "integer"},
        "fix_note": {"type": "string", "description": "The failing step, expected vs actual, and where."},
        "revert": {"type": "boolean", "description": ("true: first undo the leaf's change (its files go back "
                                                       "to before it), so Dev rebuilds it from scratch instead "
                                                       "of patching a wrong approach. Refused if a later leaf "
                                                       "changed the same files.")},
    }, "required": ["leaf_id", "fix_note"]},
}}

PASSED, FAILED = "passed", "failed"


@dataclass
class ReviewResult:
    verdict: str  # "pass" / "fix" / "missing" / "stopped"
    reopened: List[int]


class Reviewer:
    def __init__(self, console, engine, session_id: str, root: Path, llm, base_tools: Dict[str, Callable]):
        """`base_tools`: the session-bound tools the reviewer reuses
        (execute_command, start/stop_background_process, get_plan,
        get_reviewer_notes, write_review_report)."""
        self.console, self.engine, self.session_id = console, engine, session_id
        self.root, self.llm, self.base_tools = Path(root), llm, base_tools

    def run(self) -> ReviewResult:
        reopened: List[int] = []
        report_before = get_note(self.engine, self.session_id, REVIEW_REPORT)
        # A budget or turn-cap end gets one fresh episode, like the planner's and
        # Dev's. Observed on the calc run: one oversized command result ended
        # the review over budget after one turn, and that ended the whole run.
        why = self._findings()
        for attempt in range(2):
            result = self._episode(reopened, report_before, why)
            self.console.set_status(**plan_status_fields(self.engine, self.session_id))
            if result.end_reason == "finish":
                break
            if result.end_reason not in ("budget", "turn_cap") or attempt:
                return ReviewResult("stopped", reopened)
            why = " ".join(filter(None, [why, REVIEW_CONTINUE.format(reason=result.end_reason)]))
        if reopened:
            return ReviewResult("fix", reopened)
        if get_note(self.engine, self.session_id, REVIEW_REPORT) not in (None, report_before):
            return ReviewResult("missing", reopened)
        self._mark_passed()
        self.console.set_status(**plan_status_fields(self.engine, self.session_id))
        return ReviewResult("pass", reopened)

    def _episode(self, reopened: List[int], report_before, why: str):
        anchor = ScopeAnchor(role="reviewer", node_id=None,
                             node="the whole project: run the runbook's e2e and give one verdict",
                             done_when="a PASS the e2e confirms, or every problem routed (reopen_leaf / "
                                       "write_review_report)",
                             reason=why, finish="finish(0, summary)")
        impl = {**self.base_tools, **make_runbook_tools(self.engine, self.session_id, "reviewer"),
                "read_file": lambda path, start=None, end=None: read_file_range(self.root, path, start, end),
                # Observed on the calc run: with no search tool the reviewer ran
                # `findstr "JFI:"` over a 4.7 MB log and got 99,395 characters back.
                "search_code": lambda pattern, path=".", regex=False: search_code(self.root, pattern, path, regex),
                "list_dir": lambda path=".": list_dir(self.root, path),
                **make_checkpoint_tools(self.engine, self.session_id, self.root),
                **make_evidence_tools(self.engine, self.session_id, self.root, "reviewer"),
                "reopen_leaf": self._reopen_leaf(reopened),
                "finish": self._finish(reopened, report_before)}
        system = build_system_message(anchor, REVIEWER, [runbook_index(self.engine, self.session_id)])
        self.console.set_status(stage="Review")
        self.console.display_rule("REVIEWER")
        return run_episode(self.llm, self.console, self.engine, self.session_id, role="reviewer", mode="review",
                           anchor=anchor, system_message=system,
                           tools=EpisodeTools("reviewer", impl, extra_schemas=[REOPEN_LEAF_SCHEMA]),
                           budget=episode_token_budget(ROLE_ENV_PREFIXES["reviewer"]))

    def _findings(self) -> str:
        markers = scan_markers(self.root)
        if not markers:
            return ""
        return "leftover markers: " + "; ".join(f"{p}:{no} {m}" for p, no, m in markers[:20])

    def _reopen_leaf(self, reopened: List[int]):
        def reopen_leaf(leaf_id: int, fix_note: str, revert: bool = False) -> str:
            fix_note = (fix_note or "").strip()
            if not fix_note:
                return "Error: reopen_leaf needs a fix_note: the failing step, expected vs actual, and where."
            leaf = next((n for n in dev_leaves(load_nodes(self.engine, self.session_id)) if n.id == int(leaf_id)), None)
            if leaf is None:
                return f"Error: {leaf_id} isn't a leaf Dev builds. get_plan() lists the leaves and their files."
            reverted = ""
            if revert:
                if not leaf.checkpoint:
                    return (f"Error: leaf {leaf.id} has no checkpoint to revert to (git unavailable when it "
                            f"finished). Reopen it without revert.")
                reverted = revert_leaf(self.root, self.session_id, leaf.checkpoint)
                if reverted.startswith("Error"):
                    return reverted
                reverted = " " + reverted
            with get_session(self.engine) as db:
                row = db.get(Leaf, leaf.id)
                row.status, row.started_at, row.ended_at = LeafStatus.TODO, None, None
                row.attempt_count, row.fix_note, row.review_status = 0, fix_note, FAILED
                row.reopened_count += 1
                db.add(row)
                db.add(PlanEvent(session_id=self.session_id, node_id=leaf.id, type="reopen", detail=fix_note[:500]))
                db.commit()
            reopened.append(leaf.id)
            return f"Reopened leaf {leaf.id} ({leaf.description}) for Dev with your fix note.{reverted}"
        return reopen_leaf

    def _evidence_check(self) -> tuple[Optional[str], str]:
        """(a refusal, or None; a note for the pass). Every ground-truth case
        is compared again over the finished build: a later leaf can break
        what an earlier compare leaf checked."""
        sync_evidence_names(self.engine, self.session_id, self.root)
        cases = list_cases(self.root, self.session_id)
        if not cases:
            return None, ""
        results = compare_cases(self.engine, self.session_id, self.root, cases)
        bad = [r for r in results if not r.ok]
        unverified = [c for c in cases if getattr(read_evidence(self.root, self.session_id, c), "unverified", False)]
        note = (f" Checked against generated (LLM, not verified) evidence only: {', '.join(unverified)}."
                if unverified else "")
        if not bad:
            return None, f" All {len(results)} ground-truth case(s) match their evidence.{note}"
        owners = {c: n.id for n in dev_leaves(load_nodes(self.engine, self.session_id))
                  if n.kind == "compare" for c in (n.cases or [])}
        lines = [f"- {r.case} (compare leaf {owners.get(r.case, '?')}):\n{r.report}" for r in bad]
        return (cap_result("Error: not a pass -- the build doesn't match its ground truth. reopen_leaf the compare "
                           "leaf of each failing case (or the leaf whose code is wrong) with what differs:\n"
                           + "\n".join(lines), "Only the end of the output is shown."), note)

    def _finish(self, reopened: List[int], report_before: Optional[str]):
        def finish(node_id: int = 0, summary: str = "") -> str:
            if reopened or get_note(self.engine, self.session_id, REVIEW_REPORT) not in (None, report_before):
                return "Review finished: the problems are routed."
            refusal, evidence_note = self._evidence_check()
            if refusal:
                return refusal
            e2e = self._runbook("e2e")
            if e2e is None and self._is_document():
                # A document goal (G4) has no command to run: the mechanical
                # part of its pass is that no placeholder is left anywhere.
                left = scan_markers(self.root)
                if left:
                    return ("Error: not a pass -- placeholders are left: "
                            + "; ".join(f"{p}:{no} {m}" for p, no, m in left[:20])
                            + ". reopen_leaf the passages they belong to.")
                return "PASS confirmed: no placeholder is left in the document." + evidence_note
            if e2e is None:
                return ("Error: the runbook has no e2e entry, so a pass can't be confirmed. Report it with "
                        "write_review_report as not checked, then finish.")
            code, output = run_command(e2e.command, self.root)
            if code != 0:
                return cap_result(f"Error: not a pass -- the e2e `{e2e.command}` fails (exit {code}). Route it: "
                                  f"reopen_leaf for a bug in built code, write_review_report for missing work.\n"
                                  f"{output}", "Only the end of the output is shown.")
            runbook_set(self.engine, self.session_id, "e2e", e2e.command, e2e.notes, True, "reviewer")
            return f"PASS confirmed: `{e2e.command}` passed." + evidence_note
        return finish

    def _mark_passed(self) -> None:
        with get_session(self.engine) as db:
            for leaf in dev_leaves(load_nodes(self.engine, self.session_id)):
                if leaf.status == LeafStatus.DONE:
                    row = db.get(Leaf, leaf.id)
                    row.review_status = PASSED
                    db.add(row)
            db.commit()

    def _is_document(self) -> bool:
        with get_session(self.engine) as db:
            return db.exec(select(DesignEntry).where(DesignEntry.session_id == self.session_id,
                                                     DesignEntry.kind == "outline")).first() is not None

    def _runbook(self, name: str) -> Optional[RunbookEntry]:
        with get_session(self.engine) as db:
            return db.exec(select(RunbookEntry).where(RunbookEntry.session_id == self.session_id,
                                                      RunbookEntry.name == name)).first()


def run_cleanup(console, engine, session_id: str, root: Path, llm, base_tools: Dict[str, Callable]) -> bool:
    """One cleanup episode. True when it finished."""
    anchor = ScopeAnchor(role="cleanup", node_id=None, node="the working directory: tidy it",
                         done_when="stray non-deliverable files moved into .jfi/ or deleted", finish="finish(0, summary)")
    impl = {"execute_command": base_tools["execute_command"], "list_dir": lambda path=".": list_dir(Path(root), path),
            "finish": lambda node_id=0, summary="": "Cleanup finished."}
    system = build_system_message(anchor, CLEANUP, [])
    console.set_status(stage="Cleanup")
    console.display_rule("CLEANUP")
    result = run_episode(llm, console, engine, session_id, role="cleanup", mode="cleanup", anchor=anchor,
                         system_message=system, tools=EpisodeTools("cleanup", impl),
                         budget=episode_token_budget(ROLE_ENV_PREFIXES["cleanup"]))
    return result.end_reason == "finish"
