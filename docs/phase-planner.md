# Phase: `planner`

Turns the user's goal into the plan tree (see [plan-tree.md](plan-tree.md)).
It writes no deliverable code. Its output is `Leaf` rows only, plus facts
saved with `context_save` for later stages and phases.

- **Code:** `runner.run_phase` (the `tiered_planner` branch),
  `PLANNER_ARC_STAGE`, `PLANNER_NODE_STAGES`, `_node_scoped_marker`,
  `PLANNER_PHASE_COMPLETE_MARKER`.
- **Prompts:** `get_system_message(phase="planner", planner_stage=...,
  planner_node_id=...)` and `get_phase_trigger("planner", ...)`.
- **Tests:** `test/test_tiered_planner.py`.

## Two modes

| Mode | When | Shape |
|------|------|-------|
| **Tiered** (default) | `PLANNER_SINGLE_PASS` unset | Arc once, then Lead → Dev → Task Planner per top-level branch |
| **Single pass** | `PLANNER_SINGLE_PASS=1` | one prompt, one `PLANNER_COMPLETE` marker |

The planner is tiered because one combined pass let compound leaves through
("GET /x happy paths: empty → …; seeded → …; bad input → …" as one leaf).
A breadth-first sweep of the whole tree per stage was also rejected: its
context per turn grew too large.

## Tiered flow

```
Arc (whole tree, once)                     marker: ARCHITECT_STAGE_COMPLETE
for node_id in top_level_leaf_ids():       every top-level leaf, imp AND testing, created_at order
    Lead  (node's subtree only)            marker: LEAD_STAGE_COMPLETE_NODE_<id>
    Dev   (node's subtree only)            marker: DEV_STAGE_COMPLETE_NODE_<id>
    Task Planner (node's subtree only)     marker: TICKETS_STAGE_COMPLETE_NODE_<id>
append synthetic assistant message "PLANNER_COMPLETE"   (no LLM turn)
```

- **Depth-first:** one branch goes through all three per-node stages
  before the next branch starts.
- **Driving each stage:** `ssm.set_planner_stage(stage)` and
  `ssm.set_planner_node(node_id)` pick the system prompt, then
  `_drive_turn_loop` runs until that stage's marker appears.
- **Shared history:** all stages share one history, so a later stage sees
  earlier stages' turns (compressed). Only the system prompt changes.
- **Top-level list:** `top_level_leaf_ids` is re-read after Arc finishes,
  so it contains whatever Arc added.
- **Resume:** the stage and node live in memory only. Resume works by
  scanning history for each marker: Arc is skipped if its marker exists,
  and each (node, stage) pair is skipped if its node-scoped marker exists.
  A crash midway through node 2's Dev stage resumes exactly there.
- **Completion:** `PLANNER_COMPLETE` is appended so the generic
  `get_remaining_phases` resume scan sees the phase as done. No per-node
  prompt ever asks the model to say it.
- **After the phase:** `ssm.ensure_plan_file()` runs.

### Stage 1: Arc (`architect`)

**Job:** the top-level shape only.

- `get_plan()` first.
- `add_leaf(phase="imp"|"testing", description=...)` with **no parent_id**,
  one call per major piece.
- No `start_leaf` or `mark_leaf_done` calls.
- `context_save` architecture notes, the data model and constraints. This
  replaces the old "Context and Prerequisites" section.
- If a plan already exists, extend it and never re-add existing items.

The order Arc adds items in is the walk order for every later stage.

### Stage 2: Lead (`team_lead`), one node

**Job:** give node `[id=N]` feature-sized children.

- **Scope:** only node N's subtree. Other branches are off limits.
- **No source reading:** no `read_file`, no grep/cat. Lead decides from
  the description, `get_plan` / `get_leaf` and `context_lookup` alone.
  Finding the right file is Dev's job.
- **At least 2 children per parent.** The top-level node itself never
  becomes a leaf.
- **Leave the node as Arc wrote it:** no rename, merge or reorder.
- **Anything not clearly atomic stays a bare item** for Dev.

### Stage 3: Dev (`journeyman`), one node

