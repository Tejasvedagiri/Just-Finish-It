# The plan tree

The plan is a tree of `Leaf` rows (`src/JFI/models/leaf.py`) in `.jfi/JFI.db`.
The planner writes it through its node tools (`src/JFI/planner/nodes.py`, see
[phase-planner.md](phase-planner.md)); Dev finishes nodes through its gated
`mark_leaf_done` (`src/JFI/imp/dev.py`, see [phase-imp.md](phase-imp.md)). No
phase ever edits a plan file: `plan.md` survives only as the checklist
markdown `render_plan_markdown` renders for the dashboards.

## Shape

- **Parent vs leaf is structural.** A row with children is a parent; a row
  without is a leaf. There's no flag. Every node carries `status`,
  `started_at`/`ended_at` and `tokens`: Dev finishes the leaves first and then
  each parent above them, bottom-up (1.1.1, 1.1.2, 1.1, 1), checking the part
  as a whole ([phase-imp.md](phase-imp.md)).
- **`phase`** (`models/enums.Phase`) is `imp` for every node the planner
  writes. Rows with `testing` (or Program Manager review fields) come from
  sessions of the removed v1 pipeline and are only read by `export-db`.
- **`level`** is the role that wrote the node (`architect` / `lead` / `task`),
  set by code from the calling role, never by the model.
- **`plan_status`** is the judge's verdict: GOOD / BREAKDOWN / REDO. Every
  verdict is also kept as a `PlannerVerdict` row.
- **Node fields** the planner fills: `kind`, `done_when`, `files`,
  `depends_on` (ordering between nodes, acyclic), `notes` (what to implement
  and how: steps, edge cases, the contract, what not to touch) and
  `references` (where the context is: design entries like
  `contract:main->calc`, source ranges like `page.html L1376-1402`, docs or
  URLs), plus the counters its guards use (`redo_count`, `escalation_count`,
  `attempt_count`, ...). The description stays at most 200 characters; detail
  goes in `notes`. The next layer's brief shows notes and references (design
  entries inlined). The dashboards' Task | Judge table has Notes and References columns, and a node's detail shows its notes as points (`note_points`: numbered steps nest under the line before them, every other sentence is a bullet) and its references as a list.
  The judge reads the description only.
- **`checkpoint`**: the git commit taken when the leaf passed
  (`tool/checkpoint_tools.py`).
- **`cases`** ([`old_new.md`](old_new.md)): the node's own ground-truth
  cases -- on an Architect component, a Lead file node or a Task leaf -- which
  Dev compares the node with when it finishes it (evidence in
  `.jfi/evidence/<session_id>/<task number>_<case>.*`, renamed when the plan
  renumbers: `sync_evidence_names`). A case belongs to one node. Sessions
  planned before that have `compare` leaves, whose cases are the ones they
  check. **`evidence_hash`**: the evidence a node matched, so an edit or
  re-capture re-queues it (and the parents above it).
- **Order:** siblings sort by a gap-numbered `sort_key` (10, 20, 30…).
- **Numbers like `1.2.3` are computed** (`display_number`) and never
  stored. Nothing ever renumbers anything.
- **Depth** is capped in code at `MAX_LEAF_DEPTH = 5`
  (`JFI.planner.nodes`), because prompt text alone didn't stop runaway
  splitting in a real run.

## What `get_plan()` shows

```
## imp
[id=1] [ ] 1 Data model
  [id=4] [x] 1.1 add Leaf table to models/leaf.py
  [id=5] [ ] 1.2 ...
```

Every node has a checkbox, parents too. `[x]` = done, `[o]` = skipped, `[ ]` = todo.
The progress counts (`plan_progress_db`) count every node.

`render_plan_markdown` renders the older `- [ ] N.M` markdown for the status
bar, the fleet dashboard and `export-db`. It's display only.

## Read tools (`src/JFI/tool/plan_db_tools.py`)

| Tool | Does |
|------|------|
| `get_plan()` | whole tree, never truncated |
| `get_leaf(id)` | one leaf's full detail, with done_when, files, notes, references and fix note |

The reviewer has `get_plan` in its core tools (to find the leaf that owns a
bug). The planner's roles get their own `get_plan` / `get_node` /
`list_nodes` as core tools. v1's editing tools (`add_leaf`, `split_leaf`,
`merge_leaf`, `delete_leaf`, `update_leaf`, `reorder_leaf`, `start_leaf`,
`review_leaf`) were removed with v1.
