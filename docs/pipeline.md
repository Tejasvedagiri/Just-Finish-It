# The JFI pipeline

How one JFI session runs, end to end. Start here, then read the per-phase
docs:

| Phase key | Doc | Shown in the UI as |
|-----------|-----|--------------------|
| `planner` | [phase-planner.md](phase-planner.md) | Planner (Arc → Lead → Dev → Task Planner) |
| `product_owner` | [phase-product-owner.md](phase-product-owner.md) | Program Manager |
| `imp` | [phase-imp.md](phase-imp.md) | Implementation |
| `testing` | [phase-testing.md](phase-testing.md) | Testing |
| `reviewer` | [phase-reviewer.md](phase-reviewer.md) | Reviewer |
| `cleanup` | [phase-cleanup.md](phase-cleanup.md) | Cleanup |

All phases share the plan tree. It's described in [plan-tree.md](plan-tree.md).

These docs describe the code as it stands. If they disagree with the code,
**trust the code** and fix the doc in the same change. Symbols are cited by
name (not line number) so they survive edits. Every prompt quoted here lives
in `src/JFI/session/simple_session_manager.py` (`get_system_message`,
`get_phase_trigger`) unless noted.

## Call chain

```
runner.main()
 └─ run_pipeline()          loops sessions (Ctrl+N starts a new one)
     └─ _run_session()      session name + goal, tool wiring, the outer iteration loop
         ├─ run_phase(phase)                       for every phase except product_owner
         │    └─ _drive_turn_loop(completion_marker)  one LLM turn after another
         └─ _run_product_owner_loop()              product_owner ⇄ planner, before imp
```

`PHASES = ["planner", "product_owner", "imp", "testing", "reviewer", "cleanup"]`
in `runner.py`. These keys and their `<PHASE>_COMPLETE` markers are
persisted in history and drive resume. **Never rename them.** Change display
text only in `PHASE_DISPLAY_NAMES` (`manager/abstract_manager.py`).

## One session, step by step

1. **Session name → `SimpleSessionManager`** (or `AdaptiveSessionManager`,
   the default; see `SESSION_MANAGER`). It takes the project-wide
   `.jfi/.lock`, so only one session runs per project at a time.
2. **Tool wiring.** `TOOL_MAP` entries that need the DB are rebound to this
   session (`make_context_tools`, `make_note_tools`, `ssm.plan_db_tools()`,
   `make_gated_execute_command`, ...).
3. **Goal.** A fresh session asks for the goal. A resumed one uses
   `"(Resuming previous session goal from history)"` and relies on history.
4. **Phase loop.** `ssm.get_remaining_phases(PHASES)` returns the phases from
   the first one with no `<PHASE>_COMPLETE` marker in history. For each:
   - `get_phase_trigger(phase, ...)` produces the opening user message. It's
     added unless the last user message already contains it.
   - `run_phase` drives the phase to its marker.
   - `product_owner` is special: `_run_product_owner_loop` owns it (see its doc).
   - After `reviewer`, the reviewer notes are cleared (`_clear_reviewer_notes`).
5. **After cleanup → `collect_next_iteration`.**
   - A `REVIEW_REPORT` note (the reviewer called `write_review_report`) means
     the review failed. It's cleared and becomes the next iteration's feedback.
   - Anything the user queued meanwhile is folded into the same feedback.
   - Neither present: show "PIPELINE COMPLETE — IDLE" and wait for queued input.
6. **Next iteration.** A `USER FEEDBACK FOR ITERATION` message is added, and
   *all* `PHASES` run again. `MAX_REVIEW_ITERATIONS = 3` failed reviews end
   the run. `REVIEW_LOOP_APPROVAL=1` adds a human Approve/Reject before each
   retry.

## The turn loop (`_drive_turn_loop`)

Every phase and every planner stage is driven by this one loop:

- **Before each turn:** it honours pause (Ctrl+P), forced input, and skip
  requests (Ctrl+K / Ctrl+Q, `imp`/`testing` only). It also updates the
  status bar.
- **Messages:** `ssm.get_messages(phase)` = the phase's system prompt (the
  planner's depends on the current stage/node) + compressed history. For
  `imp`/`testing`, the system prompt also carries the pending-leaf work
  queue with `[id=N]` tags.
- **Tools:** every tool except `load_tool` is **deferred**. The model sees
  one-line summaries and must call `load_tool` to unlock a tool's schema.
  Unlocked tools are persisted per session.
