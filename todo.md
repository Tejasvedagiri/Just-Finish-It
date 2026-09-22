# TODO — JFI

Status as of 2026-09-20, 10:24. This file now covers two things: the
**SQLite/Pydantic persistence rewrite** (new, major, in progress — see below)
and the **pre-existing maintenance backlog** (unrelated, rolled in unchanged
from before this branch existed — see "Existing backlog" at the bottom).

Working directly on branch `feature/dbs` in the main checkout (not an
isolated worktree, per request, so it's easy to review) — but the StockUI
session (`portfolio-live-loading-states`, JFI session dir
`/home/tejas/PycharmProjects/StockUI/JFI/portfolio-live-loading-states`) is
actively running right now against `dist/jfi`, which is gitignored/untracked
and therefore unaffected by branch switches on its own. The one real risk is
**running `uv run build` on this branch, which WOULD overwrite that same
`dist/jfi` file with an incompatible binary** (once this branch introduces
DB-backed sessions, that binary could no longer correctly resume a
plan.md-based session). Self-imposed guardrail: don't run `uv run build`
from this branch until asked, even once the DB rewrite works from source.

## Why (context from today's session)

`plan.md` (markdown checkbox tree) has two real, already-observed failure
modes: (1) a renumbering bug that needed a bolt-on repair tool
(`JFI.tool.plan_renumber`) because hand-editing dot-numbered strings via
`replace_in_file` can desync; (2) the dashboard frontend
(`frontend/src/main.js`'s `parsePlanLines`/`buildPlanTree`) has its own
independently-hand-rolled regex parser over the SAME markdown, with its own
documented bug (a trailing-period parent number once flattened the entire
tree into bogus top-level roots). Two parsers, no shared schema, both
fragile in different ways.

Decision from discussion: move the plan (and session metadata) to a real
database, modeled with Pydantic, instead of markdown text edited via
`replace_in_file`. **SQLModel** (Pydantic + SQLAlchemy in one class
definition, same author as Pydantic/FastAPI) is the natural library choice
here — one model definition serves as both the validation schema and the
ORM table, and swapping SQLite/MySQL/Postgres becomes a connection-string
change, matching "all mapped to pydantic models and env" from the ask.

## Scope

### 1. Pydantic/SQLModel models — `src/JFI/models/` — ✅ DONE (schema + standalone layer; NOT yet wired into the live agent loop, see Progress below)

Revised per later instruction: **everything under `JFI/<session>/` gets a
table** (not just the plan), except `.lock` (a process mutex, not data).
11 tables, final:

| File today | Table |
|---|---|
| `plan.md` | `Leaf` — parent_id self-FK, phase, integer `sort_key` (gap-numbered 10/20/30/… so an insert never needs to renumber siblings), status enum, started_at/ended_at/tokens (folds in what `task_history` tracked separately). Display numbers ("1.1.2") are computed by `build_indexes`/`display_number`, never stored. |
| `metadata.json`: `unlocked_tools` | `UnlockedTool` — unique(session_id, tool_name) |
| `metadata.json`: `implemented_files` | `ImplementedFile` — unique(session_id, file_path) |
| `metadata.json`: `digest_summary`/`digest_block_count` | fields on `SessionRecord` |
| `context.json` | `ContextEntry` — unique(session_id, key) |
| `history.jsonl.gz` | `HistoryMessage` — role/content/tool_calls(JSON)/tool_call_id, ordered by an explicit per-session `seq` |
| `run.log`'s SYSTEM/RULE lines | `LogEvent` — same `seq` space as `HistoryMessage` so export_db can merge-sort the two back into one interleaved view |
| status snapshot: `queued_items` | `QueuedItem` |
| status snapshot: `background_processes` | `BackgroundProcess` — `elapsed` computed at read time, never stored |
| status snapshot: `done_phases` | `DonePhase` — unique(session_id, phase) |
| status snapshot: `awaiting` | `awaiting_prompt`/`awaiting_options` fields on `SessionRecord` (singular/current, no history worth a table) |
| `web_status.json` | fully derived from the above — not stored anywhere |
| `SessionRecord` (the rest) | repo path, goal text, task_type, phase/stage/state, iteration, queue_size, is_paused, current_task(_started_at), tokens_*, digest_* |

Every list-shaped thing that could have been a JSON blob got a real table
instead (per explicit instruction) — each is independently queryable now
and gets real uniqueness constraints from the schema rather than hand-
rolled de-duplication in Python.

### 2. DB layer
- Backend selected via env: `DB_BACKEND=sqlite|mysql|postgres` (default
  `sqlite`), `DATABASE_URL` when not sqlite. SQLite default path:
  `JFI/<session>/session.db` (mirrors today's per-session directory).
- Replace `SimpleSessionManager`'s plan.md read/write paths with DB reads/
  writes through the SQLModel layer. The model-facing tool surface changes
  from free-form `replace_in_file` text surgery to a small fixed API:
  `add_leaf(parent_id, phase, desc)`, `mark_leaf_done(id)`,
  `split_leaf(id, into=[...])`, `get_plan()` — wire these through
  `runner.py`'s `TOOL_MAP` and the deferred-tools schema list
  (`src/JFI/tool/schemas.py`) the same way `write_file`/`read_file`/etc.
  are wired today.
- The four-pass planner (Architect → Team Lead → Journeyman → Function
  Breakdown, `simple_session_manager.py`'s `get_phase_trigger`) currently
  writes plan.md via `write_file`/`append_to_file`/`replace_in_file`;
  every one of those call sites needs to move to the new leaf-tool API.
  This includes the Journeyman-pass "iterate-until-clean" splitting rule
  added earlier today — same rule, new mechanism for actually applying it.

### 3. `uv run export-db` CLI command
- New `[project.scripts]` entry in `pyproject.toml`, same pattern as the
  existing `build = "build_binary:main"`. Implementation under
  `src/export_db/`.
- Takes a session path (or scans all `JFI/*/session.db` under the given
  working directory), renders the DB's plan tree + session metadata back
  into a markdown file shaped like today's `plan.md` — **read-only, for
  human debugging only**. Never parsed back in by JFI itself — the whole
  point of this change is that markdown stops being a source of truth, so
  this export must not become a second one.

### 4. UI/dashboard support (`frontend/`, `server/master.js`)
- `frontend/src/main.js`'s `parsePlanLines`/`buildPlanTree`/`subtreeCounts`
  (the regex tree-parser with the documented trailing-period bug) get
  deleted once the dashboard receives structured leaf objects instead of
  a `plan_markdown` string — this is the concrete win the rewrite buys the
  UI side.
- **Open decision, leaning but not yet confirmed** (see below): how
  `master.js` gets "all info from the db" when a CLI session connects.

### 5. `uv run export-db`'s relationship to `dist/jfi`
- New CLI scripts don't need PyInstaller bundling to be usable during dev
  (`uv run export-db` works straight from source). Do NOT run `uv run
  build` on this branch (see guardrail note above) until explicitly asked.

## Open decisions (flagging rather than silently picking)

1. **How does `master.js` reach the DB?** Two real options, both consistent
   with "via these databases or from the websocket":
   - (a) **Websocket RPC** (leaning this as the default): extend the
     existing `/report` protocol with a request/response message
     (`{"type":"get_full_plan"}` → session replies with the full leaf
     tree). Keeps master.js's own documented design intact — it explicitly
     never assumes filesystem access to a session's machine (see
     `master.js`'s module docstring: sessions can legitimately run on a
     different host than the master). Works identically whether the DB
     backend is SQLite, MySQL, or Postgres, and whether master is local or
     remote.
   - (b) **Direct DB connection from master.js** (Node `better-sqlite3` /
     `pg` / a MySQL client) when session and master share a machine/DB
     server. Faster, no round-trip through the session process, but only
     works for a local SQLite file if master and session are ON THE SAME
     FILESYSTEM — breaks the "master runs on a different machine" case
     entirely for SQLite (though it's fine for a networked Postgres/MySQL
     server reachable from both).
   - Proceeding with (a) as the required baseline unless told otherwise;
     (b) stays a possible same-host fast path for later.
2. Does `ActivityEvent` derivation move server-side into the DB layer too,
   or stay exactly where it is today (`session-registry.js`'s
   `deriveEvents`, computed from consecutive status snapshots)? Leaning
   "leave it where it is" for v1 — it already works and isn't part of the
   plan.md pain points.
3. **Migration** of already-existing plan.md-based sessions: out of scope
   for v1 (new format applies to sessions created after this ships); a
   one-time `plan.md` → DB import script is a reasonable follow-up, not a
   blocker.

## Progress so far

- [x] `src/JFI/models/` — all 11 tables, `_util.utcnow()` (naive UTC —
  SQLite round-trips datetimes as naive, so this stays consistent instead
  of mixing aware/naive and raising `TypeError` on subtraction, which the
  first pass at `BackgroundProcess`/`export_db` actually hit and had to be
  fixed), `db.py`'s `get_engine`/`get_session`/`database_url`
  (`DB_BACKEND=sqlite|mysql|postgres` + `DATABASE_URL`).
- [x] `uv run export-db` — writes `plan_export.md` (session summary + plan
  tree + every non-conversation table) and `log_export.txt`
  (`HistoryMessage` + `LogEvent` merge-sorted by `seq`, reconstructing a
  run.log-shaped view) per session. Verified end-to-end against a fully
  seeded session covering all 11 tables.
- [x] `test/test_models.py` — 21 tests: DB_BACKEND resolution, round-trips
  through a SECOND engine re-opening the same file (a real disk-
  persistence check), per-phase display numbering (including gap-sort_key
  ordering and Implementation/Testing numbering independently), the three
  uniqueness constraints, and the HistoryMessage/LogEvent shared-`seq`
  merge. Full suite: 761 passed (740 pre-existing + 21 new), ruff clean.
- [x] `test/test_db_integration.py` — a dummy-session end-to-end test (no
  LLM, no JFI session, no tmux): seeds one row into every table by hand
  the way a real mid-imp session would, closes the engine, reopens a
  fresh one against the same file, and asserts every table's data came
  back correctly, including a check that `session.db` genuinely exists on
  disk. Also drives `export_db._export_one` directly against the same
  dummy data to confirm the CLI path (not just the ORM layer) works. 3
  tests, all passing.
- [x] `src/JFI/tool/plan_db_tools.py` — `get_plan`/`add_leaf`/`start_leaf`/
  `mark_leaf_done`/`split_leaf`, the tool functions that replace free-form
  `write_file`/`append_to_file`/`replace_in_file` text surgery on plan.md.
  `make_plan_db_tools(engine, session_id)` returns the bound-callable dict
  `runner.py` will wire into `TOOL_MAP`, same pattern as
  `make_context_tools`/`make_load_tool`. `test/test_plan_db_tools.py` — 18
  tests, no LLM, covering every happy path and every rejection (unknown
  phase, missing parent/leaf id, parenting under an already-real leaf,
  marking a parent done, splitting into fewer than 2 children). Verified
  by hand first via a manual smoke script exercising the exact same tree-
  build/split/complete sequence a planner+imp phase would produce.
- Full suite: **782 passed** (764 + 18 new), ruff clean (only 2
  pre-existing, unrelated `E741` hits in `benchmark/tasks/story/` remain).
- [x] **Wired into the live agent loop** (mechanically — prompts not yet
  rewritten, see below):
  - `src/JFI/tool/schemas.py`'s `DEFERRED_TOOLS` now has all 5 schemas
    (`get_plan`/`add_leaf`/`start_leaf`/`mark_leaf_done`/`split_leaf`),
    with one-line summaries in `_DEFERRED_TOOL_SUMMARIES` (the module's
    own consistency assertion catches a mismatch immediately — it did,
    the first time, before the summaries were added).
  - `SimpleSessionManager.__init__` now creates `self.db_engine` via
    `JFI.models.get_engine` alongside `plan.md` (additive, not yet a
    replacement — nothing writes to it from the live prompts yet) and
    exposes `plan_db_tools()`.
  - `runner.py`'s `TOOL_MAP` has "not available yet" placeholders for all
    5 (matching `load_tool`'s own pattern), rebound to the real session's
    engine in `_run_session` via `ssm.plan_db_tools()`, same spot
    `execute_command`/`load_tool` get rebound.
  - `test/test_plan_db_tools_wiring.py` — 2 tests, same
    fake-LLM-raises-immediately pattern `test_cmd_gate_session_e2e.py`
    already established for testing `runner.py` wiring with **zero real
    LLM calls**: `run_pipeline` runs just far enough to perform the
    `TOOL_MAP` rebind before the fake LLM raises, then the test calls the
    now-real tools through `TOOL_MAP` itself (not the underlying
    functions directly) and confirms a full add → start → mark-done round
    trip actually persists.
  - This test caught a real bug on first run: `_render_plan`'s (and
    `export_db`'s matching) root-level rendering unconditionally treated
    every root leaf as a parent (no checkbox), even one with zero
    children — fixed in both places by making the root loop call the same
    depth-aware `render_subtree` instead of a separate, buggy loop.
  - Full suite: **784 passed**, ruff clean (same 2 pre-existing,
    unrelated `benchmark/tasks/story/` hits).
- [x] **Prompt rewrite — all phases now describe the DB tools, not
  plan.md editing:**
  - Added a 6th tool, `reorder_leaf(leaf_id, after_leaf_id=0)` — a real
    gap was found mid-rewrite: the Journeyman pass's old ordering-bug fix
    (`python -m JFI.tool.plan_renumber`) had no DB-tool equivalent.
    Rebalances every sibling to a fresh 10/20/30/... sort_key sequence in
    the new order. Full schema + `TOOL_MAP` wiring + 6 new tests, same as
    the other 5.
  - Rewrote `PLAN_FORMAT_RULES` (simple_session_manager.py) AND
    `CORE_PLAN_RULES` (task_rules.py — this one was a near-miss: it's
    what `AdaptiveSessionManager` actually uses, i.e. what StockUI's own
    session runs on, and it still described the old markdown format after
    the first rewrite pass only touched `PLAN_FORMAT_RULES`. Caught before
    any live run — would have handed a real session self-contradictory
    instructions: some prompts saying "call add_leaf", the plan-format
    rules still saying "write '## Implementation' headers").
  - Rewrote all 4 planner stages (Architect/Team Lead/Journeyman/Function
    Breakdown), the single-pass fallback planner, Product Owner, Imp,
    Testing, Reviewer, Cleanup, and `get_phase_trigger`'s per-phase
    kickoff messages — every `write_file`/`read_file`/`replace_in_file`/
    `append_to_file` reference to the plan itself replaced with
    `get_plan`/`add_leaf`/`split_leaf`/`start_leaf`/`mark_leaf_done`/
    `reorder_leaf`. Architect's old "## Context and Prerequisites" section
    is now `context_save` (already existed for exactly this purpose).
    Cleanup's bookkeeping-folder file list now mentions `session.db`.
  - `_phase_system_message`'s "Your work queue" embed (what actually
    hands imp/testing their next leaf each turn) now branches on
    `has_leaves()` too — DB sessions get `[id=N]`-tagged lines naming
    `start_leaf`/`mark_leaf_done`; plan.md-only sessions get the original
    wording, completely unchanged.
  - Updated 5 existing test files whose assertions checked now-obsolete
    literal phrasing (`test_tiered_planner.py`, `test_phase_messages.py`,
    `test_plan_location.py`, `test_review_outcome.py`, plus the trivial
    `reorder_leaf` additions to `test_plan_db_tools.py`) — every change
    verified against the NEW wording's actual intent, never just relaxed
    to make a failure go away.
  - Full suite: **801 passed**, ruff clean (same 2 pre-existing,
    unrelated `benchmark/tasks/story/` hits).
- **Still open**: none of `context.json`/`history.jsonl.gz`/`run.log`/
  `metadata.json` are written to the DB by the live loop — only
  `Leaf`/`SessionRecord` are actually exercised by real prompts; the other
  9 tables exist and are tested standalone but nothing writes to them yet.
  UI/dashboard side (open decision #1) also still unresolved. Scoped out
  of "done" for now — the plan-tree migration (the part with the two real,
  already-observed bugs motivating this whole rewrite) is what needed live
  validation first.
- Guardrail lifted by request: about to rebuild `dist/jfi` and use it for
  a real validation session (see below) — StockUI's *currently running*
  session is being stopped first, specifically so this is safe.

## Live validation (in progress)

Per explicit request: once the migration above was solid, stop watching
the old StockUI session's progress, rebuild `dist/jfi` from this branch,
and launch a NEW JFI session against StockUI to finish the remaining
portfolio-dashboard work — using this as the real end-to-end test the
Testing Standard section below calls for (a dummy/unit test can't validate
prompt correctness; only a real model actually calling these tools can).

**Findings so far, running session `portfolio-complete`:**
- Architect/Team Lead/Journeyman stages all completed cleanly using the
  real tool calls (`add_leaf`/`split_leaf`/`get_plan`) — the DB-tool prompt
  rewrite works end-to-end against a real model, not just in isolated tests.
  Per-phase numbering confirmed correct live (`imp` and `testing` each
  independently start at 1, matching plan.md's old convention).
- **Real failure found, live-patched, then made permanent**: the model
  tried to reason through every remaining leaf's split decision AND the
  full execution order in one giant reasoning block before calling any
  tool, repeatedly blowing `REASONING_OUTPUT_CAP` (3000 tokens) and
  several times hitting the model server's own "Context size has been
  exceeded". Recurred at both Journeyman and Function Breakdown, across
  TWO separate session restarts (a forced `!` directive fixed it live each
  time but didn't survive a restart's context reset). After 5+
  occurrences it was clearly systematic, not transient — folded a
  permanent "work ONE item per turn" rule directly into both stages'
  prompt text (`simple_session_manager.py`), naming the exact observed
  failure the same way the existing Journeyman failure-examples already
  do. New test (`test_journeyman_and_function_breakdown_enforce_one_leaf_per_turn`,
  `test_tiered_planner.py`) locks in the wording. Full suite: **812
  passed**. Rebuilt `dist/jfi` with the fix; the *already-running* session
  was left alone rather than restarted a third time just for this, since
  the risk is confined to two planner stages it was already mid-way
  through — the fix protects any future re-plan of this session and every
  new session going forward.
- **Real gap found and fixed in the UI/dashboard side (previously
  "unresolved" open decision #1) — TWO separate dashboards, both broken,
  both fixed**:
  - `src/JFI/web/dashboard.py` (the per-session Streamlit dashboard,
    `jfi-web`/port 7778) hardcoded `st.info("No plan.md yet for this
    session.")` whenever `plan.md` didn't exist, with no DB-aware
    fallback. Fixed the same has_leaves()-gated way as every other
    plan.md reader in this migration, and — after the user flagged the
    raw `[id=N]`-tagged dump as hard to read — rendered through the new
    `render_plan_markdown()` (below) instead, so it reads as clean
    nested markdown checkboxes, no leaf-id clutter (that's for the
    model's own tool calls, never a human-facing view).
  - **`frontend/src/main.js`'s fleet-dashboard checklist** (open decision
    #1's actual resolution): rather than teach the JS parser a new wire
    format, added `render_plan_markdown(engine, session_id)` to
    `plan_db_tools.py` — renders the DB tree in plan.md's OLD bullet
    syntax byte-for-byte (`## Implementation`/`## Testing` headers,
    `- [ ]`/`- [x]` bullets, real dot-numbers, no `[id=N]` tags) — and
    wired it into `runner._read_plan_markdown` (has_leaves()-gated, same
    pattern as everywhere else). Result: **zero frontend changes** —
    `parsePlanLines`/`buildPlanTree` keep working completely unchanged
    against a DB-backed session's checklist. Verified against the live
    master.js `/view` websocket that `plan_markdown` now carries real,
    correctly-formatted data for the running session. New tests:
    `TestRenderPlanMarkdown` (7 cases, `test_plan_db_tools.py`) and
    `test_read_plan_markdown.py` (3 cases, through a real
    `SimpleSessionManager`).
  - Both gaps were caught only because the user checked the actual
    running dashboards, not by anything in the automated test suite — a
    reminder that a high pass count alone doesn't prove the UI side is
    covered.
  - Also found `frontend/dist/` (the Node fleet dashboard's build output)
    had gone missing entirely at some point this session, unrelated to
    this migration's own code — `master.js` was serving its bare
    fallback text instead of the real UI. Rebuilt; unrelated to the DB
    migration itself.
- **Real UI bugs found and fixed in `frontend/src/main.js`/`style.css`,
  from a live screenshot of the actual checklist**:
  - The plan-checklist drill-down concatenated each depth's own leaves in
    visit order while walking down the tree (a shallower level's leaf
    siblings, THEN the selected branch's own children tacked onto the
    end) instead of ever sorting the combined result — produced exactly
    the non-numeric order visible in the screenshot ("1.1, 1.3, 1.2.1,
    1.2.2"). Fixed by collecting leaf NODES across every depth visited
    and sorting once at the end instead of concatenating rendered HTML
    as it goes.
  - No visual indication of which leaf is actively being worked on
    beyond a border-color change. Added a pulsing box-shadow glow on the
    current leaf's card and a spinning animation on its "↻" status icon
    (with a `prefers-reduced-motion` fallback).
  - Rebuilt `frontend/dist/`; verified via the sort logic directly (Node
    one-liner reproducing the exact screenshot scenario → correct order)
    since no browser tool is available this session.
Double-checking closely as it runs, per request — not trusting a
"PIPELINE COMPLETE" claim blindly (see the run-jfi skill's own guidance).

## Full cutover: history/context/metadata/log, and one DB per project

Per explicit follow-up request, expanded scope beyond the plan alone:
`history.jsonl.gz`, `context.json`, `metadata.json`, and `run.log` all moved
into the DB too, with **full cutover** (chosen over "DB-primary, files still
written" when explicitly offered both) — none of the four are written to
disk anymore, at all, by a session running this code.

- **`context.json` → `ContextEntry`**: `context_tools.py` rewritten
  end-to-end. `context_save`/`context_lookup` are now ordinary,
  model-decided tool calls (`get_context_value`/`set_context_value` +
  `context_save`/`context_lookup`/`make_context_tools(engine, session_id)`)
  — **pulled, never pushed**. Per explicit design mandate: "LLM call ->
  Tools (context) -> pass context info -> the action", no exceptions. The
  old "SAVED CONTEXT (auto-loaded...)" block that force-fed every saved
  fact into every phase's system message every turn — whether relevant to
  that turn or not — is gone entirely, along with
  `render_facts_for_auto_load` and the `context_cache_path`/
  `context_cache_file`/`DEFAULT_CONTEXT_CACHE_PATH` plumbing that supported
  it (removed from `simple_session_manager.py`, `runner.py`,
  `abstract_session_manager.py`'s own interface docstring). `cmd_tools.py`'s
  approved-command "Save" prefixes and `process_tools.py`'s background-
  process mirroring were both moved onto the same `ContextEntry` store
  (`APPROVED_CMD_KEY` row, `bg_process:<handle>` rows) rather than getting
  their own separate mechanism.
- **`history.jsonl.gz` → `HistoryMessage`**: new `history_store.py`
  (`has_history`/`load_history_from_db`/`append_history_to_db`).
  `SimpleSessionManager.load_history`/`save_history` rewritten to read/
  append through it, preserving the append-only O(new-messages) cost
  property the old gzip-append format already had (never O(total history)
  per save). A pre-existing `history.pkl` (oldest format) or
  `history.jsonl.gz` (previous format) from before this cutover both still
  migrate in once, automatically, on the first resume of a legacy session
  — after that, everything lives in the DB and the old files are never
  touched again. **Real bug caught here**: `HistoryMessage.content` was
  typed `Optional[str]`, but a multimodal user message (an attached image)
  carries `content` as a *list* of parts
  (`[{"type": "text", ...}, {"type": "image_url", ...}]`, see
  `runner.py`'s own construction sites) — SQLite's driver rejected binding
  a `list` outright. Fixed by storing `content` as a JSON column
  (accepts either shape) instead of a plain string column; caught by
  `test_context_helpers.py::test_compress_history_handles_image_messages_without_corruption`
  once the full suite was run after this phase's changes, not by a new
  test — the existing regression test for this exact scenario had gone
  uncaught until collection was unblocked.
- **`metadata.json` → `SessionRecord` + `UnlockedTool`/`ImplementedFile`/
  `QueuedItem`**: new `metadata_store.py`
  (`load_metadata_from_db`/`save_metadata_to_db`). `unlocked_tools`/
  `implemented_files` are insert-if-missing (append-only, matches the old
  JSON list's own semantics); `queued_requests` is fully replaced each save
  (can shrink/reorder). `digest_summary`/`digest_block_count` live directly
  on `SessionRecord`.
- **`run.log` → `LogEvent`**: `PromptToolkitConsoleManager._log()` (the
  single choke point for every line the old run.log ever got — tags
  SYSTEM/RULE/REASONING/ASSISTANT/USER/TOOL_CALL/TOOL_RESULT/ERROR) now
  writes every tag to a `LogEvent` row, not just SYSTEM/RULE as originally
  scoped — REASONING and ERROR have no other persistence path at all (not
  part of `HistoryMessage`), so narrowing this to a subset of tags would
  have silently dropped them. `start_session_log(path)` →
  `start_session_db_log(engine, session_id)` (renamed in
  `abstract_manager.py`'s interface too).
- **`export_db` rewritten for the single-DB-per-project shape**: no longer
  takes a session directory — resolves a project root (or an explicit
  `.JFI.db`/directory path), lists every `session_id` found via
  `SessionRecord`, and exports each to its own `JFI/<session>/{plan_export.md,
  log_export.txt}` (same on-disk layout as before, just generated instead of
  live). `log_export.txt` now reads `LogEvent` alone in `seq` order — it
  already carries everything `HistoryMessage` used to need merging in for
  (assistant/user text, tool calls/results), so the old merge-by-`seq`
  logic between the two tables was deleted, not just updated. New
  `--session=<name>` flag filters to one session when a project has more
  than one.
- **One `.JFI.db` per PROJECT, not one `session.db` per session** — the
  final, most recent architecture pivot. `database_url`/`get_engine`
  (`JFI.models.db`) now take a **project root**, not a session directory,
  and resolve to `<project_root>/.JFI.db`. Every table was already keyed by
  `session_id`, so nothing about the schema changed — only where the file
  lives, and that it's now shared. `SimpleSessionManager.__init__` builds
  `self.db_engine = get_engine(self._project_root)` before
  `load_history()`/`load_metadata()`, since both now read from it.
  `export_db` lists every `session_id` in the shared file rather than
  assuming one session per DB. Added `.JFI.db` to StockUI's own
  `.gitignore` (the actual project this will run against) per explicit
  request — it is generated, per-machine state, same tier as `node_modules`
  or `dist`.
- **Test suite audit after the full-cutover implementation** (this was the
  first `pytest` run since the phase began — it hit a collection-blocking
  `ImportError` immediately, since `test_digest_summarization.py` still
  imported the just-deleted `render_facts_for_auto_load`): 44 failures
  total once collection was unblocked, all from tests pinned to the old
  file-based signatures/attributes (`context_cache_path`,
  `context_cache_file`, `make_context_tools`/`make_gated_execute_command`/
  `CmdApprovalGate`/`make_process_tools`'s old `cache_path` params,
  `get_approved_cmd_prefixes`/`save_approved_cmd_prefix`'s old file-path
  signature, `start_session_log`, `session.db`-per-session-dir path
  assertions, `metadata_path`/`history_path` file-content assertions,
  render_facts_for_auto_load's own dedicated test class). Fixed by
  rewriting each to the new engine/session_id-based API rather than papering
  over the assertions — one whole file (`test_cmd_context_cache.py`) was
  deleted outright since its entire subject (a file-path-based prefix
  cache) no longer exists and its coverage is now fully subsumed by
  `test_cmd_approval_gate.py`/`test_cmd_prefix_matching.py`'s rewritten
  engine-based versions. One test
  (`test_history_survives_a_truncated_final_write`) was deleted rather than
  rewritten: it existed specifically to pin gzip-partial-write recovery
  behavior, a failure mode that no longer exists once history writes go
  through real SQL commits instead of byte-appending a gzip stream. Added
  one new test (`test_models.py::test_one_db_file_is_shared_across_every_session_in_a_project`)
  explicitly covering the new single-DB-per-project requirement, since
  nothing in the existing suite exercised two different `session_id`s
  sharing one engine/file before. Full suite: **798 passed**. Rebuilt
  `dist/jfi`; smoke-tested the binary directly (launched against an empty
  scratch directory) to confirm the PyInstaller bundle still imports
  cleanly post-rewrite — it reached the normal "missing .env" failure
  point rather than any packaging-related import error.

## Fourth bug: "Session DB" tab's own dropdown reintroduced the theme-<select> failure

Caught immediately after shipping the "Session DB" tab: "same issues as the
theme dropdown box. I am not able to click it, refreshes and the box
closes." `renderSessionDbTab()` did a full `elTabContent.innerHTML`
replace on every `render()` call -- which fires roughly once a second per
actively-reporting session (`session_update` messages) -- destroying and
recreating the `<select id="db-table-select">` node every tick, closing
any open native dropdown out from under a click exactly like the ALREADY-
DOCUMENTED theme-`<select>` failure this same file's own `buildShell`
comment describes ("replacing a `<select>`'s own DOM node closes its
dropdown out from under whoever just clicked it (observed in practice)").
I introduced a fresh instance of a bug this codebase had already solved
once, by not reusing the existing fix pattern.

Fixed by mirroring `sessionShellFor`'s own established pattern exactly:
new `dbShellFor` (keyed on `selectedKey`) makes the shell -- the
`<select>`/checkbox/button controls -- build ONCE per session and never
get touched again by an ordinary re-render; only `#db-rows` (the actual
query results) repaints, via a new `paintDbRows()` called directly from
the `db_result` WebSocket handler, never through the generic shell-
rebuilding path. `dbShellFor` resets symmetrically whenever the user
navigates away from the tab (mirroring `sessionShellFor`'s own reset on
tab-away), so returning to it rebuilds fresh -- reusing the still-
preserved `dbTable`/dbRows` module state, so the user's last selection and
results reappear immediately without needing another click. No Python
code touched; frontend rebuilt (`npm run build`) and confirmed served by
the already-running `master.js` with no restart needed (it reads dist/
files fresh off disk per request).

## Three real bugs found live-dogfooding against StockUI's portfolio-api-wiring session

Found and fixed during close supervision of the actual live session (per
the standing "double check... make sure everything is working fine"
instruction) rather than from a code review:

- **Planning-phase bloat**: the live plan for a real bug-investigation
  branch ("SectorPie rendering bug") had spiraled into 30+ leaves nested 7
  levels deep, including near-duplicate leaves like "record the finding"
  and "close the leaf" as separate checkboxes for one action. Reviewed
  `simple_session_manager.py`'s Journeyman/imp prompts and
  `plan_db_tools.py`; per explicit request, implemented all three
  discussed fixes together rather than picking one:
  1. **Prompt-level** (Journeyman): a named FIFTH failure mode exempting
     diagnostic/investigative leaves ("investigate", "diagnose", "audit",
     "figure out", "root cause", "reproduce", "determine why", "debug",
     "explore") from the normal "one tool call" atomicity test — split at
     most once, then stop; the actual diagnostic work happens via ordinary
     tool calls + context_save, never more add_leaf/split_leaf calls.
  2. **Structural guardrail** (`plan_db_tools.py`): `add_leaf`/`split_leaf`
     now mechanically reject going past `MAX_LEAF_DEPTH=5` (general) or
     `MAX_INVESTIGATION_DEPTH=2` (investigation-keyword branches,
     detected via ancestor walk so a child inherits its parent's stricter
     cap even without repeating the keyword) — enforced independent of
     whether the model reads/follows the prompt.
  3. **imp-phase guidance**: explicitly forbids using `add_leaf` as a
     step-by-step lab notebook for its own investigation sub-steps —
     `context_save` findings instead; `add_leaf` is for genuinely new
     deliverable work discovered, never the next diagnostic question.
  New tests: `TestDepthGuardrails` (8 cases, `test_plan_db_tools.py`),
  4 new cases in `test_tiered_planner.py`.
- **Fleet dashboard plan-checklist drill-down bug** (caught from a live
  screenshot: "why is 4 in 3", "why is 3.2 not found but found under
  3.3"): `frontend/src/main.js`'s `renderPlanChecklist` collected leaf
  SIBLINGS from EVERY level walked through into one merged, number-sorted
  list — so a root-level leaf ("4", sibling of section "3" itself) and a
  level-2 leaf sibling ("3.2", sibling of "3.3") both leaked into what was
  displayed as "3.3"'s own children, with zero indication they belonged
  at completely different levels. Root cause: the walk always
  auto-descends into a default branch, so a leaf sibling of a branch had
  no other way to ever surface in the UI at all — collecting it into the
  wrong level's list was a workaround for that, not a real fix. Fixed by
  making a leaf sibling a SELECTABLE TAB at its own level (new
  `renderLevelTabRow`, reusing the exact same `data-pc-depth`/
  `data-pc-value` the click handler already generically handles for
  branches) and only ever rendering the level the walk actually stops at
  (a true dead end, or an explicitly-selected leaf tab) as full cards —
  verified with a standalone Node simulation of the exact tree shape from
  the screenshot before and after.
- **`browse_webpage`'s `eval_js` could hang forever, silently ignoring its
  own `timeout` parameter** — caught live: the StockUI session's own
  verification call sat for ~23 minutes with an actual headless Chrome
  renderer process still burning CPU (confirmed via `pstree`/`ps`) before
  intervention, well past its stated `timeout=140`. Root cause:
  Playwright's sync API applies NO timeout of its own to `Page.evaluate()`
  (unlike `goto`/`click`/`wait_for_selector`, which all genuinely honor
  `timeout_ms`) — a page-side Promise whose `resolve()` is never reached
  on some code path blocks the call, the whole tool call, and therefore
  the entire JFI turn, forever; recovered only by killing the OS-level
  Chrome process by hand (`browser.close()` never even got a chance to
  run, since the code was still stuck inside the same `try` block).
  Fixed in `browser_tools.py`: `page.evaluate()` now runs on a worker
  thread with a hard wall-clock `.result(timeout=...)`, and on timeout the
  browser is force-closed (the only thing that actually unblocks a hung
  `evaluate()` server-side) instead of leaving it running. New tests:
  `test_hung_eval_js_is_recovered_via_timeout_not_left_hanging_forever`
  (proves it returns around the timeout, not the full simulated hang) and
  `test_normal_eval_js_unaffected_by_the_timeout_wrapper`.
- Full suite green after each fix (814 passed), `dist/jfi` and the
  frontend both rebuilt.

## Fleet dashboard (port 9988): "Session DB" tab

Per explicit follow-up request ("I want to see db in 9988 fleet to. A new
tab called session-db"), extended the same DB-browsing capability to the
Node fleet dashboard -- non-trivial because master.js is DELIBERATELY
designed to run on a different machine than the sessions reporting to it,
with no shared filesystem (see socket_reporter.py's own module docstring),
so it can't just open `.jfi/JFI.db` directly the way the Streamlit
dashboard can.

- **New shared module `src/JFI/tool/db_browse.py`**: the table registry
  (all 12 SQLModel tables) and `query_table(engine, table_name,
  session_id=None)` (JSON-safe rows, `ValueError` on an unknown table
  name -- the only thing standing between a remote request and raw SQL).
  `src/JFI/web/dashboard.py`'s own Database tab now delegates to this too
  instead of duplicating the registry.
- **The actual query runs on the SESSION side, not the master.** New
  `"db_query"` control action (`src/JFI/manager/socket_reporter.py`):
  `_dispatch_control` is now async and can send a reply, `_handle_db_query`
  calls `query_table` locally (the session already has its own
  `db_engine`, now passed into `SocketReporter.__init__`) and sends the
  JSON result back as a `"db_result"` message. master.js only ever relays
  bytes both directions (viewer -> session for the query, session ->
  viewer for the result) -- it still never touches a filesystem path
  itself, preserving the whole point of the remote-safe design.
- **Frontend**: new "Session DB" tab (`frontend/src/main.js` state:
  `dbTable`/`dbScoped`/`dbRows`/`dbError`/`dbPendingRequestId`) -- a table
  picker, a "only this session's rows" checkbox, and a Refresh button that
  sends `{action: "db_query", table, scoped, request_id}` via the existing
  `sendControl` helper; a `request_id` guards against a stale response
  landing after the user already switched tables. `DB_TABLES` on this side
  is a plain picklist (must match `db_browse.table_registry()`'s keys) --
  the actual query logic never runs in the browser or in master.js.
- Full Python suite re-verified green (802 passed) after each step,
  `dist/jfi` and the frontend (`npm run build`) both rebuilt. Verified the
  whole round trip for real: master.js restarted on the new code, then a
  throwaway `SocketReporter` (seeded with one real `Leaf` row, NOT the
  live StockUI session -- didn't want to interrupt its actual in-progress
  work just to smoke-test a dashboard feature) connected as a reporter
  while a second WebSocket client acted as a viewer, sent a real
  `db_query` control message, and got back the exact seeded row over the
  real relay path end-to-end.

## Flat `.jfi/` folder, and review/feedback notes moved to the DB too

Per explicit follow-up decisions, two more changes beyond the DB layer
itself:

- **Everything JFI-related now lives in ONE flat, hidden `.jfi/` folder**
  at the project root — not the visible `JFI/` folder from before, and not
  a per-session `.jfi/<session_id>/` subfolder either (that was the first
  design, corrected once explicitly: "there is no .jfi/session/ there is
  only .jfi now"). `.jfi/JFI.db` (the shared database) and the handful of
  still-file-based artifacts (llm_debug.jsonl, web_status.json/
  web_answer.json, .lock) all sit directly in `.jfi/`. Real consequence
  the user explicitly accepted: the session lock (`.jfi/.lock`) is now
  PROJECT-WIDE, not per session_id — only one JFI session (of any name)
  can run against a given project at a time. `SimpleSessionManager.
  session_path`/`DEFAULT_PLAN_PATH`/`_resolve_session_file_path`,
  `db.py`'s `database_url`, `dashboard.py`'s session discovery (now a DB
  query via `SessionRecord`, since there's no per-session folder left to
  scan), and `export_db`'s output naming (session_id-PREFIXED filenames
  directly in `.jfi/`, since one run routinely exports every session at
  once into the same flat folder) all updated accordingly. Also fixed:
  `_acquire_session_lock`'s own error message, which used to suggest
  "pick a different session name" — no longer true once the lock is
  project-wide, so it now says what's actually true (stop the other run
  first). Cleaned up two leftover pre-existing scratch dirs from this
  repo's own earlier dev-testing (`./JFI`, `./.JFI` — already gitignored,
  never tracked) and updated `.gitignore` (`/.jfi/` is now the one entry
  that matters, with the two older layouts kept ignored for safety).
- **review.md / NotesForReviewer.md / feedback_to_plan.md → SessionNote**
  (new model, `src/JFI/models/session_note.py`) — per explicit "All
  should be in the db and used from the db" and "Remember it must be
  models" directives. One row per `(session_id, kind)`, `kind` ∈
  `reviewer_notes` / `review_report` / `plan_feedback`; deliberately a
  SEPARATE model from `ContextEntry` (the model's own free-form
  scratchpad) despite the identical shape, since these three specifically
  drive `runner.py`'s own control flow (review_outcome/
  product_owner_feedback_outcome), not just facts the model might want to
  recall. New tool module `src/JFI/tool/note_tools.py` with four
  model-facing tools (`add_reviewer_note`, `get_reviewer_notes`,
  `write_review_report`, `write_plan_feedback`) replacing the old
  write_file/append_to_file/read_file-on-a-specific-path pattern; every
  phase prompt, `get_phase_trigger`, `runner.review_outcome`/
  `product_owner_feedback_outcome`/`collect_next_iteration`/
  `_clear_reviewer_notes`/`_run_product_owner_loop`, the cleanup phase's
  protected-files list (much simpler now — just points at the whole
  `.jfi/` folder instead of naming three files that no longer exist), and
  `dashboard.py`'s review-report panel (now DB-backed and genuinely
  scoped to the selected session_id, fixing a caveat the flat-`.jfi/`
  file version had — different sessions could never disambiguate whose
  review.md was showing) all updated. `export_db` also exports these
  three as a new "Pending review/feedback notes" section.
- **Streamlit dashboard: new "Database" tab** — a plain, mechanical table
  browser (`st.selectbox` for which of the 12 SQLModel tables, a checkbox
  to scope to the selected session_id or show every session's rows,
  `st.dataframe` for the result) alongside the existing "Session" tab,
  per explicit request ("the table view would be good enough"). No
  purpose-built rendering — the Session tab's Plan/review/log sections
  already cover the tables that need one.
- Full suite re-verified green after each step (798 passed throughout),
  `dist/jfi` rebuilt each time. Smoke-tested the new Database tab two
  ways: a real `streamlit run` process against a seeded `.jfi/JFI.db`
  (server started cleanly, `/_stcore/health` OK, zero errors/tracebacks
  in its log across several seconds of real operation including at least
  one auto-refresh cycle) and a direct check of the exact
  query+`.model_dump()` logic the tab uses against all 12 tables
  (including the new `SessionNote` rows) — all returned real rows with no
  errors.

## Independent review (pre-live-session), and fixes applied

Per explicit request, before launching any new JFI session against
StockUI: a read-only independent review of this branch, focused on the
full-cutover DB migration above. Real findings, all fixed immediately
(full suite re-verified green after each, 799 passed; `dist/jfi` rebuilt):

- **Cleanup Agent's own prompt was actively dangerous.** It described
  session bookkeeping as living in a non-existent `session.db` inside the
  session folder, listed four files (context.json/history.jsonl.gz/
  metadata.json/run.log) that are never written anymore, and never
  mentioned `.JFI.db` at all — which sits in plain view at the PROJECT
  ROOT, exactly where cleanup's own instructions tell it to hunt for
  "stray" files via `find`/`ls -la`. A real risk the model deletes the
  shared session database as clutter. Rewrote the prompt to describe the
  DB-backed state accurately and explicitly, repeatedly protect
  `.JFI.db`. New test: `test_protects_the_shared_db_from_deletion`.
- **`src/JFI/web/dashboard.py` (jfi-web / port 7778) was pointed at the
  WRONG database file entirely.** `get_engine(session_path)` passed the
  *session* directory (`JFI/<name>`) as the project root, silently
  creating (and reading from) an empty `.JFI.db` two directories too deep
  instead of the real one every actual `jfi` session writes to at
  `SESSION_PATH/.JFI.db` — meaning the DB-backed Plan branch never
  actually fired; it silently fell back to legacy plan.md behavior every
  time. Fixed via a new `_project_root()` helper mirroring
  `SimpleSessionManager`'s own `SESSION_PATH` resolution.
- **Same dashboard's "Recent activity" panel was dead code** — it still
  read a literal `run.log` file that hasn't been written since the
  cutover, so the panel was permanently empty. Replaced with a
  `_recent_log_events()` query against `LogEvent`.
- **`PromptToolkitConsoleManager._log_seq` reset to 0 on every
  `start_session_db_log` call**, including on a RESUMED session that
  already has `LogEvent` rows from a prior process — colliding with
  existing seq values and breaking `export_db`'s `ORDER BY seq` output.
  Fixed by re-deriving it from `MAX(seq)` for that session_id, the same
  pattern `history_store.append_history_to_db` already used for
  `HistoryMessage.seq`.
- **`CONTEXT_CACHE_RULES` never told the model WHEN to call
  `context_lookup`** — under the new pull-only design (no auto-load), a
  model that doesn't independently form the habit simply never retrieves
  facts it saved earlier. Added an explicit trigger: call it with no
  keyword at the start of a new leaf/phase or before redoing research.
- **WAL mode enabled** (`PRAGMA journal_mode=WAL` + a 5s busy_timeout) in
  `get_engine` — the fleet dashboard, `export-db`, and a live session can
  now all open the same `.JFI.db` concurrently without a writer
  exclusive-locking every reader out for the write's duration. Cheap,
  idempotent, worth doing given multiple processes now routinely share
  one file by design.
- Confirmed NOT a live bug (verified directly, not just trusted): SQLite
  connections from one process share `check_same_thread=False` already
  set; `HistoryMessage`/`LogEvent`'s own `seq` counters are each derived
  from `MAX(seq)` per session_id already (history's was already correct —
  only the console's own `_log_seq` had the reset bug above).

## Testing standard for this change

End-to-end, not just unit tests of the models in isolation: create a fresh
JFI session against a throwaway scratch project, run it through planner
(confirm leaves land in the DB via the new tool API, not a file),
implement/tick a few leaves, run `uv run export-db` and confirm the
rendered markdown is readable and accurate, and confirm the dashboard
(`frontend/`) renders the same session's checklist correctly end-to-end
over whichever path open-decision #1 resolves to.

## Non-goals (original scope — since superseded, see below)

- ~~Do not touch `/home/tejas/PycharmProjects/StockUI/` or its running JFI
  session at all.~~ Superseded: the user explicitly asked for this branch's
  code to be exercised against a real StockUI session once the migration
  was solid enough (see "Live validation" above) — the guardrail was never
  "never touch it," just "don't touch it carelessly while still
  prototyping."
- ~~Do not run `uv run build` on this branch.~~ Superseded the same way —
  `dist/jfi` has been rebuilt from this branch multiple times since, each
  time the underlying Python source changed in a way that needed live
  validation.
- ~~Do not migrate `history.jsonl.gz` (conversation history) — out of
  scope.~~ Superseded by the explicit full-cutover request — see "Full
  cutover" section above.

---

# Existing backlog (unrelated to the above, rolled in unchanged)

- [ ] **Test with other models**
  - Smoke-test each candidate (set `MODEL` in `.env`, short conversation through the runner). *Status: only the current model (`qwen3.8-27b-ultra…`) was smoke-tested and passed; alternatives were not loaded at test time ("Failed to load model" — server may keep one model resident). Deeper testing deferred per user request.*
  - Run the `evals/csv2md` harness against each candidate and record pass/fail + latency.

- [ ] **Rename the LLM manager to OpenAI**
  - Rename `src/JFI/llm/colibri_llm_stream.py` → `openai_llm_stream.py`; class `ColibriLLMStream` → `OpenAILLMStream`.
  - Update references in `src/JFI/runner.py`, `test/test_themes.py`, and any docstrings/comments.

- [ ] **Make the tool-call cmd safer + approval on the cmd name**
  - Harden `execute_command()` in `src/JFI/tool/cmd_tools.py` (shlex parsing, empty/metacharacter guards, keep timeout) with unit tests.
  - Gate each command behind user approval before execution.
  - Add the cmd tool name (`execute_command`) to the project's approval list where that lives (no dedicated list exists yet — README's "The tool set" table is the natural home).

- [ ] **Run some benchmarks**
  - Run `evals/csv2md` (`drive.py` / `drive_pty.py`) with the current model; capture stdout/logs.
  - Compare against at least one alternative model if available.
  - Record results (pass/fail per task, wall time) in `todo.md` or `evals/RESULTS.md`.

- [ ] **Update the README**
  - Reflect the OpenAI LLM rename, safer cmd tool + its approval requirement, benchmark results, and model-testing notes in `README.md`.
