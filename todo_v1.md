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
5. **Every stage does one giant pass over the WHOLE tree before the next
   stage starts.** Today's tiered planner is breadth-first ACROSS STAGES:
   Architect adds all 5 top-level items, THEN Team Lead breaks down all 5
   of them before advancing, THEN Journeyman walks every branch, THEN
   Function Breakdown walks every branch again. Each stage's own turn loop
   has to hold the ENTIRE tree's context for the whole pass — directly
   inflating both the 176-turn count and the 73 digest calls (a bigger
   working set ages out of context faster). It also means nothing is
   reviewable by Product Owner until ALL 24 leaves exist, so even a defect
   in leaf 1.1 can't surface until the entire tree — leaves 1 through 5's
   worth — has been fully planned.

## The new pipeline

Maps directly onto today's tiered planner (`runner.PLANNER_STAGES` in
`src/JFI/runner.py`) plus the `product_owner` phase
(`simple_session_manager.py`). Two stages are kept, two are reframed, one
(Program Manager) gets a real per-ticket mechanism it doesn't have today.

### Depth-first, per-branch — not breadth-first, per-stage

The single biggest structural change, and it's not just a prompt rewrite:
**walk the tree one branch at a time, all the way down to approved
tickets, before starting the next sibling branch** — instead of today's
design, where each stage sweeps the ENTIRE tree before the next stage
starts.

Worked example, as given directly:

- Arc (one pass, whole tree): creates top-level items `1, 2, 3, 4, 5`.
- Lead gets node `1` → creates `1.1, 1.2, 1.3`. Node `1`'s branch keeps
  going (Dev, Task Planner, Program Manager — all scoped to node `1`'s
  subtree only) before Lead is ever asked about node `2`.
- Once node `1`'s entire branch is fully ticketed and approved, Lead gets
  node `2` → creates `2.1, 2.2`.
- Dev gets `2.1` → creates `2.1.1` and so on, then Dev gets `2.2`, and so
  on down each branch before the NEXT top-level node starts.

Why this is better than the current design, tied directly to the root
causes above:

- **Smaller context per turn.** A stage only ever holds ONE branch's worth
  of tree, not all 24 leaves at once — directly attacks root cause #5 (and
  by extension #3: fewer/cheaper digest calls, since there's less to age
  out of a smaller working set).