**Job:** walk node N's subtree down to genuinely atomic leaves, each naming
the **file** it touches.

The prompt names five failures seen in real runs:

1. A bundled multi-scenario test leaf → one leaf per case.
2. "kill server, start, request, inspect DB" → one leaf per action.
3. "write a check and fix whatever it finds" → write harness / run+fix /
   re-run until clean (then remove the harness).
4. Reasoning over many items in one turn blew the reasoning cap and the
   server's context limit. The rule: **one item per turn**, decide, then
   make at most one tool call.
5. Recursive splitting of investigation work (30+ leaves, 7 levels deep).
   The rule: split at most once. `MAX_INVESTIGATION_DEPTH` enforces this in
   code.

It also has to:

- fix ordering bugs with `reorder_leaf`: nothing may use a package, file
  or table before the leaf that creates it;
- make sure there's at least one concrete, command-named Testing leaf
  (per `VERIFICATION_RULES`).

### Stage 4: Task Planner (`function_breakdown`), one node

**Job:** make every leaf under node N **one mechanically-executable
ticket**.

- **Code leaves:** one child per function/method, with real names and
  signatures where they're already decided, e.g. "implement
  collect_news(held: list[str]) -> list[dict]: …".
- **Non-code leaves** (setup, config, shell, curl+assert): get the same
  one-action test and are split if they bundle more than one action.
- **Context cache:** `context_lookup` before opening any file,
  `context_save` what each file contains after reading it. This stops
  re-reading the same file for every leaf.
- **One leaf per turn**, same as Dev.

## Single-pass prompt

`get_system_message("planner", planner_stage=None)` is the original
combined prompt:

- build the whole tree with `add_leaf`, top-level first, then children;
- recurse until every leaf is one step;
- include ≥1 mechanical Testing leaf;
- extend an existing plan without touching done leaves.

It ends with `PLANNER_COMPLETE`.

## Triggers (`get_phase_trigger("planner", ...)`)

- **First iteration:** `"My goal is: <goal>\n\nBuild the step-by-step plan
  now..."`. `AdaptiveSessionManager` parses the goal back out of this exact
  prefix to detect the task type, so don't change the prefix casually.
- **Iteration > 1, review failed, or Program Manager feedback:** "Update the
  plan so it covers the request above. Call get_plan() first, leave every
  already-done leaf untouched, and add_leaf new items…". A paragraph is
  added for a failed review and/or for Program Manager feedback.

## Per-phase model

The `PLANNER_*` env overrides (`PLANNER_MODEL`, ...) apply to every stage.
There are no per-stage model overrides.

## Known gaps

**1. (Fixed) Re-planning used to be a no-op in tiered mode.** Every later
planner run found round one's `ARCHITECT_STAGE_COMPLETE` and `*_NODE_<id>`
markers in history and skipped every stage (0 LLM calls), so Program
Manager rework, failed reviews and queued follow-ups were never planned.
Stage markers are now scoped to a **planning round**:
- `run_phase` appends a synthetic `PLANNER_ROUND_START` when the previous
  round already finished (`PLANNER_COMPLETE` after its start);
- it only looks for stage markers after the latest round start
  (`_planning_round_start`);
- an interrupted round resumes instead of restarting;
- pre-fix sessions behave as before.

Pinned by the "Planning rounds" tests in `test/test_tiered_planner.py`.

**2. The planner prompts don't mention `update_leaf`.** Program Manager's
rework loop expects `update_leaf` (it clears the circuit breaker), but
`PLAN_FORMAT_RULES` lists nine tools without it. The Lead prompt even says
"there's no rename tool".

**3. Walk order ignores `reorder_leaf`.** `top_level_leaf_ids` sorts by
`created_at`, not `sort_key`.

**4. (Fixed) Feedback wording was from the markdown era.**
`product_owner_feedback_outcome`, `review_outcome`, the iteration message,
`_stuck_task_directive` and the AUTO-RECTIFY give-up message now point at
`add_leaf` / `update_leaf` / `split_leaf` / `add_reviewer_note`, not
"`- [ ]` items in the plan file".
