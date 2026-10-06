"""v2 implementation (laya_plan.md §6): one fresh, short Dev episode per GOOD
leaf, never one long conversation.

    setup      the runbook's `setup` runs once first; if it fails, one Dev
               episode fixes it
    the queue  JFI.imp.queue.next_leaf, one episode each; the leaf is done
               only when mark_leaf_done's own test run passes (the gate)
    finish-up  `build` once, plus the JFI: marker scan; one Dev episode for
               whatever's left, the rest goes to the reviewer notes

Recovery:
- the gate's test hit ANOTHER stub's NotImplementedError: a missing
  dependency, not a bug -- the leaf is re-queued after the leaf that owns
  that symbol (G6);
- an episode that ended without finishing (a crash, stop, restart) starts
  over fresh, told a previous attempt may have partially edited its files;
  at MAX_DEV_ATTEMPTS it's treated like an overflow (G13);
- overflow (the budget or turn cap ran out): the leaf goes back to Task for
  a split and the planner settles the pieces (§5.3, D19). A leaf the split
  can't break up is skipped with a reviewer note -- every leaf must reach a
  finished state (D27).
"""

import os
import re
import subprocess
import time
from dataclasses import dataclass
from datetime import timezone
from pathlib import Path
from typing import Callable, Dict, Optional

from sqlmodel import select

from JFI.episode.brief import ScopeAnchor, build_system_message
from JFI.episode.budget import episode_token_budget
from JFI.episode.engine import run_episode
from JFI.episode.roles import ROLE_ENV_PREFIXES
from JFI.episode.tools import EpisodeTools
from JFI.imp.prompts import FINISH_UP, PARTIAL_ATTEMPT, SETUP, WRAP_UP, WRAP_UP_REASON, dev_prompt
from JFI.imp.queue import FINISHED, dev_leaves, next_leaf
from JFI.models import Leaf, PlanEvent, RunbookEntry, build_indexes, display_number, get_session
from JFI.models._util import utcnow
from JFI.models.enums import LeafStatus
from JFI.planner.nodes import BREAKDOWN, children_of, load_nodes, path_of, target_symbol
from JFI.tool.checkpoint_tools import checkpoint, ensure_baseline
from JFI.tool.code_tools import DOC_SUFFIXES, _symbols, make_code_tools, resolve_path, scan_markers
from JFI.tool.design_tools import design_index, make_design_tools, references_text
from JFI.tool.evidence_tools import compare_cases, evidence_hash, make_evidence_tools
from JFI.tool.note_tools import add_reviewer_note
from JFI.tool.plan_db_tools import plan_status_fields
from JFI.tool.result_cap import cap_result
from JFI.tool.runbook_tools import make_runbook_tools, runbook_index, runbook_set

DEFAULT_MAX_DEV_ATTEMPTS = 3
WRAP_UP_TURNS = 3
MAX_DEFERRALS = 3  # two leaves whose tests each reach the other's stub would otherwise swap forever
COMMAND_TIMEOUT_SECONDS = 600
OUTPUT_TAIL_CHARS = 4000

MARK_LEAF_DONE_SCHEMA = {"type": "function", "function": {
    "name": "mark_leaf_done",
    "description": ("Finish the leaf in SCOPE. Runs its proof first: test_id = your unit test's id, run with the "
                    "runbook's test_one; or check = a shell command that must exit 0 (for leaves without a unit "
                    "test). Marks the leaf done only if it passes; otherwise returns the output to fix."),
    "parameters": {"type": "object", "properties": {
        "leaf_id": {"type": "integer"},
        "summary": {"type": "string", "description": "One line: what you did."},
        "test_id": {"type": "string", "description": "e.g. tests/test_ops.py::test_add"},
        "check": {"type": "string"},
        "accept_difference": {"type": "string", "description": (
            "compare leaves only: why a remaining VISUAL difference from the evidence is intended (it's "
            "recorded for the reviewer). Never for a behavioural mismatch.")},
    }, "required": ["leaf_id", "summary"]},
}}


