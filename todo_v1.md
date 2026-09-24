# TODO v1 — Planning pipeline redesign (Arc → Lead → Developer → Task Planner → Program Manager)

Status as of 2026-09-24. Diagnosis and design only — nothing in this file is
implemented yet. Grounded entirely in one real session's actual DB/log data
(`StockAPIServer/.jfi/JFI.db`, session `start`), not hypothetical concerns.

## Why — what the data actually shows

Queried `StockAPIServer/.jfi/JFI.db` (`historymessage`, `leaf`, `logevent`)
directly rather than guessing. One session, "start", goal: make `uv run app`
work for a small FastAPI project (5 top-level items, 24 leaves total).

- **Span: 2026-09-22 21:31 → 2026-09-24 19:21 — 46 hours wall-clock.**
  Stopped/resumed 6 times (`🎉 SESSION TERMINATED` × 6 in the log).
- **Phases actually reached: `PLAN` (11×) and `PRODUCT OWNER` (4×). Nothing
  else. Ever.** No `PHASE: IMP`, no `TESTING`, no `REVIEWER`, no `CLEANUP` —
  not once in 46 hours.
- **Leaves done: 0 / 24.** Every leaf is still `TODO`. Zero implementation
  progress of any kind.
- **176 main LLM turns + 73 separate digest/compression side-calls = 249
  total LLM round trips**, measured directly from `PROMPT SENT` log
  timestamps. Gaps between successive round trips: **median 257s (~4.3
  min)**; the 15 worst turns ranged from **979s to 4159s (16–69 minutes
  each)**. Total measured LLM time: **21.45 hours** — all of it spent on
  planning and plan review, none on building anything.
- **306 tool calls**, all inside PLAN/PRODUCT_OWNER: 168 `execute_command`,
  28 `get_plan`, 29 `get_leaf`, 20 `add_leaf`, 20 `context_lookup`, 13
  `read_file`, 11 `context_save`, 10 `load_tool`, 4 `split_leaf`, 2
  `write_plan_feedback`, 1 `reorder_leaf`.
- **Product Owner rejected the plan twice** (`write_plan_feedback` at
  13:32:54 and 15:26:30). The second rejection's own text says it plainly:
  *"the one defect I flagged last time is STILL PRESENT and unchanged. The
  plan text is byte-identical to the version I reviewed."* **A full
  Architect → Team Lead → Journeyman → Function Breakdown replan cycle ran
  for ~1h54m and changed nothing.** That's not a fluke of this run — it's
  structural: the whole plan gets regenerated and re-reviewed to fix ONE
  flagged leaf, with no mechanism to touch just that leaf.

### Root causes, tied to the evidence above

1. **No per-ticket approval loop.** Product Owner (and Reviewer, same
   pattern) can only accept-or-reject the ENTIRE plan. A single bad leaf
   sends everything back through all four planner stages, at full cost,
   with no guarantee the flagged issue even gets touched (see the
   byte-identical replan above).
2. **Tickets aren't atomic enough to implement in one shot.**
   `function_breakdown` (the last planner stage today) explicitly excludes
   anything that isn't code-writing (*"Testing leaves, and any
   Implementation leaf that is NOT itself writing code, are OUT OF SCOPE
   for this pass"* — `simple_session_manager.py`'s function_breakdown
   prompt). A leaf like "make `uv run app` work" never gets reduced to a
   single mechanical step like `uv init` / `uv sync` the way a code leaf
   gets reduced to `implement collect_news(...)`. Those leaves stay coarser
   than what Implementation can execute in one focused turn.
3. **Every one of those 249 round trips is expensive.** The local backend
   (LM Studio, `qwen/qwen3.8-27b`, a reasoning model) reasons heavily on
   everything — verified directly against it: ~50-60 reasoning tokens just
   to answer "what is 2+2?". Median 4.3 minutes and worst-case over an hour
   PER TURN means a process that's merely turn-heavy (not wrong, just
   chatty) is automatically also wall-clock-heavy. 46 hours with 0 leaves
   done is what "turn-heavy + slow backend + whole-plan rejection loop"
   compounds into.
4. **Massive re-verification overhead before any code exists.** 168
   `execute_command` + 28 `get_plan` + 29 `get_leaf` = 225 read/verify
   calls, more than the other 81 tool calls combined, all spent
   re-inspecting the plan and the repo from scratch — much of it likely
   re-deriving facts a stricter, narrower Product Owner pass wouldn't need
   to re-check every single iteration.

