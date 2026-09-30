# The plan tree

The plan is a tree of `Leaf` rows (`src/JFI/models/leaf.py`) in `.jfi/JFI.db`.
The planner writes it through its node tools (`src/JFI/planner/nodes.py`, see
[phase-planner.md](phase-planner.md)); Dev finishes leaves through its gated
`mark_leaf_done` (`src/JFI/imp/dev.py`, see [phase-imp.md](phase-imp.md)). No
phase ever edits a plan file: `plan.md` survives only as the checklist
markdown `render_plan_markdown` renders for the dashboards.

## Shape

- **Parent vs leaf is structural.** A row with children is a parent; a row
  without is a leaf. There's no flag. Only leaves carry `status`,
  `started_at`/`ended_at` and `tokens`.
- **`phase`** (`models/enums.Phase`) is `imp` for every node the planner
  writes. Rows with `testing` (or Program Manager review fields) come from
  sessions of the removed v1 pipeline and are only read by `export-db`.
- **`level`** is the role that wrote the node (`architect` / `lead` / `task`),
  set by code from the calling role, never by the model.
- **`plan_status`** is the judge's verdict: GOOD / BREAKDOWN / REDO. Every
  verdict is also kept as a `PlannerVerdict` row.
- **Node fields** the planner fills: `kind`, `done_when`, `files`,
  `depends_on` (ordering between nodes, acyclic), plus the counters its
  guards use (`redo_count`, `escalation_count`, `attempt_count`, ...).
- **Order:** siblings sort by a gap-numbered `sort_key` (10, 20, 30…).
- **Numbers like `1.2.3` are computed** (`display_number`) and never
  stored. Nothing ever renumbers anything.
- **Depth** is capped in code at `MAX_LEAF_DEPTH = 5`
  (`JFI.planner.nodes`), because prompt text alone didn't stop runaway
  splitting in a real run.

## What `get_plan()` shows

```
## imp
[id=1] 1. Data model
  [id=4] [ ] 1.1 add Leaf table to models/leaf.py
  [id=5] [x] 1.2 ...
```

A parent has no checkbox. `[x]` = done, `[o]` = skipped, `[ ]` = todo.

`render_plan_markdown` renders the older `- [ ] N.M` markdown for the status
bar, the fleet dashboard and `export-db`. It's display only.

## Read tools (`src/JFI/tool/plan_db_tools.py`)

| Tool | Does |
|------|------|
| `get_plan()` | whole tree, never truncated |
| `get_leaf(id)` | one leaf's full detail |

The reviewer has `get_plan` in its core tools (to find the leaf that owns a
bug). The planner's roles get their own `get_plan` / `get_node` /
`list_nodes` as core tools. v1's editing tools (`add_leaf`, `split_leaf`,
`merge_leaf`, `delete_leaf`, `update_leaf`, `reorder_leaf`, `start_leaf`,
`review_leaf`) were removed with v1.
