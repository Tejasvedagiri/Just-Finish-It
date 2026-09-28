# The plan tree

Every phase reads and writes the plan. It's a tree of `Leaf` rows
(`src/JFI/models/leaf.py`) in `.jfi/JFI.db`, edited **only** through the
tools in `src/JFI/tool/plan_db_tools.py`. No phase ever edits a plan file:
`plan.md` / `plan_path` survive only as a display/fallback path.

## Shape

- **Parent vs leaf is structural.** A row with children is a parent; a row
  without is a leaf. There's no flag. Only leaves carry `status`,
  `started_at`/`ended_at` and `tokens`, and the tools refuse status changes
  on a parent.
- **`phase`** (`models/enums.Phase`) is almost always `imp` or `testing`.
  Implementation and Testing are separate trees, each numbered from 1.
  `split_leaf` children inherit the parent's phase.
- **Order:** siblings sort by a gap-numbered `sort_key` (10, 20, 30…).
  Top-level branches, though, are *walked by the planner* in `created_at`
  order (`top_level_leaf_ids`). So `reorder_leaf` on a top-level item
  changes the display order, not the planner's walk order.
- **Numbers like `1.2.3` are computed** (`display_number`) and never
  stored. Nothing ever renumbers anything.
- **Review fields** (set by Program Manager, see
  [phase-product-owner.md](phase-product-owner.md)):
  - `review_status`: `approved` / `rejected` / None
  - `review_note`: the expected changes
  - `rejection_count`
- **Depth caps**, enforced in code (`_check_depth`) because prompt text
  alone didn't stop runaway splitting in a real run:
  - `MAX_LEAF_DEPTH = 5`
  - `MAX_INVESTIGATION_DEPTH = 2` for any branch whose own or ancestor
    description contains an `INVESTIGATION_KEYWORDS` word ("investigate",
    "debug", "root cause", ...).

  Hitting a cap returns an `Error` that tells the model to execute the leaf
  instead.

## What `get_plan()` shows

```
## imp
[id=1] 1. Data model
  [id=4] [ ] 1.1 add Leaf table to models/leaf.py
  [id=5] [x] 1.2 ...
## testing
[id=3] [ ] 1 run `uv run pytest` and confirm exit 0
```

A parent has no checkbox. `[x]` = done, `[o]` = skipped, `[ ]` = todo.
Neither `get_plan` nor `get_leaf` shows the review fields: a planner
reworking a rejection only learns why from the feedback message.

`render_plan_markdown` renders the older `- [ ] N.M` markdown for the
status bar, the fleet dashboard and `export-db`. It's display only.

## Tools

| Tool | Does | Refuses |
|------|------|---------|
| `get_plan()` | whole tree, never truncated | — |
| `get_leaf(id)` | one leaf's full detail | — |
| `add_leaf(phase, description, parent_id=0)` | new item; `parent_id=0` means top-level | unknown phase; a parent that already has status/timing; depth cap |
| `split_leaf(id, into=[...])` | leaf → parent with ≥2 new children (resets its status) | fewer than 2 children; depth cap |
| `merge_leaf(child_id)` | folds an *only* child back into its parent, which becomes a leaf | parent with ≠1 child; a child that has children |
| `delete_leaf(id)` | removes a wrong/duplicate leaf | parents; done leaves |
| `update_leaf(id, description)` | edits a leaf in place and clears its review verdict | parents; done leaves |
| `reorder_leaf(id, after_leaf_id=0)` | moves a leaf among its siblings | — |
| `start_leaf(id)` | sets `started_at`, the session's current task | — |
| `mark_leaf_done(id, tokens=0)` | status → done | parents |
| `review_leaf(id, verdict, expected_changes)` | Program Manager's per-leaf verdict | parents; a rejection without `expected_changes`; a second rejection with no `update_leaf` in between (circuit breaker) |

All tools are **deferred**: the model must `load_tool` each one before it
can call it.

The tool-list text the model sees is in `PLAN_FORMAT_RULES`
(`simple_session_manager.py`) and in `CORE_PLAN_RULES` (`task_rules.py`),
and the two must stay in sync. Both say "these nine tools" and describe
neither `update_leaf` nor `review_leaf`; those two appear only in the
Program Manager prompt and the deferred-tool summaries.

## Rules the prompts enforce (model-side)

- **One leaf = one tool call's worth of work.** "and", "+", "/" or ";"
  joining separate deliverables means the item should be split.
- **Every parent has ≥2 children.** A single-child parent should be merged
  back with `merge_leaf`; don't invent a second child.
- **Split at most once for investigation/diagnostic work.** The actual
  digging happens inside the leaf, recorded with `context_save`.
- **Never touch done leaves.** Extend the plan with new `add_leaf` items
  instead.
