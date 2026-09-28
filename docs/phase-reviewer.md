# Phase: `reviewer`

Judges the finished work and decides whether another full iteration
(planner → product_owner → imp → testing → reviewer → cleanup) is needed. It
gates nothing interactively.

- **Code:** the generic `runner.run_phase`. The completion marker is
  `REVIEWER_COMPLETE`. After it: `_clear_reviewer_notes`, then (once
  cleanup has also run) `collect_next_iteration` / `review_outcome`.
- **Tools:** `get_reviewer_notes`, `write_review_report`
  (`tool/note_tools.py`; `SessionNote` kinds `REVIEWER_NOTES` and
  `REVIEW_REPORT`).
- **Prompt:** `get_system_message("reviewer")`. Unlike `imp`/`testing`,
  there's no inline work queue.
- **Trigger:** "Testing is done. Call get_plan() and evaluate the finished
  work against it…"

## What the prompt asks for

1. **Gather:** `get_plan()`, inspect the produced files, and
   `get_reviewer_notes()`. Treat each implementer note as something to
   re-check yourself, not something to accept.
2. **Re-run the mechanical checks yourself** (build, tests,
   start-and-hit-it). A testing leaf marked done isn't evidence the check
   still passes. A review with zero `execute_command`/`read_file` calls
   isn't a review.
3. **Pass:** reply "Review: PASS" with a summary, and **don't** call
   `write_review_report`.
4. **Fail:** call `write_review_report` with numbered, concrete issues
   (file/line, how to fix).
5. End with `REVIEWER_COMPLETE`.

## What happens with the verdict

- **Reviewer notes** are cleared right after this phase, so each pass
  starts clean.
- **Report present** (checked after `cleanup`): `review_outcome` wraps it
  as "REVIEW FAILED: …" and clears the note. That text, plus anything the
  user queued, becomes the next iteration's `USER FEEDBACK FOR ITERATION`.
  The planner's trigger switches to update mode with
  `review_failed=True`.
- **Loop cap:** `MAX_REVIEW_ITERATIONS = 3` failed reviews end the run.
  `REVIEW_LOOP_APPROVAL=1` asks a human before each retry.
- **No report and nothing queued:** the pipeline idles ("PIPELINE
  COMPLETE") until something is queued.

## Known gaps

- **The planner doesn't act on a failed review in tiered mode,** so the
  next iteration has no new leaves (see
  [phase-planner.md](phase-planner.md#known-gaps)).
- **"PIPELINE COMPLETE" isn't independent verification.** The reviewer is
  the same kind of model that did the work. See
  `.claude/skills/run-jfi/SKILL.md` §9.
