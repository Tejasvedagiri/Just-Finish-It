"""Phase 9: the document path (laya_plan.md G4). A goal that produces prose,
not code: the Architect records an outline instead of a runbook and test
framework, Lead scaffolds the Markdown with one placeholder per passage, Task
makes one passage leaf per placeholder, and Dev's gate is mechanical
(placeholder gone, length met). Real SQLite, real files, the planner, Dev and
the reviewer scripted at the console boundary."""
from __future__ import annotations

import json
from pathlib import Path

from JFI.imp.dev import Imp
from JFI.manager.abstract_manager import AbstractManager
from JFI.models import Episode, SessionRecord, get_engine, get_session
from JFI.models.enums import LeafStatus
from JFI.planner.judge import FallbackJudge
from JFI.planner.loop import Planner
from JFI.planner.nodes import load_nodes
from JFI.review import Reviewer
from JFI.tool.file_tools import replace_in_file, write_file

# scaffold_file is used, but only with fill lines (the Markdown placeholders), never code stubs.
CODE_TOOLS = {"read_symbol", "replace_symbol", "list_symbols", "copy_lines", "mark_change", "execute_command"}


class Console(AbstractManager):
    def __init__(self, turns):
        self.turns = list(turns)

    def wait_while_paused(self):
        pass

    def print_agent_response(self, response, prompt_tokens_estimate=0):
        return self.turns.pop(0) if self.turns else {"content": "(no more script)", "tool_calls": None, "usage": None}

    def display_tool_call(self, *a, **k):
        pass

    def display_tool_result(self, *a, **k):
        pass

    def display_system(self, *a, **k):
        pass

    def display_user(self, *a, **k):
        pass

    def display_assistant(self, *a, **k):
        pass

    def get_user_input(self, *a, **k):
        return ""

    def get_user_choice(self, *a, **k):
        return "s"


class LLM:
    def send_message(self, messages, tools=None):
        return object()


_ids = iter(range(10_000))


def turn(name, **args):
    return {"content": None, "usage": None, "tool_calls": [
        {"id": f"c{next(_ids)}", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}


ARRIVAL = ("The keeper climbed the last of the spiral stairs as the sun dropped behind the headland, "
           "lit the great lamp, and watched its beam sweep across the grey water for the first time that season.")
STORM = ("By midnight the storm had found the island. Waves broke over the rocks below, the glass rattled in its "
         "frame, and still the light turned, steady, long after the keeper stopped counting the hours.")


def test_a_story_goal_plans_and_completes_with_no_code_tools(tmp_path):
    engine = get_engine(tmp_path / ".jfi")
    with get_session(engine) as db:
        db.add(SessionRecord(session_id="s", repo_path=str(tmp_path), pipeline_version="v2"))
        db.commit()

    plan_turns = [
        # Architect: an outline, no runbook, no test framework -- finish accepts it.
        turn("design_set", kind="stack", key="stack", text="document: Markdown"),
        turn("design_set", kind="outline", key="outline",
             text="1. Arrival: the keeper lights the lamp, ~40 words. 2. The storm: the light holds, ~40 words."),
        turn("add_node", description="write the story in story.md: arrival and the storm", kind="section",
             files=["story.md"], done_when="both passages written"),
        turn("finish", node_id=0, summary="outline + one section"),
        # Lead: the Markdown file with one placeholder per passage.
        turn("scaffold_file", path="story.md", purpose="The Lighthouse",
             fill=["passage: arrival, the keeper lights the lamp, ~40 words",
                   "passage: the storm, the light holds, ~40 words"]),
        turn("add_node", description="write the passages in story.md", kind="section", files=["story.md"],
             done_when="no placeholder left"),
        turn("finish", node_id=1, summary="scaffolded"),
        # Task: one passage leaf per placeholder.
        turn("add_node", description="write the arrival passage in story.md: the keeper lights the lamp",
             kind="passage", files=["story.md"], done_when="at least 30 words; covers the lamp"),
        turn("add_node", description="write the storm passage in story.md: the light holds", kind="passage",
             files=["story.md"], done_when="at least 30 words; covers the storm"),
        turn("finish", node_id=2, summary="two passages"),
    ]
    result = Planner(Console(plan_turns), engine, "s", "a short story about a lighthouse keeper", tmp_path,
                     lambda role: LLM(), FallbackJudge()).run()
    assert result.complete, result.reason
    placeholder = "<!-- JFI: passage: arrival, the keeper lights the lamp, ~40 words -->"
    assert placeholder in (tmp_path / "story.md").read_text(encoding="utf-8")

    dev_turns = [
        turn("mark_leaf_done", leaf_id=3, summary="too early"),  # refused: the placeholder is still there
        turn("replace_in_file", file_path="story.md", old_string=placeholder, new_string=ARRIVAL),
        turn("mark_leaf_done", leaf_id=3, summary="arrival written"),
        turn("replace_in_file", file_path="story.md",
             old_string="<!-- JFI: passage: the storm, the light holds, ~40 words -->", new_string=STORM),
        turn("mark_leaf_done", leaf_id=4, summary="storm written"),
    ]
    imp = Imp(Console(dev_turns), engine, "s", tmp_path, LLM(),
              {"write_file": write_file, "replace_in_file": replace_in_file}, replan=lambda: None)
    assert imp.run().complete

    leaves = {n.id: n for n in load_nodes(engine, "s")}
    assert leaves[3].status == LeafStatus.DONE and leaves[4].status == LeafStatus.DONE
    story = (tmp_path / "story.md").read_text(encoding="utf-8")
    assert "JFI:" not in story.replace("JFI-FILE:", "") and ARRIVAL in story and STORM in story
    with get_session(engine) as db:
        used = {t for e in db.exec(Episode.__table__.select()).all() for t in (e.tools_used or [])}
    assert not used & CODE_TOOLS, used & CODE_TOOLS

    reviewer = Reviewer(Console([turn("finish", node_id=0, summary="PASS: read against the outline")]), engine, "s",
                        tmp_path, LLM(), {"get_reviewer_notes": lambda: "no notes"})
    assert reviewer.run().verdict == "pass"


def test_the_passage_gate_wants_the_length_done_when_asks_for(tmp_path):
    from JFI.imp.dev import _passage_counts, _passage_problem
    doc = Path(tmp_path / "essay.md")
    doc.write_text("<!-- JFI-FILE: Essay -->\n<!-- JFI: passage: intro, ~50 words -->\n", encoding="utf-8")
    before = _passage_counts(doc)
    doc.write_text("<!-- JFI-FILE: Essay -->\nToo short.\n", encoding="utf-8")
    assert "asks for 50" in _passage_problem(doc, before, "at least 50 words")
    assert _passage_problem(doc, before, "covers the thesis") is None
