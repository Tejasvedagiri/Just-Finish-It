# Planner rewrite: implementation phases

How to build [laya_plan.md](laya_plan.md) in steps. Each phase is one
branch/PR that ships on its own, with the **whole test suite green**.
Section references (§, G, D) point into `laya_plan.md`.

Status: **draft for review.**

## Resuming this work (read first)

This rewrite spans many sessions. Any agent (or person) picking it up
should:

1. **Read, in order:**
   - [`AGENTS.md`](../AGENTS.md): repo conventions;
   - [`docs/pipeline.md`](pipeline.md) and the `docs/phase-*.md`
     files: how `v1` works today;
   - [`laya_plan.md`](laya_plan.md): the design (§10 decisions, §12 gaps,
     §13 database);
   - this file.
2. **Find the current phase** in the Progress table below. Its row says
   what's done, and the "Next step" line says exactly what to do next.
3. **Before changing anything,** check the Progress log's last entry
   against `git log` on the rewrite branch. If they disagree, trust git
   and fix the log.
4. **After each working session,** update the Progress table and add a
   Progress log entry: what was done, what's next, any decision made or
   question opened. If a decision changes the design, record it in
   `laya_plan.md` §10 too. An update that isn't written down here is lost
   to the next session.

## Progress

**Status: complete** (2026-09-29), on `feature/v2`. Every phase is built and
the full pipeline passed a real run end to end (the calc benchmark: planner →
imp → reviewer → cleanup, then the benchmark's own verifier 6/6). This file
and `laya_plan.md` moved to `docs/` as a reference. The Fleet repo's matching
change (phase 10) is made there, uncommitted.

Still worth measuring on bigger goals (not blocking): episode overflows,
escalations, how often the judge's tie-break fires with `LAYA=1`, and whether
the reviewer's fix loop converges when a leaf is really wrong.

| Phase | Status | Branch / PR | Notes |
|---|---|---|---|
| 0 Groundwork | **done** | `feature/v2` | re-plan bug (planning rounds), server token usage, DB-era wording |
| 1 Database schema | **done** | `feature/v2` | §13: new columns + 6 tables, portable column upgrade, export-db |
| 2 Runbook + design | **done** | `feature/v2` | tools + index lines + result cap; schemas kept out of v1's tool list |
| 3 Episode engine | **done** | `feature/v2` | `src/JFI/episode/`: brief + anchor, role tool sets, budget, turn cap, finish, directives, role models |
| 4 Code tools | **done** | `feature/v2` | stubs, mark_change, symbol read/replace (ast + brace matcher, no tree-sitter), search, markers |
| 5 Planner v2 (fallback judge) | **done** | `feature/v2` | `src/JFI/planner/`: nodes, prompts, loop; `JFI_PIPELINE`; `run_phase("planner")` dispatch; no PM phase in v2 |
| 6 Dev v2 | **done** (real run: calc, 13/13 leaves in one episode each) | `feature/v2` | `src/JFI/imp/`: queue, gated `mark_leaf_done`, deferral, restart/attempt cap, overflow split, setup/finish-up; testing phase skipped in v2 |
| 7 Reviewer + cleanup v2 | **done** (real run: calc, PASS confirmed by the e2e, cleanup in one episode) | `feature/v2` | `src/JFI/review/`: e2e episode, pass re-runs the e2e itself, `reopen_leaf` fix loop (G8), missing work → report; cleanup episode |
| 8 Laya judge | **done** | `feature/v2` | two scores (the rule + base Laya), LLM tie-break at `LAYA_MIN_CONFIDENCE` 0.75; no fine-tune (overfitted); G10 duplicates and G1.3 tool picks measured and not built (see the log) |
| 9 Document path | **done** | `feature/v2` | outline instead of runbook/tests, `passage` leaves, mechanical passage gate, reviewer pass without an e2e |
| 10 UI + reporting | **done** (Fleet change uncommitted there) | `feature/v2` | Judge stage, current node, plan fields pushed from episodes, dashboard Judge/Laya/LLM table + runbook/design views (the plan lines stay plain); the same table reaches the fleet as `plan_detail`; Dev reports each finished leaf's tokens and time |
| 11 v2 default | **done** (as part of 12) | `feature/v2` | v2 is the only pipeline; `JFI_PIPELINE` removed |
| 12 Remove v1 | **done** (2026-09-29, the user's call) | `feature/v2` | v1 deleted; after phase 7 the turn loop, history compression and session-wide tool unlocking went too |

Status values: `not started` / `in progress` / `in review` / `done`.

### Progress log

Newest first. One entry per working session.

- **2026-09-29 (end):** **The first full real run passes; the rewrite is
  complete.** The calc benchmark (`qwen/qwen3.8-27b`, `LAYA=1`, fleet and
  Streamlit live):
  - **Planner**, about 7 min: 3 components → 4 files → 13 leaves; every
    episode ended on `finish`. **Imp:** 13/13 leaves, one Dev episode each.
    **Review:** PASS in one episode (8 turns, 7.1k tokens), every leaf
    `passed`, the e2e verified. **Cleanup:** one episode. Checked
    independently in a fresh process: the benchmark's `verify_calc.py` 6/6,
    the project's pytest 7 passed.
  - **Two bugs found and fixed on the way:** (1) shared tools'
    results weren't capped -- the reviewer's `findstr` over a 4.7 MB log came
    back at 99,395 characters and ended the episode over budget after one
    turn; the engine now caps every result (`cap_result` is idempotent, so a
    tool's own hint survives). (2) A reviewer episode that ended on budget or
    turn cap returned "stopped", which ended the whole session; it now gets
    one fresh episode, like the planner and Dev. The run was resumed from the
    reviewer after the fix.
  - **Not JFI:** the first attempt died because LM Studio had reloaded the
    model with a 4096-token context (every reply cut off mid-reasoning); the
    test driver wrote its screen log inside the project, so the marker scan
    found the stubs' `JFI:` lines in it. Both fixed outside the repo.
  - The fleet's Task | Judge tab and Streamlit both showed the run live; the
    fleet's current-row highlight drew on every cell (fixed there).
  - `benchmark/expected_plans/` (hand-written reference plans for the
    stui goal, used while tuning the judge) deleted.
  - **Names:** v2 is the only pipeline, so nothing is called v2 any more:
    `models/v2.py` split into `models/episode.py` (Episode, Directive),
    `models/planning.py` (PlannerVerdict, PlanEvent) and
    `models/runbook_design.py`; `runner._run_*_v2` -> `_run_*`; the tests
    `test_planner.py`, `test_imp.py`, `test_reviewer.py`, `test_schema.py`.
    `uv run create-env` now always opens the setup wizard in a terminal.
  - **The episode budget is the context:** `EPISODE_TOKEN_BUDGET` (20k under
    the window) removed; an episode gets `CONTEXT_SIZE ×
    CONTEXT_COMPRESSION_RATIO` (D20 superseded). Overflow → split still works
    the same way, just at the model's real size.

- **2026-09-29 (late night, cont.):** **Laya is opt-in.** `JUDGE=laya|fallback`
  (Laya on by default) became `LAYA=1` (off by default): unset, the rule
  judges alone; set, the rule + Laya + the LLM tie-break as before (the
  user's call).

- **2026-09-29 (late night):** **The fleet gets the Task | Judge table; the
  last v1 leftovers go.**
  - **Run 15** (stui, planning only) reached imp in 2 h 23 min: 17 Architect,
    23 Lead and 58 Task nodes. Rule and Laya agreed on 17/17, 7/23 and
    17/58; every disagreement had Laya below 0.75, so the rule decided and
    the LLM tie-break never fired.
  - **Fleet:** it can't read the DB, so its Plan view only had the
    checklist. `plan_status_fields` now also pushes `plan_detail` (the
    `plan_judge_rows`, runbook and design) and the fleet
    (`Just-Finish-It-Fleet`, uncommitted) renders it as a Task | Judge tab.
    Same change there: the 4 v2 phases, `[○]` skipped leaves counted, and a
    Windows fix in `master.js` (assets were served as `text/html`).
  - **Streamlit** crashed with an `ImportError` after a hot reload (it
    reloads `dashboard.py` but not modules it already imported); a fresh
    process renders run 15 (checked with `AppTest`). `use_container_width`
    → `width="stretch"` (removed after 2025-12-31).
  - **Found dead after the cutover:** nothing called `track_file`, so a
    failed review's re-plan was told "No files have been tracked yet." (with
    v1 tool tips); `get_project_state_summary` now lists the done leaves'
    files. Nothing called `record_task_tokens` and Dev's status task was the
    bare description, so the fleet never marked a leaf current and its
    heatmap stayed empty; Dev now sets "1.2.3 description" plus a start time,
    sums `Leaf.tokens` over attempts and reports each finished leaf.
  - **Removed:** the `plan.md` progress fallback (`plan_progress`,
    `phase_progress`, `plan_path`, `DEFAULT_PLAN_PATH`, `PHASE_SECTION`,
    `has_leaves`, the dashboard's plan.md branch), `track_file` and the
    implemented-files metadata writes (the table stays for export-db),
    `test_plan_location.py`, and docstrings citing deleted files
    (`todo.md`, `plan_renumber.py`, the turn loop). Suite: 756 passed, 3
    failed (the POSIX-only process tests).

- **2026-09-29 (night):** **Phases 7-10 built; the judge is two scores; the
  cleanup.**
  - **Judge (the user's decision):** fine-tuning dropped -- `real-v5` answered
    BREAKDOWN at 0.98 for every Architect/Lead node and GOOD at 0.98 for every
    Task node on stui run 14 (it learned the level, not the text). Now every
    node gets the rule's verdict and base Laya's (`english`); they agree ->
    that; they disagree and Laya is >= `LAYA_MIN_CONFIDENCE` (0.75) -> one
    batched LLM tie-break per judge step (the Architect's model); otherwise
    the rule. `JUDGE` defaults to `laya`. `PlannerVerdict` gained
    `rule_verdict`, `tiebreak_verdict`, `decided_by`; the dashboard shows them
    side by side. The fine-tune repo and its HF model were deleted.
  - **Planner:** a Lead/Task episode that hits the turn cap is continued once
    even when it already added nodes (told which); an episode cut off by an
    LLM error is retried once, then planning stops instead of calling the
    node GOOD (stui run 12 lost three views to that).
  - **Episodes:** forced input (`!text`) now reaches the running episode; the
    optional tool pool is actually wired (it was empty); LLM failures ask
    Retry/Stop after the automatic retries instead of ending the episode;
    `LOG_LLM_CALL_DEBUG` / `SHOW_STREAM_PROMPTS` work for episodes.
  - **Phase 7:** `src/JFI/review/` (see docs/phase-reviewer.md).
  - **Phase 8, measured and not built:** base Laya's mean-pooled embeddings
    can't separate duplicates from ordinary siblings (348 real sibling pairs
    in run 14: median cosine 0.957, max 0.993; 4 true duplicates: 0.948-0.979),
    nor say when a node needs no optional tool (top-pick scores 0.93-0.95
    either way) -- so no G10 embedding check (the mechanical duplicate
    refusals in `nodes.py` stay) and no G1.3 tool picks (`load_tool` covers it).
  - **Phase 9:** a document goal records a `design` outline instead of a
    runbook/test framework; Lead scaffolds Markdown with `JFI: passage`
    placeholders; Task makes `passage` leaves; Dev's gate is mechanical (the
    placeholder gone, >= 80% of the asked length); the reviewer's pass for a
    document is "no placeholder left".
  - **Phase 10:** see the table. The Fleet repo still needs its PR (4 phases,
    the new stage tags).
  - **Cleanup:** removed `_drive_turn_loop` and everything only it used
    (history compression and digests, `get_system_message` and its rule
    blocks, session-wide `load_tool` / `UnlockedTool` unlocking, the deferred
    tool machinery, `get_phase_trigger`), plus the legacy `orchestrator/`,
    `tool/plan_renumber.py` and `utils/laya_poc.py`. `runner.py` 1,790 -> ~790
    lines, `simple_session_manager.py` 2,470 -> ~620.
  - **Tests:** Windows baseline (5 known failures); new
    `test_review_v2.py`, `test_document_path.py`, `test_v1_sessions_refused.py`.
    `uv run build` OK.

- **2026-09-29 (late):** **v1 removed; v2 is the only pipeline** (the
  user's decision, ahead of phases 7-11).
  - **Deleted:** the tiered planner and `PLANNER_SINGLE_PASS`, the Program
    Manager loop/prompt/tools (`review_leaf`, `write_plan_feedback`), the
    testing phase, `AdaptiveSessionManager` + `task_rules.py`
    (`SESSION_MANAGER`), v1's planner/imp/testing prompts and
    `PLAN_FORMAT_RULES`, v1's plan-editing tools (`add_leaf`, `split_leaf`,
    `merge_leaf`, `delete_leaf`, `update_leaf`, `reorder_leaf`,
    `start_leaf`, `mark_leaf_done`), the stuck-leaf split nudge
    (`TASK_STUCK_*`), the imp/testing work queue and Ctrl+K/Ctrl+Q skips.
  - **Kept:** the reviewer and cleanup (`_drive_turn_loop`, session-wide
    `load_tool`) until phase 7; `get_plan`/`get_leaf`; the enum values and
    v1 columns so old DBs still open for `export-db`.
  - `PHASES = ["planner", "imp", "reviewer", "cleanup"]`; every new
    session records `pipeline_version="v2"`; resuming a v1 session is
    refused with an error (`test_v1_sessions_refused.py`).
  - **Fleet repo:** it gets 4 phases now, not 6; its `product_owner` /
    `testing` columns need to go (phase 10's PR).
  - **Tests:** v1-only files deleted (tiered planner, product owner loop,
    task rules, adaptive manager, plan parsing, stuck-task split); the
    rest retargeted to the reviewer. Suite: Windows baseline only.
  - **Next:** phase 7 (reviewer + cleanup as episodes), which also retires
    `_drive_turn_loop` and the session-wide `UnlockedTool` unlocking; wire
    forced input (`!text`) into episodes as directives.

- **2026-09-29 (later):** **Laya judges the v2 planner; fine-tuning
  moved out.**
  - **Wiring:** `make_judge` picks `JUDGE=laya|fallback`; `LAYA_MODEL_PATH`
    is a local checkpoint folder or a Hugging Face repo id (downloaded on
    first use, `HF_TOKEN` for a private repo); `LAYA_DEVICE` defaults to
    cpu; `.env` is found in the cwd when JFI runs from source.
  - **Checkpoint:** `jfi-judge-english-real-v3`, published as
    `tejasvedagiri/Just-Finish-It-Laya-Finetune`. On 95 held-out real
    plan nodes: 80% at confidence 0.7 vs the fallback rule's 69%. Live stui
    run 11: agreed with the hand labels on 67/68 nodes (the miss: a
    400-line data module called GOOD).
  - **Moved:** `laya-finetuning/` and `src/laya_finetune/` are gone from
    this repo. Training data, `finetune`/`evaluate`/`upload` and the
    checkpoints live in the sibling **`Just-Finish-It-Laya-Finetune`**
    repo, which imports `JFI.planner.judge`'s questions (changing
    `questions_for()`/`build_state()` means retraining there). Older
    entries below that name `laya-finetuning/` refer to that code.
  - **Open:** the Task turn-cap coverage gap (continue on `turn_cap` even
    when the node already has children); the rest of phase 8 (G10
    duplicates in the judge step, tool picks, the Laya-less binary check).

- **2026-09-28 (cont. 2):** **First real v2 run: the `calc` benchmark
  passed.**
  - **Setup:** Windows, LM Studio
    `qwen/qwen3.8-27b`, fallback
    judge. It was driven through a ConPTY stand-in for `harness.py`, since
    there's no tmux on Windows.
  - **Result:** the hidden `verify_calc.py` passed 6/6, and the
    deliverable's own suite 16/16. That took 3 iterations and 24 minutes.
    Every episode stayed under 7.7k tokens. Re-plan (Architect extend
    mode), the Dev gate, the overflow → Task split, and resume all worked
    for real.
  - **Fixed after the run (from what it showed):**
    - The goal and the review feedback were cut at 600 chars in the
      Architect's brief, which dropped "place them under tests/" and the
      second review issue. The caps are now 16k/12k chars.
    - Briefs now carry the OS, shell and Python command
      (`episode/environment.py`). The runbook had been all `python3`
      (the Store alias here) and POSIX shell syntax under `cmd.exe`.
    - The setup/finish-up gate re-reads its runbook command. A fixed
      setup kept failing until the turn cap.
    - `add_node` refuses a leaf for a function that another unfinished
      leaf in the same file already covers. `evaluate()` was implemented
      twice.
    - The fallback judge accepts a node that already names one function
      (D31). "Implement parse()" had been restated at 3 levels.
    - Task/Lead/Architect prompt tightening: only your node's functions;
      one node per file; list every deliverable, including docs.
    - The v2 planner and imp report their phase to the UI.
  - **Still open:**
    - The fallback judge over-plans small chores. "Move 2 test files"
      became 19 nodes, which is Laya's job (phase 8).
    - There's no duplicate check for non-function leaves (two
      `tests/conftest.py` leaves).
    - `execute_command` can still read `.jfi/`.

- **2026-09-28 (cont.):** **Phase 6 done in code** (`src/JFI/imp/`):
  - **`queue.py`:** real GOOD leaves in tree order. A leaf waits for its own
    `depends_on` plus every leaf under any node an ancestor depends on
    (G6). A cycle across levels falls back to the first unfinished leaf
    instead of stalling.
  - **`dev.py` (`Imp`):**
    - One fresh Dev episode per leaf.
    - `mark_leaf_done` is the gate: it runs the runbook's `test_one` with
      the given `test_id` (or a `check` command), and only exit 0 marks
      the leaf done. Commands run with `PYTHONDONTWRITEBYTECODE=1`: a
      same-size one-character fix within the same second otherwise ran
      the stale `.pyc`, and the gate refused a correct fix (observed in
      the tests).
    - A failure on another leaf's stub (native traceback, or pytest's
      `file:line: NotImplementedError` mapped to its def) defers the
      leaf behind the leaf whose target symbol matches. Deferral doesn't
      count as an attempt and is capped at 3.
    - Restarts are briefed as a partial attempt. At `MAX_DEV_ATTEMPTS`
      or on a budget/turn-cap overflow, the leaf goes `BREAKDOWN` and the
      planner is re-run (a Task split). A leaf that can't be split is
      SKIPPED with a reviewer note.
    - `setup` runs first (one fix episode if it fails). `build` and the
      marker scan run last (one finish-up episode, the rest noted).
  - **`prompts.py`:** per kind: implement, integrate, modify, delete, fill,
    and generic (D31).
  - **Wiring:** `run_phase("imp")` dispatches v2 to `_run_imp_v2`
    (`IMP_COMPLETE` from DB state). v2 skips `testing` like
    `product_owner`, writing their markers so resume moves past them.
    `_replan_feedback` now reads only the loop's own `USER FEEDBACK FOR
    ITERATION:` messages, not the phase triggers.
  - **Other changes:**
    - The TASK split prompt says the original leaf becomes a parent.
    - `escalate` targets the layer above in both breakdown and redo.
    - `MAX_DEV_ATTEMPTS` is in `JFI_ENV_TEMPLATE`.
    - AGENTS.md points at the v2 packages.
  - **Tests:** `test/test_imp_v2.py` (9), which run real pytest in a
    subprocess. Full suite: 1019 passed, the 6 known Windows failures.
    ruff clean; build OK.
  - **Not yet:** a real-model run (the phase 6 exit criterion).

- **2026-09-28:** **Phase 5 done** (`src/JFI/planner/`):
  - **`nodes.py`:** the node tools (`add_node`, `update_node`,
    `delete_node`, `escalate`, `get_node`, `list_nodes`, `get_plan`) with
    creator-only edits, Lead/Task scoped to their one node, the depth cap,
    `PLANNER_ITEM_MAX_CHARS`, acyclic `depends_on`, and
    `PLANNER_ESCALATION_CAP`. `escalate` sends back the layer above's
    node: the scope node itself during a breakdown, its parent during a
    redo.
  - **`prompts.py`:** Architect create/extend/redo, Lead breakdown/redo,
    Task breakdown/split/redo (G2 project component + artifacts, G3
    `mark_change` / modify / delete leaves).
  - **`loop.py`:** `Planner.run()` takes the first pending action per
    level (judge -> redo -> breakdown), so the gates (D15) and resume come
    from DB state. It also has the redo cap, the overflow -> REDO
    "too_big" path, `MAX_PLANNER_EPISODES`, and `PlannerVerdict` /
    `PlanEvent` rows. An escalation during a breakdown is kept (not
    overwritten with GOOD).
  - **`judge.py`:** `FallbackJudge` (same interface as `LayaJudge`).
  - **Wiring:** `session/pipeline.py`. `SessionRecord.pipeline_version`
    comes from `JFI_PIPELINE` at creation. `run_phase("planner")` sends
    v2 sessions to `_run_planner_v2`, which uses the goal from the
    "My goal is:" trigger and the user messages since the last
    `PLANNER_COMPLETE` as extend feedback, and appends `PLANNER_COMPLETE`.
    v2 sessions skip `product_owner` (D3). The v2 env vars are in
    `JFI_ENV_TEMPLATE`.
  - **Not done:** the §5.3 token check forcing BREAKDOWN is not
    implemented. An oversized node is caught by the next role's episode
    overflowing instead.
  - **Tests:** `test/test_planner_v2.py` (9): the scripted Architect ->
    Lead -> Task run with stubs on disk, the gate, redo + cap, escalation
    + unpause, resume, the episode budget, the pipeline flag, and the
    `run_phase` dispatch. Full suite: 1010 passed, the 6 known Windows
    failures. ruff clean; `uv run build` OK.

- **2026-09-29 (cont.):** **Phase 4 done** (`tool/code_tools.py`):
  - **Writing:** `scaffold_file` (language-aware stubs whose bodies are
    generated, artifact `fill` skeletons, test files without failing
    stubs, append-only), `unscaffold_file` (only pure stubs),
    `mark_change`.
  - **Reading and editing:** `read_symbol` / `replace_symbol` /
    `list_symbols` (Python `ast`; brace matcher for JS/TS/Go/Rust),
    `search_code`, `list_dir`, `scan_markers`.
  - **Path safety:** refuses anything outside the root, `.git/` or
    `.jfi/`.
  - **Wiring:** the schemas are registered in `EpisodeTools`. Every role's
    core set is now complete except `escalate` (phase 5); per-role schema
    cost is 530–1,820 tokens (lead/dev a little over G1.2's ~1.5k
    target).
  - **tree-sitter:** not added.
  - **Tests:** `test/test_code_tools.py` (30; byte-identical round trips
    per language). Suite = baseline, 1000 passed.

- **2026-09-29 (cont.):** **Phase 3 done** (`src/JFI/episode/`):
  - **`run_episode()`:** one scoped LLM conversation, ending on `finish`,
    `budget`, `turn_cap`, `error` or `stopped`, with an `Episode` row and
    history rows tagged by `episode_id`.
  - **The brief:** a never-trimmed SCOPE anchor as the first thing in the
    system message, plus the runbook/design index lines.
  - **Tools:** per-role core tool sets (not-yet-built phase 4 tools are
    skipped automatically), and a per-episode `load_tool` limited to the
    optional pool.
  - **Budget:** capped at `CONTEXT_SIZE × ratio`; server usage is counted
    when the server reports it.
  - **Directives** are delivered once, to their node's next episode.
  - **Role models:** `phase_env` accepts a prefix chain
    (ARCHITECT→PLANNER→shared).
  - **Tests:** `test/test_episode_engine.py` (19). Suite = baseline, 971
    passed; build OK.
  - **Data (parallel):** the LM Studio rewriter now works (unique goals;
    distinct styles per batch; 16k token cap for reasoning models); the
    full `train.jsonl` rewrite is running.

- **2026-09-29 (cont.):** **Phase 2 done.**
  - `tool/runbook_tools.py` and `tool/design_tools.py`: set/get (upsert;
    a changed command resets `verified`), one-line index renderers for
    briefs, and `Error…` refusals.
  - `tool/result_cap.py`: the shared `TOOL_RESULT_MAX_TOKENS` cap with a
    "read more" hint, reused by phase 4's read tools.
  - Their schemas live beside the tools (`RUNBOOK_TOOL_SCHEMAS`,
    `DESIGN_TOOL_SCHEMAS`), **not** in `schemas.py`, so v1's
    deferred-tool list and prompts are unchanged.
  - **Tests:** `test/test_runbook_design_tools.py` (11). Suite = Windows
    baseline, 952 passed.

- **2026-09-29 (cont.):** **Phase 1 done** (schema, no behaviour change):
  - **Columns:** `Leaf` gets the v2 fields, `SessionRecord.pipeline_version`
    (default `v1`), `HistoryMessage.episode_id`.
  - **Tables:** `JFI.models.v2` has `Episode`, `PlannerVerdict`, `PlanEvent`,
    `RunbookEntry`, `DesignEntry` and `Directive`.
  - **Column upgrade:** `_ensure_columns` now works on every backend
    (SQLAlchemy inspector + portable ADD COLUMN); it used to be SQLite-only,
    so MySQL/Postgres never got new columns. The columns live in one
    `ADDED_COLUMNS` map.
  - **export-db:** shows the pipeline version, the v2 fields on plan lines,
    and runbook / design / episodes / verdicts / events / directives
    sections.
  - **Tests:** `test/test_v2_schema.py` (fresh DB, a real old-schema SQLite
    DB upgraded in place with its rows intact, idempotence, the
    Postgres/MySQL statements, uniqueness, export). Suite = Windows
    baseline (940 passed); `uv run build` OK.

- **2026-09-29:** Started implementing, on branch `feature/v2` (off
  `feature/laya`). **Phase 0 done:**
  - **Re-plan bug fixed:** stage markers are scoped to a planning round
    (`PLANNER_ROUND_START`, `_planning_round_start`); interrupted rounds
    resume, and pre-fix sessions behave as before (3 new tests in
    `test_tiered_planner.py`).
  - **Real token counts:** `OpenAICompatableStream` requests
    `stream_options.include_usage` and permanently drops it after a
    server's 400 about it. The console returns the server's `usage` on
    the parsed turn (`test_openai_stream_usage.py`).
  - **DB-era wording:** the feedback, iteration, stuck-leaf and
    AUTO-RECTIFY messages now name the plan tools.
  - **Checks:** suite = Windows baseline (6 known failures, 933 passed);
    ruff clean apart from pre-existing noise.
  - `laya-finetuning` data work (LM Studio rewrite of goals/nodes) is
    separate and still pending on `feature/laya`'s data.

- **2026-09-28 (night):** Fine-tuned the Laya judge; **target met.**
  - **Held-out 101-node test:** base `english` 44% → **full fine-tune 90%
    raw / 88% judge** (BREAKDOWN recall 95%, REDO precision 100%, redo
    reason 25/26). Head-only fine-tune: 87% / 89%.
  - **Data:** train 362 (hand-written + benchmark-derived + contrastive
    pairs); test 101 (other apps and benchmark tasks, split by problem).
  - **What moved it:** the contrastive GOOD-vs-BREAKDOWN pairs, plus
    augmentation that removes an empty-files → REDO shortcut.
  - **Tooling:** `laya-finetuning/finetune.py` (CPU or CUDA; head-only or
    `--train-encoder`) and `evaluate.py` (confusion matrices). The judge
    loads a local checkpoint via `LAYA_ENGLISH_PATH`.
  - **GPU:** training ran on the RTX 5090 with torch 2.14.0+cu130
    installed into the venv (`uv run --no-sync`). `uv.lock` still pins the
    CPU build.
  - Checkpoints are git-ignored (`laya-finetuning/checkpoints/`).
  - **Next:** phase 0 on a new branch; the judge plugs into phase 5.

- **2026-09-28 (late):** Tuned the Laya judge questions against a
  49-node labelled set. Zero-shot never reached 80% (best 67%, R3 +
  calibration; see `laya-finetuning/README.md`).
  - `judge.py` now uses the user's simple persona questions ("You are a
    lead, tasked with…").
  - Started fine-tuning: `laya-finetuning/data/train.jsonl` (140 nodes)
    and `test.jsonl` (49, held out). **Next:** write `finetune.py` +
    `evaluate.py` per the plan in `laya-finetuning/README.md`, train on
    CPU (head only), and report held-out accuracy.

- **2026-09-28 (later):** Implemented phase 8's standalone pieces ahead of
  order, at the user's request:
  - `laya` optional extra (`pyproject.toml`);
  - `src/JFI/planner/judge.py` (`LayaJudge`: english + typed-decisions,
    one `predict_batch` per judge step, both answers kept,
    `LAYA_MIN_CONFIDENCE` gate, `LAYA_JUDGE_MODEL`, fallback rule; lazy
    import so it works without Laya);
  - `src/JFI/llm/lmstudio_control.py` + `UNLOAD_LLM_BEFORE_LAYA` (D35);
  - `build_binary` excludes laya/torch/transformers (D22);
  - `utils/laya_poc.py`; env vars documented in `JFI_ENV_TEMPLATE`.

  **Tests:** `test_laya_judge.py`, `test_lmstudio_control.py`,
  `test_build_binary.py`. Whole suite = the 6 documented Windows-baseline
  failures only; ruff clean; `uv run build` OK (79 MB, torch excluded).
  Smoke-tested on real weights, in process and in the child process. The
  unload flag was **not** run against a live LM Studio (it would unload
  the user's models); the `lms` commands were checked with
  `--estimate-only` and `ps`. **Measured:** both checkpoints ≈ 3.5 GiB
  resident; `unload()` in process frees nothing, a child exit frees
  everything. **Watch:** `uv add`/`uv sync` drops unlisted extras. Use
  `uv sync --extra laya --extra anthropic --group dev` or the anthropic
  tests fail. **Next:** commit; phase 0.

- **2026-09-28:** Design session. Wrote `laya_plan.md` (design,
  decisions D1–D34, gap review G1–G23, database §13) and this phase plan.
  Added `docs/pipeline.md`, `docs/plan-tree.md` and `docs/phase-*.md`
  describing `v1`. Found and confirmed the `v1` re-plan bug (phase 0).
  **Open:** skipped-leaf rule (`laya_plan.md` §11); tree-sitter dependency
  (below). **Next:** user review, then phase 0.

## How this stays shippable: two pipelines side by side

Decision **G16a** makes an incremental build possible. Every session
records its pipeline in `SessionRecord.pipeline_version`:

- **`v1`:** today's pipeline, untouched. Every existing session, and
  every new session **until phase 10**, runs on it.
- **`v2`:** the new design, built up phase by phase behind
  `JFI_PIPELINE=v2` (an `.env` flag, off by default). You can try `v2` on
  a throwaway project at any point from phase 7 on.

Rules for every phase:

1. **`v1` behaviour doesn't change** (except phase 0's bug fixes, which
   are bug fixes). Its existing tests stay as they are and stay green.
2. **New `v2` code gets new tests,** in new files (`test/v2/…`), following
   `AGENTS.md`: real SQLite in a tmp dir, observed behaviour, no hand-copied
   reimplementations.
3. **Every phase runs** `uv run pytest` (whole suite; on Windows, the
   documented baseline), `uv run ruff check`, and `uv run build`.
4. **Docs are updated in the same PR** for anything the phase makes real.

```
0 groundwork ─► 1 DB ─► 2 runbook/design ─► 3 episode engine ─► 4 code tools
                                                   │                 │
                                                   └──────┬──────────┘
                                                          ▼
                              5 planner v2 (no Laya) ─► 6 Dev v2 ─► 7 reviewer + cleanup v2
                                                                         │  (first full v2 run)
                                                          ┌──────────────┼──────────────┐
                                                          ▼              ▼              ▼
                                                   8 Laya judge   9 document path   10 UI + reporting
                                                          └──────────────┴──────────────┘
                                                                         ▼
                                                             11 v2 becomes the default
                                                                         ▼
                                                             12 remove v1 (later release)
```

Phases 8, 9 and 10 are independent of each other once phase 7 is done.

---

## Phase 0: Groundwork (small)

Fixes that help both pipelines and de-risk the rest.

- **Re-plan bug** (`docs/phase-planner.md` → Known gaps): a second tiered
  planner run is a no-op. Fix it in `v1`, because `v1` stays in use until
  phase 10. Regression test: a second `run_phase("planner")` after PO
  feedback makes at least one LLM call.
- **Done early (2026-09-28):** `laya` added as the optional extra `laya`
  in `pyproject.toml` (`uv add --optional laya laya`), to measure RAM.
- **Real token counts (G14):** request `stream_options={"include_usage":
  True}` in `openai_compatable_stream.py`, and prefer server `usage` over
  chars/4 where the console already reads it. Phase 3's budget depends on
  this.
- **Markdown-era wording:** `review_outcome`,
  `product_owner_feedback_outcome`, `_stuck_task_directive` and the
  iteration message stop asking for "`- [ ]` items".
- **Exit:** suite green; the re-plan regression test passes.

## Phase 1: Database schema (medium)

All of §13, with no behaviour change.

- **Models:**
  - new `Leaf` / `SessionRecord` / `HistoryMessage` columns (§13.1);
  - new tables `Episode`, `PlannerVerdict`, `PlanEvent`, `RunbookEntry`,
    `DesignEntry`, `Directive` (§13.2).
- **Migration for existing DBs:**
  - `_ensure_columns` for SQLite;
  - **extended to MySQL/Postgres** (§13.4).
- **`pipeline_version`:** new sessions write `v1` (the flag isn't read yet).
- **`export-db`:** dumps the new tables.
- **Tests:** §13.5 (fresh DB, old-DB fixture upgrade, dialect DDL,
  uniqueness, `v1` session unaffected).
- **Exit:** an existing `.jfi/JFI.db` opens, gains the columns, and a `v1`
  session resumes exactly as before.

## Phase 2: Runbook and design tables (small)

§3.1, §3.2. Usable on their own.

- **Tools:** `runbook_set` / `runbook_get(name=None)`, `design_set` /
  `design_get(kind=None, key=None)`, each with a size-capped result.
- **The index renderer:** the one-line list of runbook names / design
  keys that briefs will carry.
- **Not wired into `v1` prompts** (keeps `v1` stable). `v2` roles use it
  from phase 5.
- **Tests:** upsert, uniqueness, the index format, size caps.

## Phase 3: The episode engine (large; the core runtime)

Everything that makes "one small, scoped LLM call per node" work. It's
built as a module `v2` code calls; nothing user-visible changes yet.

- **Package:** `src/JFI/episode/` (or `planner/episode.py` per §9).
- **Briefs** (G1.1, G1.2):
  - the **scope anchor**, built in code and never trimmed;
  - the role prompt;
  - the node;
  - the runbook/design index.
- **Per-role core tool sets** fixed in code (G1.2). `load_tool` is scoped
  to the optional pool and lasts one episode (G1.3, without Laya picks
  yet).
- **Budget** (§0, §5.3):
  - counts tokens per episode (server `usage`, else estimate);
  - stops at `EPISODE_TOKEN_BUDGET`, capped at `CONTEXT_SIZE ×
    CONTEXT_RATIO` (D20);
  - `TOOL_RESULT_MAX_TOKENS` caps on read tools.
- **End of episode** (G5):
  - the `finish` tool;
  - `MAX_EPISODE_TURNS`;
  - an `end_reason` on every episode;
  - the no-tool-call nudge reworded to "call `finish`".
- **Persistence:**
  - an `Episode` row per call;
  - history rows tagged with `episode_id`;
  - an episode's messages rebuilt from its own rows only.
- **Directives, skip, pause** (G7): `Directive` rows delivered to the
  node's next episode; pause and skip between episodes.
- **Per-role models** (G15): `ARCHITECT_*` / `LEAD_*` / `TASK_*` /
  `DEV_*` prefixes via `phase_env`, falling back to the phase, then the
  shared default.
- **Tests:**
  - the anchor is present after trimming;
  - overhead stays under `ROLE_OVERHEAD_MAX_TOKENS` for each role;
  - budget stop, turn cap and `load_tool` expiry;
  - episode isolation (no messages from another episode);
  - a directive reaches the right episode;
  - model prefixes resolve.
- **Exit:** a scripted fake LLM can run a tiny role episode end to end,
  under budget, with its rows in the DB.

## Phase 4: Code tools (medium)

The tools roles use on disk (§4.5, G3, G17, G18).

- **`scaffold_file`:** language-aware stub bodies (Python, TS/JS, Go, Rust,
  comment fallback); artifact skeletons with `JFI:` lines (G2); append-only;
  path safety (refuse outside the project, `.jfi/`, `.git/`).
- **`unscaffold_file`:** removes only pure JFI stubs.
- **`mark_change`:** `JFI-CHANGE:` / `JFI-DELETE:` above existing symbols
  (G3).
- **`read_symbol`, `replace_symbol`, `list_symbols`:**
  - Python via `ast`;
  - other languages via tree-sitter if the dependency is accepted, else
    the marker-bounded fallback (G17). **Decide the tree-sitter dependency
    at the start of this phase**, including how it interacts with
    `uv run build`.
- **`search_code`, `list_dir`:** size-capped.
- **The end-of-imp marker scan** (`JFI:`, `JFI-CHANGE:`, `JFI-DELETE:`).
- **Tests:** per language, round-trips leave the rest of the file
  byte-identical; refusals (non-stub delete, bad path, multi-line
  signature, unknown symbol).

## Phase 5: Planner v2, with the fallback judge only (large)

The whole §2 loop, **without Laya**. The judge is the deterministic
fallback rule (§5.2):

- `architect` / `lead` nodes → `BREAKDOWN`;
- `task` leaves → `GOOD`;
- the §5.3 token check can still force `BREAKDOWN`.

That's a complete, predictable planner: every goal goes Architect → Lead →
Task. Laya drops in later as a better judge behind the same interface.

- **`src/JFI/planner/`:**
  - `loop.py`: gated stages (D15), routing by `level`, broken-down nodes
    → `GOOD`, planning complete when every node is `GOOD` (D27);
  - `roles.py`: Architect create/redo/extend, Lead, Task prompts per §4.3–
    §4.6, including G2 (artifacts, the `project` component), G3
    (`mark_change` in Lead, `modify`/`delete` leaves in Task), G9;
  - `judge.py`: the judge interface + the fallback implementation.
- **Redo** (§4.7): the creator redoes only its own node; the redo cap;
  Lead fixes its own stubs.
- **Escalation** (§4.7): `escalate`, pausing the subtree, the cap,
  `PlanEvent` rows.
- **Operational REDO → runbook.** In this phase REDO only comes from the
  token check or Architect's own redo path, since the fallback judge never
  says REDO. It's fully exercised in phase 8.
- **`depends_on`** validated acyclic (G6), and `plan_status` resume from DB
  state.
- **Wiring:** `run_phase("planner")` dispatches on `pipeline_version`;
  `v1` is unchanged.
- **Tests:** §9A's planner items with the fallback judge, plus redo,
  escalation and resume at every stage.
- **Exit:** with `JFI_PIPELINE=v2` and a scripted LLM, a goal produces
  component → file (stubs on disk) → task leaves, all `GOOD`, and the
  planner phase completes from DB state.

## Phase 6: Dev v2 (large)

§6, G6, G8 (the Dev side), G11, G13.

- **Queue:** one fresh episode per `GOOD` leaf, in topological order over
  the whole tree (G6).
- **Leaf kinds:**
  - `implement`: stub → code + one unit test;
  - `modify`: change + existing tests + one new test;
  - `delete`: caller check first;
  - `fill`: artifact + mechanical check;
  - `passage`: stub for phase 9.
- **Completion gate:** `mark_leaf_done` is refused until the leaf's
  test/check passed in this episode. `status=done` is imp's done state
  (D27).
- **Recovery:**
  - a test failing on another symbol's `NotImplementedError` → re-queue,
    not "fix" (G6);
  - restart after a crash with a "partial attempt" brief, and
    `attempt_count` capped at 3 (G13);
  - overflow → a short Task split, then continue (§5.3, D19).
- **Test files** are scaffolded without failing test stubs; `test_one`
  selects one test (G11).
- **End of imp:** `setup` verified first; `build` + the marker scan at the
  end; `IMP_COMPLETE` written by the runner from DB state.
- **Tests:** ordering across files, each kind, the refusal gate, restart,
  overflow split, the marker scan.
- **Exit:** on a small real goal (e.g. a CLI todo app) with a real model,
  every leaf reaches `done` with its test passing.

## Phase 7: Reviewer and cleanup v2: the first full v2 pipeline (medium)

§7, G8, G12, G19.

- **Reviewer:** one scoped episode that runs the runbook `e2e` (often a
  single command like `uv run pytest` / `npm run test`, G12; a scripted
  scenario when needed; a library/CLI e2e per G19).
- **Per-leaf `review_status`:**
  - pass → every leaf `passed`;
  - fail → the owning leaves become `failed`, are re-opened for Dev with a
    `fix_note`, and Dev + the reviewer run again (G8);
  - `MAX_REVIEW_ITERATIONS` still caps it.
- **Extend mode:** only for genuinely missing work (queued requests,
  missing features), through Architect.
- **Cleanup:** one scoped episode; scaffolded files are deliverables.
- **`v2` `PHASES` = `planner, imp, reviewer, cleanup`**, no
  `product_owner` / `testing`. `v1`'s list is unchanged.
- **Tests:** pass, fail → re-open → pass, extend mode, phase completion
  from leaf state, resume in iteration 2 (the old marker bug can't happen
  in `v2`).
- **Exit:** **real runs.** `v2` on 2–3 real goals of different sizes,
  measured for the §11 "tuned during implementation" items: how many
  episodes overflow, how often Lead/Task escalate, whether the fallback
  judge's always-breakdown shape is too fine-grained. Findings adjust
  prompts, caps and budgets before phase 8.

## Phase 8: Laya judge (medium)

§5, D21, D22, G1.3, G10.

- **The `laya` extra** (`pyproject.toml`), imported lazily.
- **`laya_judge.py`:** one lazy `Router(max_loaded=2)` per session,
  preloading `english` + `typed-decisions` and kept loaded; one
  `predict_batch` per judge step; every request names its checkpoint (D21,
  measured ~3.5 GiB resident, 1.69 GB on disk). No multilingual.
- **Starting point:** `utils/laya_poc.py` already loads, predicts and
  unloads both checkpoints (in process and in a child process).
- **The questions and the confidence gate** (§5.1, §5.2), the opt-in LLM
  fallback, and `PlannerVerdict` rows.
- **Duplicate check** in the judge step (G10) and **tool picks** stored on
  the node (G1.3). (The per-goal checkpoint pick, G20, is superseded: no
  multilingual.)
- **Both checkpoints judge during tuning;** both answers are logged, and
  one is picked per question from the data (`laya_plan.md` §11).
- **Operational REDO** now actually fires; Architect moves the command to
  the runbook.
- **Binary:** exclude `laya` / `torch` / `transformers` in
  `build_binary` (D22).
- **Tests:** a fake `Router` (one load per session, one batch per judge step); low
  confidence and missing-Laya fallbacks; duplicates; tool picks only add;
  build args exclude the modules.
- **Exit:** the same real goals as phase 7, with Laya judging. Compare
  `PlannerVerdict` against the phase 7 baseline; set `LAYA_MIN_CONFIDENCE`
  from the data.

## Phase 9: Document path (medium)

G4.

- **Role variants** for `story`-type goals: an outline (`DesignEntry`
  `kind=outline`), section file nodes, passage leaves, and Markdown
  `JFI:` placeholders.
- **Dev `passage` kind** with mechanical checks (word count, required
  points, placeholder removed).
- **The reviewer** reads the whole document against the outline.
- **Tests:** a story goal plans and completes with no code tools used.

## Phase 10: UI and reporting (medium)

G21.

- **TUI:** status/stage (`Arc` / `Lead` / `Task` / `Judge` / `Dev` /
  `Review`), the current episode, per-phase leaf counts.
- **`web/dashboard.py`:** plan with the new fields; runbook and design
  views; reviewer per-leaf state. Replaces the plan-feedback view for `v2`.
- **`socket_reporter`:** the new stage names; `render_plan_markdown` keeps
  its `- [ ] N.M` format with the new fields as a suffix.
- **`Just-Finish-It-Fleet` (separate repo, matching PR):** drop the
  `product_owner` / `testing` columns for `v2` sessions (via
  `pipeline_version`), and add the new stage tags.
- **`export-db`:** the full `v2` view.
- **Tests:** status snapshot shape; `render_plan_markdown` still parses
  with the fleet's format.

## Phase 11: v2 becomes the default (small)

- New sessions write `pipeline_version=v2`; `JFI_PIPELINE=v1` opts back.
- **Remove from the `v2` path** anything left only for comparison;
  `PLANNER_SINGLE_PASS` is gone (D13).
- **Docs:** rewrite `docs/phase-planner.md`, `phase-imp.md` and
  `phase-reviewer.md`; add the `v2` pipeline to `pipeline.md` /
  `plan-tree.md`; mark `phase-product-owner.md` / `phase-testing.md` as
  `v1` only; update `AGENTS.md`, `README.md` and `JFI_ENV_TEMPLATE` (every
  new env var).
- **Exit:** a new session with no flags runs `v2`; an old session still
  resumes on `v1`.

## Phase 12: Remove v1 (later release)

Once no `v1` sessions are expected.

- **Delete** the tiered planner, the Program Manager loop/prompt/tools,
  the testing phase, session-wide `UnlockedTool` unlocking, marker-based
  resume, and the `v1` tests (G22).
- **Keep** the enum values and legacy columns, so old DBs still open for
  `export-db`.
- **Delete** `docs/phase-product-owner.md` and `docs/phase-testing.md`.

---

## Size and risk at a glance

| Phase | Size | Main risk | Mitigation |
|---|---|---|---|
| 0 | S | none | — |
| 1 | M | breaking old DBs, non-SQLite backends | old-DB fixture test; dialect DDL tests |
| 2 | S | none | — |
| 3 | L | the budget/anchor design doesn't hold up on real models | overhead caps tested per role; real-model check at phase 7 |
| 4 | M | non-Python symbol editing | decide tree-sitter up front; marker-bounded fallback |
| 5 | L | prompts produce bad structure | deterministic judge first; real runs in phase 7 |
| 6 | L | cross-file ordering, stuck leaves | topological queue + re-queue rule + attempt cap |
| 7 | M | e2e too weak/strong | real runs are the exit criterion |
| 8 | M | Laya near chance zero-shot | the fallback stays the safety net; tune from `PlannerVerdict` |
| 9 | M | prose checks too mechanical | keep them simple; the reviewer reads the whole document |
| 10 | M | fleet repo out of sync | matching PR in `Just-Finish-It-Fleet` |
| 11 | S | surprise for users | `JFI_PIPELINE=v1` escape hatch |
| 12 | M | removing code still in use | only after a release with `v2` as default |

## Open for this document

1. **Skipped leaves** (`laya_plan.md` §11) must be decided **by phase 6**,
   which is where `done` vs `skipped` gates imp completion.
2. ~~tree-sitter~~ **Decided in phase 4: no new dependency.** Python uses
   `ast`; JS/TS, Go and Rust use a brace matcher that skips strings and
   comments. Revisit only if it proves fragile on real code.
