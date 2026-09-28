# Phase: `product_owner` (shown as "Program Manager")

A read-only review of the **plan** against the **real repo**, after
planning and before any code is written. It catches a bad plan while fixing
it still costs one planner round instead of a full implementation cycle.

- **Code:** `runner._run_product_owner_loop`,
  `product_owner_feedback_outcome`, `MAX_PRODUCT_OWNER_ITERATIONS = 3`.
- **Tools:** `review_leaf`, `update_leaf` (`tool/plan_db_tools.py`),
  `write_plan_feedback` (`tool/note_tools.py`, `SessionNote` kind
  `PLAN_FEEDBACK`).
- **Tests:** `test/test_product_owner_loop.py`.

The key stays `product_owner`; only `PHASE_DISPLAY_NAMES` says "Program
Manager".

## Loop

```
round = 0
loop:
    add the product_owner trigger (unless the last user message already has it)
    run_phase("product_owner")                  until PRODUCT_OWNER_COMPLETE
    feedback = PLAN_FEEDBACK note (None if write_plan_feedback wasn't called)
    clear the PLAN_FEEDBACK note                 always, immediately (one-shot signal)
    feedback is None  → approved, continue to imp
    round += 1; round >= 3 → stop the whole run
    add the feedback + planner trigger(po_feedback_given=True)
    run_phase("planner")                         see the known gap in phase-planner.md
```

`_run_session` hands the `product_owner` slot of `PHASES` to this function
and never calls `run_phase("product_owner")` directly.

## What the prompt asks for

1. **`get_plan()`.** On a re-review, re-check only what changed
   (`get_leaf` on reworked leaves).
2. **Inspect the real repo** with `execute_command` / `read_file`, and
   don't trust facts from `context_save`. Look for:
   - wrong assumptions about what already exists;
   - ordering bugs;
   - goal requirements that no leaf covers;
   - approaches that conflict with existing conventions for no stated reason.
3. **`review_leaf(leaf_id, "approved"|"rejected", expected_changes=...)`**
   on **every** genuine leaf. The per-leaf verdict is the durable record.
   A whole-plan verdict once cost about 2 hours re-reviewing a plan that
   came back byte-identical.
4. **One rejection per leaf.** A second rejection without an `update_leaf`
   in between is refused (`REVIEW_REJECTION_CIRCUIT_BREAKER`). Approve the
   leaf or name it as stuck.
5. **All approved:** reply "Product Owner: APPROVED" with a summary, and
   **don't** call `write_plan_feedback`.
6. **Any rejected:** call `write_plan_feedback` once, naming every
   rejected leaf id and its expected changes.
7. End with `PRODUCT_OWNER_COMPLETE`.

## Known gaps

- **Rework never reaches the planner in tiered mode.** The planner re-run
  after feedback makes no LLM calls (see
  [phase-planner.md](phase-planner.md#known-gaps)). Re-review then meets an
  unchanged plan with the circuit breaker still armed.
- **Neither tool shows the review verdict.** `get_plan` and `get_leaf`
  don't show `review_status` / `review_note`. A re-review can't see its own
  earlier verdicts except through history.
