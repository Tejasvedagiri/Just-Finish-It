"""The brief every v2 episode starts from (laya_plan.md §0, G1.1).

The SCOPE ANCHOR comes first and is built in code: role, node, done_when,
files, path, what the episode may and must not touch, and how it finishes.
It is part of the system message, which every request in the episode
re-sends unchanged -- so it can never be trimmed away as context grows.
Everything else the role needs it pulls with tools; the brief carries only
the one-line runbook and design indexes (pull, don't push).
"""

from dataclasses import dataclass, field
from typing import Sequence

from JFI.episode.environment import environment_line

ROLE_LABEL = {
    "architect": "Architect (base and design)",
    "lead": "Lead (folders, files and stubs for one component)",
    "task": "Task (implement / integrate tickets for one file)",
    "dev": "Dev (implement one leaf and its unit test)",
    "reviewer": "Reviewer (run the end-to-end test)",
    "cleanup": "Cleanup (tidy the working directory)",
}

ROLE_MAY = {
    "architect": "read the repo; probe the ground truth with read-only commands and capture each component's "
                 "overview of it; add or change top-level plan items; write the design and the runbook",
    "lead": "create this component's folders and files with stubs; add file nodes under this node; capture the "
            "ground truth's evidence for its cases",
    "task": "add implement/integrate/compare leaves under this node; add helper stubs to this node's file",
    "dev": "edit the files listed above; write one unit test; run commands from the runbook",
    "reviewer": "run the app and the e2e scenario from the runbook; read files; report",
    "cleanup": "move or delete stray files outside the deliverable",
}

ROLE_MUST_NOT = {
    "architect": "write code or files; add operational steps (run/stop/test commands) as plan items",
    "lead": "write function bodies; touch other components or other nodes; change the design",
    "task": "write function bodies; touch other files or other nodes; change the design",
    "dev": "change other files' behaviour, other plan nodes, the design or the plan, or anything in evidences/",
    "reviewer": "fix code yourself; change the plan",
    "cleanup": "touch .git, .jfi/, evidences/, the deliverable's source/tests/docs, or anything you're unsure about",
}


@dataclass
class ScopeAnchor:
    role: str
    node_id: int | None
    node: str
    finish: str
    done_when: str = ""
    files: Sequence[str] = field(default_factory=tuple)
    path: Sequence[str] = field(default_factory=tuple)
    reason: str = ""  # a redo's Laya reason, or the node an escalation came from
    notes: str = ""
    references: Sequence[str] = field(default_factory=tuple)  # already resolved (design entries inlined)
    cases: Sequence[str] = field(default_factory=tuple)  # ground-truth cases, evidence in evidences/

    def render(self) -> str:
        lines = ["SCOPE (fixed for this whole conversation)",
                 f"  role:      {ROLE_LABEL.get(self.role, self.role)}"]
        node = f"[id={self.node_id}] {self.node}" if self.node_id is not None else self.node
        lines.append(f"  node:      {node}")
        if self.done_when:
            lines.append(f"  done_when: {self.done_when}")
        if self.files:
            lines.append(f"  files:     {', '.join(self.files)}")
        if self.notes:
            lines.append(f"  notes:     {self.notes}")
        if self.cases:
            lines.append(f"  cases:     {', '.join(self.cases)} (evidence in evidences/<case>.txt or .png)")
        if self.references:
            lines.append("  read first:")
            lines.extend(f"    - {ref}" for ref in self.references)
        if self.path:
            lines.append(f"  path:      {'  >  '.join(self.path)}")
        if self.reason:
            lines.append(f"  why:       {self.reason}")
        lines.append(f"  may:       {ROLE_MAY.get(self.role, '')}")
        lines.append(f"  must not:  {ROLE_MUST_NOT.get(self.role, '')}")
        lines.append(f"  finish:    {self.finish}")
        return "\n".join(lines)


def build_system_message(anchor: ScopeAnchor, role_prompt: str, index_lines: Sequence[str] = ()) -> str:
    parts = [anchor.render(), environment_line(), role_prompt.strip()]
    if index_lines:
        parts.append("AVAILABLE CONTEXT -- pull what you need with tools, only when you need it:\n"
                     + "\n".join(f"  {line}" for line in index_lines if line))
    return "\n\n".join(p for p in parts if p)


def build_kickoff(anchor: ScopeAnchor) -> str:
    return f"Work on {'node ' + str(anchor.node_id) if anchor.node_id is not None else 'this'} now. " \
           f"When it's complete, call {anchor.finish}."
