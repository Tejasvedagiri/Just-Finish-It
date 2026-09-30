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
| **Architect** | The whole map once: components, the runbook (`setup`, `run`, `test`, `test_one`, `build`, `e2e`) and design contracts | `architect` |
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

The Architect's `finish` is refused until the runbook has every required entry,
the stack names a test framework, and a plan with 3+ components has at least one
design contract. Its conversation may be continued up to `ARCHITECT_CONTINUATIONS`
times.

## The judge (`LAYA`)

By default every node is judged by the rule alone. With `LAYA=1` in `.env`,
every node gets two scores:

- **the rule** (`fallback_status`): Architect and Lead nodes are BREAKDOWN (a
  Lead node naming exactly one function is GOOD); Task nodes are GOOD. Never
  REDO.
- **Laya** ([laya](https://github.com/NandhaKishorM/laya)'s published `english`
  checkpoint), asked one question per level, with a confidence.

| Rule and Laya | Verdict | `decided_by` |
|---|---|---|
| agree | that answer | `agree` |
| disagree, Laya confidence >= `LAYA_MIN_CONFIDENCE` (0.75) | one LLM call (the Architect's model) picks one of the two, for every disagreement of the step at once | `llm` |
| disagree, Laya less sure | the rule | `rule` |

REDO can only come from Laya, through the tie-break. Without `LAYA=1`, or
without the `laya` extra (including in the binary), the rule decides alone
and the judge says so once. Laya runs on `LAYA_DEVICE` (cpu by default), loaded
once per session.

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