- **Review happens per-branch, not per-plan.** Program Manager reviews
  node `1`'s tickets as soon as Task Planner produces them — it never
  waits for nodes `2`-`5` to exist first. A defect caught in branch `1`
  gets fixed and re-reviewed WITHIN branch `1`, without branches `2`-`5`
  ever being regenerated or re-touched. This is what actually prevents the
  byte-identical ~2-hour replan cycle (root cause #1) — it's not just that
  rejection becomes per-ticket (§5 below), it's that a rejection can no
  longer implicate work outside its own branch at all.
- **Implementation can plausibly start sooner.** Once branch `1` is fully
  approved, its tickets are ready for `imp` even while branch `5` hasn't
  been planned yet — real pipelining becomes possible instead of a strict
  "all planning, then all implementation" split (see §5's note on this).

This changes the tiered planner's CONTROL FLOW, not just its prompts —
`_drive_turn_loop`'s per-stage marker (`ARCHITECT_STAGE_COMPLETE`, etc.)
currently fires once per stage, for the whole tree. Under this design it
needs to fire once per (node, stage) pair instead — see Scope §1.

| Role (requested) | Today | Change |
|---|---|---|
| **Arc** — vision | `architect` stage (tag `Arc`) | Keep as-is: one pass, whole tree, top-level shape only. The one stage that stays breadth-first — it's producing the list of branches everything else will walk depth-first. |
| **Lead** — modules | `team_lead` stage (tag `Lead`) | Scope to ONE top-level node per invocation (its own children only), not the whole tree — see the depth-first walk above. |
| **Developer** — files & functions | `journeyman` stage (tag `Journy`) — already literally titled "JOURNEYMAN DEVELOPER" in its own prompt | Rename tag to `Dev`; scope to ONE Lead-created node per invocation; reframe the prompt to decompose it into concrete **files** and, within each file, the **functions/responsibilities** it needs — not just "atomic checkbox leaves" in the abstract. |
| **Task Planner** — small implementable tickets | `function_breakdown` stage (tag `Func`) | Rename tag to `Tickets`; scope to ONE Dev-created node per invocation; **broaden scope to every leaf, not just code-writing ones.** Each leaf must bottom out in one mechanically-executable ticket: `implement collect_news(...)`, but equally `run \`uv init\``, `add [build-system] to pyproject.toml`, `curl /summary.json and assert the JSON shape`. If a leaf isn't already that concrete, split it here. |
| **Program Manager** — validate tickets, approve/reject with expected changes | `product_owner` phase — whole-plan accept/reject only | **New per-ticket review mechanism** (see Scope §5), scoped to ONE branch's freshly-ticketed leaves as soon as Task Planner finishes that branch — not the whole plan at once. Only rejected leaves go back for rework, within the same branch; approved ones are ready for `imp` immediately. |

## Scope

### 1. Depth-first per-branch traversal (structural prerequisite for everything below)

The highest-risk, highest-value ticket in this file — every other section
assumes this lands first.

- [ ] Restructure `run_phase`'s tiered-planner loop (`src/JFI/runner.py`,
      currently a flat `for stage in PLANNER_STAGES: ... _drive_turn_loop`)
      into a recursive/iterative walk: Arc runs once for the whole tree:
      then for EACH top-level node it created, in order, drive that node's
      entire branch through Lead → Dev → Task Planner → Program Manager
      before moving to the next top-level node.
- [ ] Completion markers need to become per-(node, stage), not per-stage.
      `_marker_present`/`ARCHITECT_STAGE_COMPLETE`-style bare markers
      can't distinguish "Lead finished node 1" from "Lead finished node 2"
      — needs either a marker that names its node id (e.g.
      `LEAD_STAGE_COMPLETE:1`) or an equivalent DB-backed signal (e.g.
      checking whether every leaf under node 1 already has children/status
      set the way Lead's own job would leave them). Prefer the DB-backed
      check if feasible — same philosophy as the depth checks in
      `plan_db_tools.py` (mechanical enforcement, not marker-parsing).
- [ ] Resume behavior: a session stopped mid-branch must resume at the
      correct node AND stage — extend the existing resume-scan added for
      `test_run_phase_planner_resumes_after_the_last_completed_stage`
      (currently scans for the first INCOMPLETE stage across the whole
      tree) to scan for the first INCOMPLETE (node, stage) pair instead.
- [ ] Each stage's system prompt (Lead/Dev/Task Planner) needs the current
      node id passed in explicitly (which branch am I working on right
      now), not just "the plan" generically — `get_system_message`'s
      signature and the phase-trigger text both need a node-id parameter.
- [ ] Decide whether Arc's own top-level items must ALL exist before Lead
      starts on node 1, or whether Arc could also be incremental (produce
      node 1, hand off immediately, come back for node 2 later). Recommend
      keeping Arc as one whole-tree pass for v1 (matches the worked
      example exactly: "Arc creates 1, 2, 3, 4, 5" as a single step) —
      revisit only if Arc itself turns out to be a bottleneck once this
      ships.
- [ ] Tests: a fake multi-branch tree exercising the walk order (node 1's
      entire subtree fully resolved before node 2's Lead pass ever
      starts — mirror `test_tiered_planner.py`'s `_RecordingConsole`
      pattern, tracking stage+node calls in order) and a resume test
      (stop mid-branch-2, resume, confirm branch 1 is untouched and
      branch 2 picks up where it left off, not from node 1 again).

### 2. Rename/reframe the Developer stage (was `journeyman`)

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

### 3. Broaden Task Planner (was `function_breakdown`) to every leaf, not just code

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

### 4. Reduce the ~225 read/verify calls' overhead

Not a pipeline-role change, but directly evidenced (225 of 306 tool calls
were `execute_command`/`get_plan`/`get_leaf`, almost all before any code
existed) — and partly subsumed by §1 (smaller per-branch scope naturally
means less to re-verify per pass), but worth calling out explicitly too:

- [ ] Give the Program Manager pass (§5) explicit incremental-review
      guidance: on a re-review after a targeted fix, only re-check the
      leaf(s) that changed plus their direct dependencies — not the entire
      repo from scratch. The old Product Owner prompt has no such
      incremental mode; it re-derives everything every single pass, which
      is exactly what cost the ~2-hour byte-identical cycle.
- [ ] Consider whether `context_save`'d facts from an EARLIER Program
      Manager pass should be explicitly surfaced to a LATER one
      (`context_lookup` is pull-based today, easy to skip) so re-verifying
      an unchanged fact isn't repeated from scratch every iteration.

### 5. Program Manager: per-ticket approve/reject with expected changes

Together with §1 (depth-first traversal), this is the actual fix for the
~2-hour wasted replan cycle.

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
      (`simple_session_manager.py`) so it's scoped to the ONE branch Task
      Planner just finished (per §1) — inspecting the real repo state
      against just that branch's tickets, then calling
      `review_leaf(...)` on each of its genuine leaves (never on a parent
      — same guard `mark_leaf_done` already has) — recording a verdict per
      leaf instead of one APPROVED/REJECTED for the whole plan.
- [ ] Rewire `runner.py`'s phase-advance logic: with §1 landed, a branch's
      approved tickets are ready for `imp` as soon as THAT branch clears
      Program Manager — evaluate seriously whether `imp` can start on
      branch 1's tickets while branch 2 is still being planned, rather
      than waiting for the whole tree. This is a bigger change to the
      phase loop (today: planner fully completes, then product_owner, then
      imp, strictly sequential) — if it turns out non-trivial, ship §1+§5
      with the existing strict phase order first (still a large
      improvement on its own: per-branch review without full replans) and
      split real phase-pipelining into a follow-up `todo_v2.md`.
- [ ] Tests: mirror the existing `test_tiered_planner.py`/
      `test_plan_db_tools.py` style — a `review_leaf` happy path, the
      parent-rejection guard, and a regression test asserting a rejected
      leaf's rework does NOT touch already-approved leaves in the SAME
      branch, let alone other branches (the exact byte-identical-replan
      failure this exists to prevent).

### 6. Circuit breaker on repeated rejection (defense in depth, even with §5)

- [ ] However §5 lands, add an explicit cap: if the SAME leaf gets rejected
      N times in a row (recommend N=3, matching the existing
      `_is_retryable_llm_error`/review-fail-iteration cap conventions
      elsewhere in `runner.py`) with no change to its description between
      attempts, stop looping — surface it for the user rather than
      burning further turns. Directly prevents a repeat of the observed
      ~2-hour zero-progress cycle even if some other future gap in §1/§5
      reintroduces the same failure mode.

## Out of scope for v1

- Any change to the digest/compression mechanism itself (already has
  caps, live streaming — see prior session work) — the 73 digest calls
  here are a SYMPTOM of the turn-heavy, whole-tree planning loop (§1/§5
  fix the cause), not a separate bug to chase.
- Speeding up the local LLM backend itself (LM Studio config, model
  choice) — infrastructure, not JFI code; already covered by
  recommendations given directly in conversation, not repeated here.
- The `imp`/`testing`/`reviewer`/`cleanup` phases' own prompts — this
  session never reached them, so there's no evidence here about whether
  they have analogous problems. Revisit once §1/§5 get a real branch past
  Program Manager and into `imp` for the first time on a session like this
  one.
