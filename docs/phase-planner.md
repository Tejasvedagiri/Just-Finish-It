# Phase: planner

Code: `src/JFI/planner/` (`loop.py` the loop, `nodes.py` the plan tree and its
tools, `judge.py` the judge, `prompts.py` the role prompts). `runner._run_planner`
hands the phase to `Planner.run()`. The design and every decision behind it are
in [`laya_plan.md`](laya_plan.md).

## Shape

Three roles, each working in short, scoped **episodes**
([`JFI.episode`](../src/JFI/episode/__init__.py)): one small conversation about
one node, with a never-trimmed scope anchor, pulling everything else through
tools, ended by the role's `finish` tool (or the token budget / turn cap).

| Role | Writes | Level of its nodes |
|------|--------|--------------------|
| **Architect** | The whole map once: components, the runbook (`setup`, `run`, `test`, `test_one`, `build`, `e2e`, `script`, and the layout: `src_dir`, `test_dir`, `test_naming`) and design contracts | `architect` |
| **Lead** | One component broken into files | `lead` |
| **Task** | One file broken into functions (or copies, config lines, ...) | `task` |

After each layer the **judge** labels every new node:

- **GOOD**: one small, clear job. It goes straight to Dev.
- **BREAKDOWN**: several jobs. The next role splits it.
- **REDO**: operational ("run the app") or too vague. It goes back to the role that wrote it.

## The loop (`Planner.run`)

Each step takes the first pending action, re-reading the DB every time, so resume
is just "run again":

```
for level in architect, lead, task:
    judge      the level's unjudged nodes        (one batch)
    redo       the level's REDO nodes            (one episode each, by their creator)
    break down the level's BREAKDOWN nodes       (one episode each, by the next role)
               that have no children yet
```

A level's judging and redos come before its breakdowns, and each level before
the next, so nothing reaches Lead while an Architect node is unjudged or REDO
(the gates). A broken-down node becomes GOOD once it has children. Planning is
done when every live node is GOOD; then `PLANNER_COMPLETE` is appended to
history for the phase-level resume.

The Architect's `finish` is refused until the runbook has every required entry
(`REQUIRED_RUNBOOK` in `planner/loop.py`), `test_one`'s notes give an example id,
`script` has its `{file}` placeholder, the stack names a test framework, and a
plan with 3+ components has at least one design contract. Its conversation may
be continued up to `ARCHITECT_CONTINUATIONS` times.

**Where tests live is the runbook's, not a prompt example.** On the QA
machine's stui run the Lead prompt's hard-coded colocated example
(`src/views/news.js -> src/views/news.test.js`) fought the Architect's
`__tests__/`, and the colocated tests couldn't be run through `test_one` at all.
Now Lead, Task and Dev read `src_dir` / `test_dir` / `test_naming`, and
`add_node` refuses a test file outside `test_dir` with the expected path.

**Notes and references.** Every node can carry `notes` (what to implement and
how) and `references` (design entries, source ranges, docs) besides its short
description; see [`plan-tree.md`](plan-tree.md). The node tools' errors are
written to be acted on: `depends_on` given as positions (`[1, 2]`) gets the real
node ids back, and `name(` with no space is what counts as a function name, so
"verbatim (L…)" in a description isn't a false duplicate.

**Project memory.** A new session in the same project starts with the most
recent earlier session's runbook (unverified) and its stack, component,
contract and convention design entries (`session/project_memory.py`); the
Architect's prompt says to keep what still fits the goal and correct the rest.

## The judge (`LAYA`)

By default every node is judged by the rule alone. With `LAYA=1` in `.env`,
every node gets two scores:

- **the rule** (`fallback_status`): Architect and Lead nodes are BREAKDOWN (a
  Lead node naming exactly one function is GOOD); Task nodes are GOOD. Never
  REDO.
- **Laya** ([laya](https://github.com/NandhaKishorM/laya)'s published `english`
  checkpoint), asked one yes/no question per node: given the task (its
  description) and its description (its notes), "Can this be solved with 20k
  tokens?". P(yes) >= 0.4 (`LAYA_YES_THRESHOLD`) is GOOD, otherwise
  BREAKDOWN; its confidence is the calibrated `answer_confidence`.

| Rule and Laya | Verdict | `decided_by` |
|---|---|---|
| agree | that answer | `agree` |
| disagree, Laya confidence >= `LAYA_MIN_CONFIDENCE` (0.75) | one LLM call (the Architect's model) picks one of the two, for every disagreement of the step at once | `llm` |
| disagree, Laya less sure | the rule | `rule` |

Laya answers GOOD or BREAKDOWN only; REDO comes from an escalation or the
redo cap, never from the judge. Without `LAYA=1`, or
without the `laya` extra (a source install that didn't sync it; the binary always has it), the rule decides alone
and the judge says so once. Laya runs on `LAYA_DEVICE` (cpu by default), loaded
once per session.

Why this question: the first state -- goal, level, path, node, done_when,
files, "is this clean enough?" A/B/C per level -- gave Laya labels it can't
interpret; its confidence never passed 0.55 and it ranked nodes barely better
than chance (AUC 0.59). With only the task and its description and a concrete
sizing question, it separates the nodes Dev finished in one episode from the
ones that had to be broken down: AUC 0.76 over 67 nodes from 4 runs, 73%
accuracy at the 0.4 cut-off, 88% on the nodes that needed a breakdown
(`benchmark/laya_poc.py`, [`laya_poc.md`](laya_poc.md); rerun it as runs
accumulate). `multilingual` stayed at 0.92-0.999 on everything.

Why the base checkpoint and not a fine-tune: fine-tunes on this project's own
plan nodes scored well on held-out data but overfitted to the node's level --
on stui run 14 one answered BREAKDOWN at 0.98 for every Architect and Lead node
and GOOD at 0.98 for every Task node, whatever the text said.

Every verdict is stored as a `PlannerVerdict` row.

## Rules enforced in code (`nodes.py`)

- A node's `level` comes from the calling role, never the model; a role only
  updates or deletes nodes it created (REDO goes back to the creator).
- Lead and Task add nodes only under the node their episode is about.
- Descriptions are capped at `PLANNER_ITEM_MAX_CHARS` (200): the judge reads them.
- `depends_on` must name existing nodes and stay acyclic.
- Depth is capped at `MAX_LEAF_DEPTH` (5); a function that already exists in the
  plan, or a file another component owns, is refused as a duplicate.

## Guards

- `PLANNER_REDO_CAP` (2): redos per node before it's escalated.
- `MAX_PLANNER_EPISODES` (200): the whole phase's episode budget.
- `MAX_EPISODE_TURNS`: per episode; the token budget is `CONTEXT_SIZE ×
  CONTEXT_COMPRESSION_RATIO`.

## Later iterations

A failed review or queued request is recorded as a `USER FEEDBACK FOR ITERATION`
message. `runner._replan_feedback` hands the feedback since the last finished
planning round to the Architect, which extends the plan; finished leaves are left
alone.

## Cut-off episodes

- **Turn cap:** a Lead or Task episode that runs out of turns is continued once,
  told which nodes it already added, whether it added some or none (on the stui
  runs, Task episodes cut off after some functions left files incomplete).
  Still out of turns with no nodes: the node goes back to its creator as too big.
- **LLM error:** retried once; still failing with no nodes, the node stays
  BREAKDOWN and planning stops (an error says nothing about the node's size).