## The new pipeline

Maps directly onto today's tiered planner (`runner.PLANNER_STAGES` in
`src/JFI/runner.py`) plus the `product_owner` phase
(`simple_session_manager.py`). Two stages are kept, two are reframed, one
(Program Manager) gets a real per-ticket mechanism it doesn't have today.

| Role (requested) | Today | Change |
|---|---|---|
| **Arc** — vision | `architect` stage (tag `Arc`) | Keep as-is. Already does top-level shape only. |
| **Lead** — modules | `team_lead` stage (tag `Lead`) | Keep as-is. Already breaks top-level items into feature-sized children. |
| **Developer** — files & functions | `journeyman` stage (tag `Journy`) — already literally titled "JOURNEYMAN DEVELOPER" in its own prompt | Rename tag to `Dev`; reframe the prompt to decompose every module-level item into concrete **files** and, within each file, the **functions/responsibilities** it needs — not just "atomic checkbox leaves" in the abstract. |
| **Task Planner** — small implementable tickets | `function_breakdown` stage (tag `Func`) | Rename tag to `Tickets`; **broaden scope to every leaf, not just code-writing ones.** Each leaf must bottom out in one mechanically-executable ticket: `implement collect_news(...)`, but equally `run \`uv init\``, `add [build-system] to pyproject.toml`, `curl /summary.json and assert the JSON shape`. If a leaf isn't already that concrete, split it here — this is the stage that owns "is this literally one tool call?", for every leaf type. |
| **Program Manager** — validate tickets, approve/reject with expected changes | `product_owner` phase — whole-plan accept/reject only | **New per-ticket review mechanism** (see Scope §4): instead of one plan-wide verdict, review each leaf and record an explicit verdict + expected-changes note on THAT leaf. Only rejected leaves go back for rework; approved ones proceed to `imp` immediately. Eliminates the byte-identical-replan failure mode directly. |

## Scope

### 1. Rename/reframe the Developer stage (was `journeyman`)

- [ ] In `PLANNER_STAGES` (`src/JFI/runner.py`), change the tag from
      `"Journy"` to `"Dev"`.
- [ ] Rewrite the `journeyman` branch of `get_system_message`
      (`simple_session_manager.py`) so its job is explicitly: for each
      Team-Lead-level item, name the actual **files** involved (new or
      existing) and, per file, the **functions/responsibilities** it needs
      — not just "break into atomic leaves" in the abstract. Keep the
      existing depth guardrails (`MAX_LEAF_DEPTH`,
      `MAX_INVESTIGATION_DEPTH`) and the "one leaf at a time" discipline —
      both are already working correctly (no evidence in the session data
      that Journeyman itself caused runaway depth or blew reasoning caps).
- [ ] Update the completion marker name if the stage is meaningfully
      redefined (`JOURNEYMAN_STAGE_COMPLETE` → `DEVELOPER_STAGE_COMPLETE`),
      or keep the marker and just change the prompt/tag — the marker name
      itself is internal, no user-visible cost either way. Pick whichever
      keeps `_marker_present`/resume-scanning (`runner.py`'s
      `start_index` scan added for stage-resume, see
      `test_run_phase_planner_resumes_after_the_last_completed_stage`)
      working correctly for BOTH old, already-in-flight sessions (marker
      unchanged) and new ones — needs a decision, not just a rename.

### 2. Broaden Task Planner (was `function_breakdown`) to every leaf, not just code

- [ ] Rewrite the `function_breakdown` branch of `get_system_message` so it
      no longer says *"Testing leaves, and any Implementation leaf that is
      NOT itself writing code, are OUT OF SCOPE for this pass"* — instead,
      every leaf (code or not) gets checked for whether it's already ONE
      mechanical action. Add explicit worked examples for non-code leaves:
      `uv init`, `uv sync`, adding one `pyproject.toml` section, a single
      curl+assert — mirroring the existing code-leaf worked example
      (`implement collect_news(...)`) so the model has a concrete pattern
      for non-code tickets too, not just an abstract instruction.
- [ ] Rename tag `"Func"` → `"Tickets"` in `PLANNER_STAGES`.
- [ ] Re-verify the existing "AT LEAST 2 children" / "don't split a
      genuinely-one-piece leaf" balance still holds once non-code leaves
      are in scope — a leaf like "run `uv sync`" must stay ONE leaf, never
      get artificially split.

