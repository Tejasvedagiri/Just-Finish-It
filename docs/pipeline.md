# The JFI pipeline

How one JFI session runs, end to end. Start here, then read the per-phase
docs:

| Phase key | Doc | Shown in the UI as |
|-----------|-----|--------------------|
| `planner` | [phase-planner.md](phase-planner.md) | Planner (Architect → Lead → Task, judged) |
| `imp` | [phase-imp.md](phase-imp.md) | Implementation (one Dev episode per leaf) |
| `reviewer` | [phase-reviewer.md](phase-reviewer.md) | Reviewer (the e2e, then fix or report) |
| `cleanup` | [phase-cleanup.md](phase-cleanup.md) | Cleanup |

All phases share the plan tree, described in [plan-tree.md](plan-tree.md).

These docs describe the code as it stands. If they disagree with the code,
**trust the code** and fix the doc in the same change. Symbols are cited by
name (not line number) so they survive edits. Each role's prompt lives with
its code: `JFI.planner.prompts`, `JFI.imp.prompts`, `JFI.review.prompts`.

JFI's first pipeline (v1: one long, compressed conversation per phase, a tiered
Arc → Lead → Dev → Task Planner planner, a Program Manager phase and a testing
phase) was removed. Its sessions can't be resumed (`_run_session` refuses them
by `SessionRecord.pipeline_version`), but `export-db` still reads them, so the
`product_owner` / `testing` enum values and v1 columns stay.

## Call chain

```
runner.main()
 └─ run_pipeline()          loops sessions (Ctrl+N starts a new one)
     └─ _run_session()      session name + goal, tool wiring, the outer iteration loop
         └─ run_phase(phase)
              ├─ planner  → _run_planner  → JFI.planner.loop.Planner.run()
              ├─ imp      → _run_imp      → JFI.imp.dev.Imp.run()
              ├─ reviewer → _run_reviewer → JFI.review.Reviewer.run()   (+ the fix loop)
              └─ cleanup  → _run_cleanup  → JFI.review.run_cleanup()
```

