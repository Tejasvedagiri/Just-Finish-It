# Phase: `imp` (Implementation)

Builds the deliverables, one pending `imp` leaf at a time, and keeps the
plan's status honest while it works.

- **Code:** the generic path in `runner.run_phase` → `_drive_turn_loop`.
  The completion marker is `IMP_COMPLETE`.
- **Prompt:** `get_system_message("imp")`, plus the **work queue**
  `_phase_system_message` adds: the currently pending `imp` leaves as
  `[id=N] <number> <description>` lines (from `_pending_items` /
  `render_pending_lines`). The queue is rebuilt every turn, so it shrinks as
  leaves are marked done.
- **Trigger:** "Begin implementation. Work through the pending imp leaves
  one at a time…"

## The per-leaf loop the prompt asks for

1. **Pick the first leaf in the queue.** Use `get_plan()` only if the queue
   looks stale.
2. **`start_leaf(id)`,** then announce it as `**[CURRENT TASK: 1.1]**`.
   `start_leaf` sets the session's current task, which drives the status
   bar and the stuck-leaf timer.
3. **Do the work** with `write_file` / `append_to_file` / `replace_in_file`
   / `execute_command`. Prefer `read_file` over `cat`.
   - For an **investigation leaf** ("investigate", "debug", …), don't
     `add_leaf` each next step. Dig with ordinary tool calls and
     `context_save` the findings.
   - `add_leaf` is only for genuinely **new deliverable** work the digging
     uncovered.
4. **`add_reviewer_note`** when something needed a workaround, rested on an
   assumption, or couldn't be fully verified. Skip it for clean steps. The
   notes are appended to the `REVIEWER_NOTES` `SessionNote` and read by the
   reviewer.
5. **`mark_leaf_done(id)` immediately.** Never batch completions.
6. **Repeat** until nothing is pending.
7. **Run one whole-project build/typecheck** (`npm run build`,
   `tsc --noEmit`, `go build ./...`, test collection, …) and fix anything it
   reports. Per-leaf checks miss cross-file drift.

Testing leaves are left alone. The phase ends with `IMP_COMPLETE`.

## Runtime help from `_drive_turn_loop`

- **Stuck leaf.** If the current leaf stays current longer than
  `TASK_STUCK_TIME_LIMIT_SECONDS` (default 300) or spends more than
  `TASK_STUCK_TOKEN_LIMIT` (default 150k) re-sent tokens,
  `_stuck_task_directive` orders a split. Its wording still describes
  editing a markdown plan file; the real action is `split_leaf`.
- **Ctrl+K / Ctrl+Q.** These skip the current leaf, or all remaining leaves
  (status → skipped, `[o]`), and tell the model to move on.
- **`AUTO-RECTIFY`.** A tool result starting with `Error` triggers repair
  coaching for that tool.

## Things to know when editing

- **Mid-phase resume** needs nothing special. Done leaves are simply no
  longer in the queue.
- **An empty queue** (for example, after the planner re-plan gap in
  [phase-planner.md](phase-planner.md#known-gaps)) makes the phase trivially
  say `IMP_COMPLETE`.