- **LLM errors:** retryable errors get `LLM_RETRY_LIMIT` automatic retries.
  After that, the user picks Retry / Stop; the run never dies on its own. A
  `ResponseTooLongError` (`STREAM_OUTPUT_CAP`) adds a one-off "commit to one
  action" directive to the retry.
- **Tool calls:** each one goes through `execute_tool_call`. A result
  starting with `Error` counts as a failure, which triggers `AUTO-RECTIFY`
  coaching and per-signature retry counting (`FAILURE_RETRY_LIMIT`).
- **Completion:** the loop ends when the assistant's *content* contains the
  expected marker **alone on its own line** (`_marker_present`). It's checked
  even when the same message also had tool calls. A marker mentioned inside
  a sentence doesn't count.
- **Stall handling:**
  - No tool calls and no marker: a "please continue … output
    '<MARKER>'" nudge.
  - Reasoning only, no content: a sharper `AUTO-RECTIFY` telling the model
    to take exactly one action.
- **Stuck leaf:** if the same current leaf lasts longer than
  `TASK_STUCK_TIME_LIMIT_SECONDS` (default 300) or spends more than `TASK_STUCK_TOKEN_LIMIT`
  re-sent tokens, `_stuck_task_directive` tells the model to split it. Only
  leaves that were `start_leaf`'d count, so in practice this applies only to
  `imp`/`testing`.

## Per-phase models

`PHASE_ENV_PREFIX` maps each phase to an env prefix (`PLANNER`,
`PRODUCT_OWNER`, `IMP`, `TESTING`, `REVIEWER`, `CLEANUP`).
`llm/base_llm_stream.phase_env` resolves `<PREFIX>_MODEL` first, then the
shared `MODEL`. The same applies to URL/key/temperature, so any phase can
run on a different model. `ask_llm` is rebound per phase to that phase's
model.

## Where state lives

Everything is in `.jfi/JFI.db`, keyed by `session_id`:

| State | Where |
|-------|-------|
| History | history table (append-only; compressed only in the view sent to the LLM) |
| Plan | `Leaf` rows (see [plan-tree.md](plan-tree.md)) |
| Context cache | `context_save` / `context_lookup` |
| Reviewer notes, review report, plan feedback | `SessionNote` rows (`REVIEWER_NOTES`, `REVIEW_REPORT`, `PLAN_FEEDBACK`) |
| Queued requests, processes, unlocked tools | their own tables |

Resume is driven entirely by **markers in history**. Nothing
phase-positional is stored anywhere else.

## Context compression

The DB never loses history. `compress_history` only shapes what gets *sent*:

- The last `KEEP_RECENT_BLOCKS` turns go verbatim.
- Older tool results are trimmed to head/tail.
- Aged-out turns are folded into a digest message, LLM-summarized with the
  phase's own model once enough has accumulated
  (`DIGEST_SUMMARY_MIN_CHARS`).

Anything the model must remember across compression belongs in
`context_save` (see `CONTEXT_CACHE_RULES`). That's why every phase prompt
pushes `context_lookup` at the start of new work.

## Rules shared by every phase prompt

Every phase's system prompt is built from these shared blocks:

- **`PLAN_FORMAT_RULES`** (or `CORE_PLAN_RULES` + a task-type addendum from
  `task_rules.py` when `AdaptiveSessionManager` detects python / javascript /
  go / sql / html-css / data-eng / story): how to use the plan tools.
- **`CONTEXT_CACHE_RULES`**: pull context with `context_lookup`, save
  run commands under `run_commands`.
- **`VERIFICATION_RULES`**: "verified" means a command actually ran, never
  just code-reading.
- **`deferred_tools_rules(unlocked_tools)`**: the list of still-locked tools.

Every phase is also told: never ask the user a question, never wait for
approval.

## Known gaps (verify before relying on these)

- **(Fixed) Re-planning didn't run under the tiered planner.** Stage
  markers are now scoped to a planning round; see
  [phase-planner.md](phase-planner.md#known-gaps).
- **Resume can misjudge a later iteration.** `get_remaining_phases` scans
  *all* history for markers. Once iteration 1 has finished, every
  `<PHASE>_COMPLETE` marker is present, so a crash midway through iteration
  2+ likely resumes as "All phases have already been completed". This is
  from reading the code; there's no test for it.
- **(Fixed) Runtime messages spoke markdown-plan.** They now name the
  plan tools (`add_leaf`, `update_leaf`, `split_leaf`).