def max_dev_attempts() -> int:
    try:
        return max(1, int(os.environ.get("MAX_DEV_ATTEMPTS", DEFAULT_MAX_DEV_ATTEMPTS)))
    except ValueError:
        return DEFAULT_MAX_DEV_ATTEMPTS


@dataclass
class ImpResult:
    complete: bool
    episodes: int
    reason: str = ""


def run_command(command: str, root: Path) -> tuple[int, str]:
    # No .pyc files: a one-character fix written within the same second as
    # the broken version has the same size and mtime, so Python would keep
    # running the cached bytecode of the broken one and the gate would
    # refuse a correct fix.
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    try:
        proc = subprocess.run(command, shell=True, cwd=root, env=env, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=COMMAND_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        return 124, f"(timed out after {COMMAND_TIMEOUT_SECONDS}s)"
    output = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode, output[-OUTPUT_TAIL_CHARS:]


_NATIVE_FRAME = re.compile(r', in (\w+)\s*$', re.MULTILINE)
_PYTEST_RAISE = re.compile(r'^(\S+?):(\d+): NotImplementedError\s*$', re.MULTILINE)


def not_implemented_symbol(output: str, root: Optional[Path] = None) -> Optional[str]:
    """The function whose stub raised NotImplementedError, or None when the
    failure is anything else. A native traceback names it in its last frame;
    pytest's default traceback only gives the raising file:line, which is
    mapped back to the top-level symbol containing it."""
    if "NotImplementedError" not in output:
        return None
    raised = _PYTEST_RAISE.findall(output)
    if raised and root is not None:
        path, line = raised[-1]
        target, error = resolve_path(root, path.replace("\\", "/"))
        if not error and target.is_file():
            index = int(line) - 1
            for name, start, end in _symbols(target, target.read_text(encoding="utf-8")):
                if start <= index < end:
                    return name
    frames = _NATIVE_FRAME.findall(output.split("NotImplementedError", 1)[0])
    return frames[-1] if frames else None


_WORDS_WANTED = re.compile(r"(\d+)\s*(?:-\s*\d+\s*)?words", re.I)


def _passage_counts(document: Path) -> tuple[int, int]:
    """(placeholders left, words) in a document: what a passage leaf's gate
    compares against the snapshot taken when its episode started."""
    text = document.read_text(encoding="utf-8")
    placeholders = sum(1 for line in text.splitlines() if "JFI:" in line and "JFI-FILE:" not in line)
    return placeholders, len(re.findall(r"\b\w+\b", re.sub(r"<!--.*?-->", "", text, flags=re.S)))


def _passage_problem(document: Path, before: tuple[int, int], done_when: str) -> Optional[str]:
    """The document path's mechanical gate (G4): one placeholder fewer, and at
    least 80% of the length done_when asks for added."""
    placeholders, words = _passage_counts(document)
    if placeholders >= before[0]:
        return f"its JFI: placeholder is still in {document.name} -- replace that line with the passage"
    wanted = _WORDS_WANTED.search(done_when)
    added = words - before[1]
    if wanted and added < 0.8 * int(wanted.group(1)):
        return f"it adds about {added} words; done_when asks for {wanted.group(1)}"
    return None


def _test_id_problem(test_id: str, test_one) -> Optional[str]:
    """A test_id that is really a command or a file path. Observed on the QA
    machine: Dev passed "npx vitest run __tests__/loader.test.js", the gate
    put it into test_one ("npx vitest run __tests__/{test_id}.test.js") and
    ran "npx vitest run __tests__/npx vitest run __tests__/loader.test.js.test.js"."""
    template = test_one.command
    if "{test_id}" not in template:
        return None
    before, after = template.split("{test_id}", 1)
    prefix = before.split()[-1] if before and not before.endswith(" ") else ""
    suffix = after.split()[0] if after and not after.startswith(" ") else ""
    fixed = test_id.strip()
    if " " in fixed:
        fixed = fixed.split()[-1]
    if prefix and fixed.startswith(prefix):
        fixed = fixed[len(prefix):]
    if suffix and fixed.endswith(suffix):
        fixed = fixed[:-len(suffix)]
    if fixed == test_id:
        return None
    notes = f" Its notes: {test_one.notes.rstrip('. ')}." if test_one.notes else ""
    return (f"Error: test_id is only the id that goes into test_one's {{test_id}}: `{template}`.{notes} "
            f"You passed {test_id!r}; try test_id={fixed!r}.")


def _epoch(moment) -> Optional[float]:
    """A stored (naive UTC, see JFI.models._util) datetime as time.time()."""
    return moment.replace(tzinfo=timezone.utc).timestamp() if moment else None


class Imp:
    def __init__(self, console, engine, session_id: str, root: Path, llm, base_tools: Dict[str, Callable],
                 replan: Callable[[], object]):
        """`base_tools`: the v1 tools Dev reuses (write_file, replace_in_file,
        execute_command, ...). `replan`: runs the planner once more, to
        settle a leaf sent back for a split."""
        self.console, self.engine, self.session_id = console, engine, session_id
        self.root, self.llm, self.base_tools, self.replan = Path(root), llm, base_tools, replan
        self.deferred: Dict[int, int] = {}
        self.deferrals: Dict[int, int] = {}
        self.episodes = 0

    # ---------------------------------------------------------------- run

    def run(self) -> ImpResult:
        ensure_baseline(self.root, self.session_id)
        if not self._setup():
            return ImpResult(False, self.episodes, "stopped")
        while True:
            if self.console.should_stop():
                return ImpResult(False, self.episodes, "stopped")
            self._requeue_changed_evidence()
            leaf = next_leaf(load_nodes(self.engine, self.session_id), self.deferred)
            if leaf is None:
                break
            if not self._work(leaf):
                return ImpResult(False, self.episodes, "the model stopped responding")
        self._finish_up()
        left = [n for n in dev_leaves(load_nodes(self.engine, self.session_id)) if n.status not in FINISHED]
        return ImpResult(not left, self.episodes, f"{len(left)} leaves unfinished" if left else "")

    def _requeue_changed_evidence(self) -> None:
        """A compare leaf that passed is checked again when its evidence was
        edited or re-captured since (by a person, or the dashboard)."""
        for leaf in dev_leaves(load_nodes(self.engine, self.session_id)):
            if leaf.kind != "compare" or leaf.status != LeafStatus.DONE or not leaf.evidence_hash:
                continue
            if evidence_hash(self.root, leaf.cases or []) == leaf.evidence_hash:
                continue
            with get_session(self.engine) as db:
                row = db.get(Leaf, leaf.id)
                row.status, row.started_at, row.ended_at, row.attempt_count = LeafStatus.TODO, None, None, 0
                row.fix_note = (f"the evidence for {', '.join(leaf.cases or [])} changed after this leaf passed "
                                f"(edited or re-captured): compare again and fix the code if it no longer matches")
                db.add(row)
                db.commit()
            self._event(leaf.id, "reopen", "evidence changed")
            self.console.display_system(f"Evidence for {', '.join(leaf.cases or [])} changed: leaf {leaf.id} "
                                        f"is compared again.")

    def _work(self, leaf: Leaf) -> bool:
        if leaf.attempt_count >= max_dev_attempts():
            self._event(leaf.id, "overflow", f"{leaf.attempt_count} attempts without finishing")
            self._split(leaf)
            return True
        partial = leaf.started_at is not None
        with get_session(self.engine) as db:
            row = db.get(Leaf, leaf.id)
            row.attempt_count += 1
            row.started_at = row.started_at or utcnow()
            db.add(row)
            db.commit()
        self.deferred.pop(leaf.id, None)

        title = self._title(leaf)
        result = self._leaf_episode(leaf, partial, title)
        with get_session(self.engine) as db:
            row = db.get(Leaf, leaf.id)
            row.tokens = (row.tokens or 0) + result.tokens
            db.add(row)
            db.commit()
        self.console.set_status(**plan_status_fields(self.engine, self.session_id))
        after = self._leaf(leaf.id)
        if after.status == LeafStatus.DONE:
            self.console.record_task_tokens(title, after.tokens or 0, _epoch(after.started_at),
                                            _epoch(after.ended_at), phase="imp")
        if leaf.id in self.deferred:
            with get_session(self.engine) as db:
                row = db.get(Leaf, leaf.id)
                row.attempt_count -= 1  # waiting for another leaf isn't a failed attempt
                db.add(row)
                db.commit()
            return True
        if after.status == LeafStatus.DONE:
            return True
        if result.end_reason in ("budget", "turn_cap"):
            # Observed on the calc run: the leaf's test had passed, but the
            # episode ended on the turn cap before mark_leaf_done, and the
            # finished leaf was re-split (~7 minutes). One short wrap-up first.
            self._wrap_up(after, title, result.end_reason)
            after = self._leaf(leaf.id)
            if after.status == LeafStatus.DONE:
                self.console.record_task_tokens(title, after.tokens or 0, _epoch(after.started_at),
                                                _epoch(after.ended_at), phase="imp")
                return True
            self._event(leaf.id, "overflow", f"dev ended on {result.end_reason}")
            self._split(after)
            return True
        return result.end_reason != "error"

    def _wrap_up(self, leaf: Leaf, title: str, reason: str) -> None:
        nodes = load_nodes(self.engine, self.session_id)
        by_id = {n.id: n for n in nodes}
        anchor = ScopeAnchor(role="dev", node_id=leaf.id, node=leaf.description, done_when=leaf.done_when or "",
                             files=leaf.files or [], path=path_of(by_id, leaf),
                             reason=WRAP_UP_REASON.format(reason=reason), notes=leaf.notes or "",
                             references=references_text(self.engine, self.session_id, leaf.references),
                             finish=f"mark_leaf_done({leaf.id}, summary, test_id=... or check=...)")
        result = self._episode(anchor, WRAP_UP, self._mark_leaf_done(leaf), "wrap-up", task=title,
                               max_turns=WRAP_UP_TURNS)
        with get_session(self.engine) as db:
            row = db.get(Leaf, leaf.id)
            row.tokens = (row.tokens or 0) + result.tokens
            db.add(row)
            db.commit()

    def _split(self, leaf: Leaf) -> None:
        if leaf.kind == "compare":
            # A comparison can't be split into smaller ones; what doesn't match
            # goes to the reviewer, who can reopen the code's own leaf.
            with get_session(self.engine) as db:
                row = db.get(Leaf, leaf.id)
                row.status, row.ended_at = LeafStatus.SKIPPED, utcnow()
                db.add(row)
                db.commit()
            add_reviewer_note(self.engine, self.session_id,
                              f"Dev: compare leaf {leaf.id} ({leaf.description}) still doesn't match its evidence "
                              f"({', '.join(leaf.cases or [])}) after {leaf.attempt_count} attempt(s); skipped.")
            return
        with get_session(self.engine) as db:
            row = db.get(Leaf, leaf.id)
            row.plan_status = BREAKDOWN
            db.add(row)
            db.commit()
        self.replan()
        if not children_of(load_nodes(self.engine, self.session_id), leaf.id):
            with get_session(self.engine) as db:
                row = db.get(Leaf, leaf.id)
                row.status, row.ended_at = LeafStatus.SKIPPED, utcnow()
                db.add(row)
                db.commit()
            add_reviewer_note(self.engine, self.session_id,
                              f"Dev: leaf {leaf.id} ({leaf.description}) couldn't be finished or split; skipped.")

    # ---------------------------------------------------------------- the gate

    def _mark_compare_done(self, leaf: Leaf):
        """A compare leaf is done when the new code matches its cases'
        evidence (docs/old_new.md) -- checked here, not claimed."""
        cases = list(leaf.cases or [])
        start_hash = evidence_hash(self.root, cases)

        def mark_leaf_done(leaf_id: int, summary: str = "", test_id: Optional[str] = None,
                           check: Optional[str] = None, accept_difference: str = "") -> str:
            if int(leaf_id) != leaf.id:
                return f"Error: this conversation is about leaf {leaf.id}, not {leaf_id}."
            if not cases:
                return "Error: this compare leaf names no case; add_reviewer_note it and stop."
            if evidence_hash(self.root, cases) != start_hash:
                return ("Error: evidences/ changed while you worked on this leaf. The evidence is the ground truth: "
                        "put it back as it was (git checkout it, or ask in add_reviewer_note) and change the code "
                        "instead.")
            results = compare_cases(self.engine, self.session_id, self.root, cases)
            bad = [r for r in results if not r.ok]
            report = "\n\n".join(r.report for r in results)
            if not bad:
                self._finish_leaf(leaf.id, evidence_hash=start_hash)
                return f"Done: leaf {leaf.id} matches its evidence.\n{report}"
            blocker = self._missing_dependency(leaf, "\n".join(r.output for r in bad))
            if blocker is not None and self.deferrals.get(leaf.id, 0) < MAX_DEFERRALS:
                self.deferrals[leaf.id] = self.deferrals.get(leaf.id, 0) + 1
                self.deferred[leaf.id] = blocker.id
                self._event(leaf.id, "skip", f"waits for leaf {blocker.id}: its stub raised NotImplementedError")
                return (f"Deferred: the new code reached {not_implemented_symbol(bad[0].output, self.root)}(), which "
                        f"is still a stub (leaf {blocker.id} implements it). This leaf is retried after it.")
            accept_difference = (accept_difference or "").strip()
            if accept_difference and all(r.visual for r in bad):
                add_reviewer_note(self.engine, self.session_id,
                                  f"Dev: leaf {leaf.id} accepted a visual difference from its evidence "
                                  f"({', '.join(r.case for r in bad)}): {accept_difference}")
                self._finish_leaf(leaf.id, evidence_hash=start_hash)
                return f"Done: leaf {leaf.id}, with the visual difference recorded for the reviewer."
            return cap_result(f"Error: the new code doesn't match the evidence yet. Fix the code in "
                              f"{', '.join(leaf.files or []) or 'its files'} and call mark_leaf_done again.\n"
                              f"{report}", "Only the end of the output is shown.")
        return mark_leaf_done

    def _mark_leaf_done(self, leaf: Leaf):
        if leaf.kind == "compare":
            return self._mark_compare_done(leaf)
        document = self._document(leaf)
        before = _passage_counts(document) if document else None

        def mark_leaf_done(leaf_id: int, summary: str = "", test_id: Optional[str] = None,
                           check: Optional[str] = None, accept_difference: str = "") -> str:
            if int(leaf_id) != leaf.id:
                return f"Error: this conversation is about leaf {leaf.id}, not {leaf_id}."
            if leaf.kind == "passage" and document and not test_id and not check:
                problem = _passage_problem(document, before, leaf.done_when or "")
                if problem:
                    return f"Error: the passage isn't done: {problem}."
                self._finish_leaf(leaf.id)
                return f"Done: leaf {leaf.id} (the passage is written)."
            if test_id:
                test_one = self._runbook("test_one")
                if test_one is None:
                    return ('Error: the runbook has no test_one entry. runbook_set("test_one", "<command with '
                            '{test_id}>") for this stack, or pass check="<command>" instead.')
                problem = _test_id_problem(test_id, test_one)
                if problem:
                    return problem
                command = test_one.command.replace("{test_id}", test_id)
            elif check:
                command = check
            else:
                return "Error: pass test_id (the unit test you wrote) or check (a command proving done_when)."
            code, output = run_command(command, self.root)
            if code == 0:
                self._finish_leaf(leaf.id)
                if test_id:
                    self._verify("test_one")
                return f"Done: leaf {leaf.id} ({command} passed)."
            blocker = self._missing_dependency(leaf, output)
            if blocker is not None and self.deferrals.get(leaf.id, 0) < MAX_DEFERRALS:
                self.deferrals[leaf.id] = self.deferrals.get(leaf.id, 0) + 1
                self.deferred[leaf.id] = blocker.id
                self._event(leaf.id, "skip", f"waits for leaf {blocker.id}: its stub raised NotImplementedError")
                return (f"Deferred: the test reached {not_implemented_symbol(output, self.root)}(), which is still a stub "
                        f"(leaf {blocker.id} implements it). Nothing to fix here; this leaf is retried after it.")
            return cap_result(f"Error: `{command}` failed (exit {code}). Fix it and call mark_leaf_done again.\n"
                              f"{output}", "Only the end of the output is shown.")
        return mark_leaf_done

    def _document(self, leaf: Leaf) -> Optional[Path]:
        for name in leaf.files or []:
            target, error = resolve_path(self.root, name)
            if not error and target.suffix.lower() in DOC_SUFFIXES and target.is_file():
                return target
        return None

    def _missing_dependency(self, leaf: Leaf, output: str) -> Optional[Leaf]:
        """The unfinished leaf that implements the stub the test ran into.
        Leaves are matched by the function they're FOR -- the first `name(`
        in "implement name(...) in file: ..." -- since a description also
        mentions the helpers it calls ("sum a list with add()")."""
        symbol = not_implemented_symbol(output, self.root)
        if symbol is None or target_symbol(leaf.description) == symbol:
            return None  # its own stub: the leaf just isn't implemented yet
        return next((n for n in dev_leaves(load_nodes(self.engine, self.session_id))
                     if n.id != leaf.id and n.status not in FINISHED
                     and target_symbol(n.description) == symbol), None)

    def _finish_leaf(self, leaf_id: int, evidence_hash: Optional[str] = None) -> None:
        with get_session(self.engine) as db:
            row = db.get(Leaf, leaf_id)
            row.status, row.ended_at, row.fix_note = LeafStatus.DONE, utcnow(), None
            if evidence_hash:
                row.evidence_hash = evidence_hash
            row.checkpoint = checkpoint(self.root, self.session_id, f"leaf {leaf_id}: {row.description}")                 or row.checkpoint
            db.add(row)
            db.commit()

    # ---------------------------------------------------------------- episodes

    def _title(self, leaf: Leaf) -> str:
        """"1.2.3 description": the dashboards find the current leaf, and
        file its tokens and time, by that leading number."""
        by_id, siblings = build_indexes(load_nodes(self.engine, self.session_id))
        return f"{display_number(by_id[leaf.id], by_id, siblings)} {leaf.description}"

    def _leaf_episode(self, leaf: Leaf, partial: bool, title: str):
        nodes = load_nodes(self.engine, self.session_id)
        by_id = {n.id: n for n in nodes}
        why = []
        if leaf.fix_note:
            why.append(f"the review found a problem here: {leaf.fix_note}")
        if partial:
            why.append(PARTIAL_ATTEMPT.format(files=", ".join(leaf.files or []) or "its files"))
        anchor = ScopeAnchor(role="dev", node_id=leaf.id, node=leaf.description, done_when=leaf.done_when or "",
                             files=leaf.files or [], path=path_of(by_id, leaf), reason=" ".join(why),
                             notes=leaf.notes or "",
                             references=references_text(self.engine, self.session_id, leaf.references),
                             cases=leaf.cases or [],
                             finish=f"mark_leaf_done({leaf.id}, summary"
                                    + (")" if leaf.kind == "compare" else ", test_id=... or check=...)"))
        return self._episode(anchor, dev_prompt(leaf.kind, leaf.description), self._mark_leaf_done(leaf), "leaf",
                             task=title)

    def _chore(self, mode: str, prompt: str, why: str, entry: Optional[str]):
        """A leafless Dev episode (setup, finish-up) whose mark_leaf_done
        re-runs the runbook's `entry` command as its proof. Read afresh on
        every call: fixing the command itself (runbook_set) is often the fix,
        and a command captured at the start kept failing after the real
        run's Dev had corrected it, until the episode hit its turn cap."""
        def mark_leaf_done(leaf_id: int = 0, summary: str = "", test_id: Optional[str] = None,
                           check: Optional[str] = None, accept_difference: str = "") -> str:
            row = self._runbook(entry) if entry else None
            if row is None:
                return "Done."
            command = row.command
            code, output = run_command(command, self.root)
            if code == 0:
                return f"Done: `{command}` passed."
            return cap_result(f"Error: `{command}` still fails (exit {code}).\n{output}",
                              "Only the end of the output is shown.")
        anchor = ScopeAnchor(role="dev", node_id=None, node=f"{mode}: {why[:200]}", reason=why,
                             finish="mark_leaf_done(0, summary)")
        return self._episode(anchor, prompt, mark_leaf_done, mode)

    def _episode(self, anchor: ScopeAnchor, prompt: str, finish_tool: Callable, mode: str,
                 task: Optional[str] = None, max_turns: Optional[int] = None):
        self.episodes += 1
        impl = {**self.base_tools,
                **make_runbook_tools(self.engine, self.session_id, "dev"),
                **make_design_tools(self.engine, self.session_id, "dev"),
                **make_code_tools(self.root),
                **make_evidence_tools(self.engine, self.session_id, self.root, "dev"),
                "add_reviewer_note": lambda text: add_reviewer_note(self.engine, self.session_id, f"Dev: {text}"),
                "mark_leaf_done": finish_tool}
        system = build_system_message(anchor, prompt, [runbook_index(self.engine, self.session_id),
                                                       design_index(self.engine, self.session_id)])
        self.console.set_status(stage="Dev", task=(task or anchor.node)[:140], task_started_at=time.time())
        self.console.display_rule(f"DEV · {mode}" + (f" — leaf {anchor.node_id}" if anchor.node_id else ""))
        return run_episode(self.llm, self.console, self.engine, self.session_id, role="dev", mode=mode,
                           anchor=anchor, system_message=system,
                           tools=EpisodeTools("dev", impl, extra_schemas=[MARK_LEAF_DONE_SCHEMA]),
                           budget=episode_token_budget(ROLE_ENV_PREFIXES["dev"]), max_turns=max_turns)

    # ---------------------------------------------------------------- setup / finish-up

    def _setup(self) -> bool:
        setup = self._runbook("setup")
        if setup is None or setup.verified:
            return True
        code, output = run_command(setup.command, self.root)
        if code != 0:
            if self.console.should_stop():
                return False
            self._chore("setup", SETUP, f"`{setup.command}` failed (exit {code}): {output[-1500:]}", "setup")
            setup = self._runbook("setup")
            code, _ = run_command(setup.command, self.root)
        if code == 0:
            self._verify("setup")
        else:
            add_reviewer_note(self.engine, self.session_id, f"Dev: the runbook's setup (`{setup.command}`) fails.")
        return True

    def _finish_up(self) -> None:
        build = self._runbook("build")
        problems = []
        if build is not None:
            code, output = run_command(build.command, self.root)
            if code != 0:
                problems.append(f"`{build.command}` fails (exit {code}): {output[-1500:]}")
        markers = scan_markers(self.root)
        if markers:
            problems.append("leftover markers: " + "; ".join(f"{p}:{no} {m}" for p, no, m in markers[:30]))
        if not problems or self.console.should_stop():
            return
        self._chore("finish-up", FINISH_UP, " | ".join(problems), "build" if build else None)
        build = self._runbook("build")
        if build is not None:
            code, _ = run_command(build.command, self.root)
            if code == 0:
                self._verify("build")
            else:
                add_reviewer_note(self.engine, self.session_id, f"Dev: `{build.command}` still fails after imp.")
        left = scan_markers(self.root)
        if left:
            add_reviewer_note(self.engine, self.session_id, "Dev: unfinished markers after imp: "
                              + "; ".join(f"{p}:{no} {m}" for p, no, m in left[:30]))

    # ---------------------------------------------------------------- DB helpers

    def _runbook(self, name: str) -> Optional[RunbookEntry]:
        with get_session(self.engine) as db:
            return db.exec(select(RunbookEntry).where(RunbookEntry.session_id == self.session_id,
                                                      RunbookEntry.name == name)).first()

    def _verify(self, name: str) -> None:
        entry = self._runbook(name)
        if entry is not None and not entry.verified:
            runbook_set(self.engine, self.session_id, name, entry.command, entry.notes, True, "dev")

    def _leaf(self, leaf_id: int) -> Leaf:
        with get_session(self.engine) as db:
            return db.get(Leaf, leaf_id)

    def _event(self, node_id: Optional[int], kind: str, detail: str) -> None:
        with get_session(self.engine) as db:
            db.add(PlanEvent(session_id=self.session_id, node_id=node_id, type=kind, detail=detail))
            db.commit()