### 3. Reduce the ~225 read/verify calls' overhead

Not a pipeline-role change, but directly evidenced (225 of 306 tool calls
were `execute_command`/`get_plan`/`get_leaf`, almost all before any code
existed):

- [ ] Give the Program Manager pass (§4) explicit incremental-review
      guidance: on a re-review after a targeted fix, only re-check the
      leaf(s) that changed plus their direct dependencies — not the entire
      repo from scratch. The old Product Owner prompt has no such
      incremental mode; it re-derives everything every single pass, which
      is exactly what cost the ~2-hour byte-identical cycle.
- [ ] Consider whether `context_save`'d facts from an EARLIER Program
      Manager pass should be explicitly surfaced to a LATER one
      (`context_lookup` is pull-based today, easy to skip) so re-verifying
      an unchanged fact isn't repeated from scratch every iteration.

### 4. Program Manager: per-ticket approve/reject with expected changes

This is the actual fix for the ~2-hour wasted replan cycle — the highest-
value change in this file.

- [ ] Design decision needed before implementing (recommend option A):
  - **(A) New tool, `review_leaf(leaf_id, verdict, expected_changes="")`**
    — `verdict` is `"approved"` or `"rejected"`. Persists a review verdict
    per leaf (new `Leaf` field, e.g. `review_status`/`review_note`, or a
    new small table keyed by `leaf_id` — mirrors how `SessionNote` already
    keys feedback by kind). `imp` only starts a leaf whose review_status is
    `approved` (or leaves review-status untouched/optional for a plan that
    never uses this mechanism, so it's backward compatible). A rejected
    leaf's `expected_changes` becomes the trigger text for JUST that leaf
    going back to whichever planner stage owns it — not a full replan.
  - **(B) Reuse `write_plan_feedback`, but require it to be structured**
    (one entry per leaf id, each with its own verdict) instead of prose —
    cheaper to build, but loses the "approved leaves proceed immediately"
    property that's the actual point of this redesign; not recommended.
- [ ] Rewrite the `product_owner` system prompt
      (`simple_session_manager.py`) to call `get_plan()` once, then
      `review_leaf(...)` on every genuine leaf (never on a parent — same
      guard `mark_leaf_done` already has), inspecting the real repo state
      per leaf the same way it does today, but recording a verdict instead
      of only being able to say APPROVED/REJECTED for everything at once.
- [ ] Rewire `runner.py`'s phase-advance logic: `imp` should be able to
      start on any leaf already marked `approved`, even while other leaves
      are still `rejected`/unreviewed — i.e., Program Manager review and
      Implementation can pipeline instead of being strictly sequential
      phases. (Bigger change — may want a follow-up ticket/spec of its own
      rather than folding into this same pass; flag for a `todo_v2.md` if
      the sequencing turns out non-trivial.)
- [ ] Tests: mirror the existing `test_tiered_planner.py`/
      `test_plan_db_tools.py` style — a `review_leaf` happy path, the
      parent-rejection guard, and a regression test asserting a rejected
      leaf's rework does NOT touch already-approved leaves (the exact
      byte-identical-replan failure this exists to prevent).

### 5. Circuit breaker on repeated rejection (defense in depth, even with §4)

- [ ] However §4 lands, add an explicit cap: if the SAME leaf gets rejected
      N times in a row (recommend N=3, matching the existing
      `_is_retryable_llm_error`/review-fail-iteration cap conventions
      elsewhere in `runner.py`) with no change to its description between
      attempts, stop looping — surface it for the user rather than
      burning further turns. Directly prevents a repeat of the observed
      ~2-hour zero-progress cycle even if some other future gap in §4
      reintroduces the same failure mode.

## Out of scope for v1

- Any change to the digest/compression mechanism itself (already has
  caps, live streaming — see prior session work) — the 73 digest calls
  here are a SYMPTOM of the turn-heavy planning loop (§4 fixes the cause),
  not a separate bug to chase.
- Speeding up the local LLM backend itself (LM Studio config, model
  choice) — infrastructure, not JFI code; already covered by
  recommendations given directly in conversation, not repeated here.
- The `imp`/`testing`/`reviewer`/`cleanup` phases' own prompts — this
  session never reached them, so there's no evidence here about whether
  they have analogous problems. Revisit once §4 gets a real plan past
  Product Owner and into `imp` for the first time on a session like this
  one.