`PHASES = ["planner", "imp", "reviewer", "cleanup"]` in `runner.py`. These
keys and their `<PHASE>_COMPLETE` markers are persisted in history and drive
resume. **Never rename them.** Change display text only in
`PHASE_DISPLAY_NAMES` (`manager/abstract_manager.py`). Every marker is written
from DB state (every node GOOD, every leaf finished, the review's outcome),
never from model text.

## One session, step by step

1. **Session name → `SimpleSessionManager`.** It takes the project-wide
   `.jfi/.lock`, so only one session runs per project at a time. Resuming a
   v1 session stops here with an error.
2. **Tool wiring.** `TOOL_MAP` entries that need the DB are rebound to this
   session (`make_context_tools`, `make_note_tools`, `ssm.plan_db_tools()`,
   `make_process_tools`, `make_gated_execute_command`).
3. **Goal.** A fresh session asks for the goal and records it once as
   `My goal is: <goal>` (`GOAL_PREFIX`; `_session_goal` reads it back). A
   resumed one relies on history.
4. **Phase loop.** `ssm.get_remaining_phases(PHASES)` returns the phases from
   the first one with no `<PHASE>_COMPLETE` marker; `run_phase` runs each.
   After the reviewer, the reviewer notes are cleared (`_clear_reviewer_notes`).
5. **After cleanup → `collect_next_iteration`.**
   - A `REVIEW_REPORT` note (missing work, or fixes that failed for
     `MAX_REVIEW_ITERATIONS` rounds) is cleared and becomes the next
     iteration's feedback.
   - Anything the user queued meanwhile is folded into the same feedback.
   - Neither: "PIPELINE COMPLETE — IDLE", waiting for queued input.
6. **Next iteration.** A `USER FEEDBACK FOR ITERATION` message is added and all
   `PHASES` run again; `_replan_feedback` hands the feedback to the Architect in
   extend mode. `MAX_REVIEW_ITERATIONS` failed reviews end the run;
   `REVIEW_LOOP_APPROVAL=1` adds a human Approve/Reject before each retry.

## Episodes (`JFI.episode`)

Every LLM call belongs to one **episode**: a short conversation about one node
(or the whole project, for the Architect, reviewer and cleanup).

- **Brief:** a never-trimmed SCOPE anchor (role, node, done_when, files, path,
  why, how to finish), the role prompt, and one-line runbook/design indexes.
  Everything else is pulled with tools.
- **Tools:** the role's core set (`roles.ROLE_CORE_TOOLS`) plus an optional
  pool (`roles.OPTIONAL_POOL`: browser, screenshots, `ask_llm`, background
  processes, the context cache) that the episode can `load_tool` for itself.
- **Ends on:** the role's finish tool (`finish` / `mark_leaf_done`), the token
  budget (`CONTEXT_SIZE × CONTEXT_COMPRESSION_RATIO`), the turn cap (`MAX_EPISODE_TURNS`), or a stop.
  Older tool results are trimmed once the conversation passes 75% of its budget.
- **Before each turn:** pause (Ctrl+P) is honoured, and forced input (`!text`)
  goes into the episode as a `USER INTERJECTION` (also stored as a `Directive`).
  The header gets the request's tokens against the episode's budget.
- **Failed tool calls** (a result starting with `Error`) get `AUTO-RECTIFY`
  coaching (`episode/rectify.py`), and after `FAILURE_RETRY_LIMIT` identical
  failures the model is told to change approach.
- **Failed LLM calls** (`llm/retry.py`): a dropped connection, a 5xx, or a
  reply cut off by the output cap is retried `LLM_RETRY_LIMIT` times
  automatically; then (or straight away for anything else) the user is asked
  "Retry, or stop the run?". The run never gives up by itself.
- **Debugging:** `LOG_LLM_CALL_DEBUG=1` appends every request/response to
  `.jfi/llm_debug.jsonl`; `SHOW_STREAM_PROMPTS=1` prints every request in full.

## Per-phase models

`PHASE_ENV_PREFIX` maps each phase to an env prefix (`PLANNER`, `IMP`,
`REVIEWER`, `CLEANUP`), and episode roles add a chain in front
(`JFI.episode.roles.ROLE_ENV_PREFIXES`): `ARCHITECT_`/`LEAD_`/`TASK_` then
`PLANNER_`, `DEV_` then `IMP_`. `llm/base_llm_stream.phase_env` resolves
`<PREFIX>_MODEL` first, then the shared `MODEL`; the same for URL, key,
temperature and `CONTEXT_SIZE`.

## Where state lives

Everything is in `.jfi/JFI.db`, keyed by `session_id`:

| State | Where |
|-------|-------|
| History | `HistoryMessage` rows, tagged with their `episode_id` |
| Plan | `Leaf` rows (see [plan-tree.md](plan-tree.md)); verdicts in `PlannerVerdict`, events in `PlanEvent` |
| Runbook, design | `RunbookEntry`, `DesignEntry` |
| Episodes, directives | `Episode`, `Directive` |
| Context cache | `context_save` / `context_lookup` |
| Reviewer notes, review report | `SessionNote` rows (`REVIEWER_NOTES`, `REVIEW_REPORT`) |
| Queued requests, processes | their own tables |

Resume: the planner and imp re-read the DB every step, so resuming is "run the
phase again"; the phase-level markers in history say which phases are done.

## Known gaps (verify before relying on these)

- **Resume can misjudge a later iteration.** `get_remaining_phases` scans
  *all* history for markers. Once iteration 1 has finished, every
  `<PHASE>_COMPLETE` marker is present, so a crash midway through iteration
  2+ likely resumes as "All phases have already been completed". This is
  from reading the code; there's no test for it.
