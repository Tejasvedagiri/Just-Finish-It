# AGENTS.md

Working conventions for any agent (human-directed AI or otherwise) making
changes in this repo. This is about *how* to work here — see `README.md`
for what JFI is and how to run it (but see "Docs that have drifted" below
before trusting README's file/path details).

## Read first: how the pipeline works

Before you change `runner.py`, any phase prompt, or the plan tools, read
[`docs/pipeline.md`](docs/pipeline.md) (call chain, turn loop, resume,
iterations, known gaps). Then read the doc for the phase you're touching:

- [`docs/phase-planner.md`](docs/phase-planner.md) (Arc → Lead → Dev → Task Planner)
- [`docs/phase-product-owner.md`](docs/phase-product-owner.md)
- [`docs/phase-imp.md`](docs/phase-imp.md)
- [`docs/phase-testing.md`](docs/phase-testing.md)
- [`docs/phase-reviewer.md`](docs/phase-reviewer.md)
- [`docs/phase-cleanup.md`](docs/phase-cleanup.md)
- [`docs/plan-tree.md`](docs/plan-tree.md) (the `Leaf` model and plan tools)

If you change behavior those docs describe, update them in the same
change. If a doc disagrees with the code, the code wins; fix the doc.

## Repo shape

This repo is the Python agent (`src/JFI/`, `uv`-managed,
PyInstaller-packaged via `uv run build`). It has no Node/npm tooling, but
it does have UI — all of it Python:

- **The terminal UI** (`manager/pt_console_manager.py`, prompt_toolkit) —
  what `uv run jfi` shows. Themes (`THEME=`, `PT_THEME_PRESETS`) are
  documented in `docs/Themes.md`; `utils/capture_theme_screenshots.py`
  regenerates `docs/images/`.
- **`jfi-web`** (`web/dashboard.py`, Streamlit, `--extra web`) — a local
  dashboard. It's a separate process that reads the DB directly and talks
  to the running session only through the `web_status.json` /
  `web_answer.json` files that `manager/web_bridge.py` writes when
  `JFI_WEB_BRIDGE=1`. Both sides of that file contract live here.

What's *not* here is the **fleet dashboard** (the old `frontend/`: a Vite
UI plus the `master.js` WebSocket server). It moved to its own repo,
[`Just-Finish-It-Fleet`](https://github.com/Tejasvedagiri/Just-Finish-It-Fleet);
fleet-dashboard work (and its `npm` build/verify steps) belongs there. Don't
recreate `frontend/` or add JS tooling here.

The only link between this repo and the fleet repo is a WebSocket
(`MASTER_WS_URL`, see `manager/socket_reporter.py`) — no shared code, build
tooling, or filesystem access. Many docstrings here still name that repo's
`src/main.js` / `master.js` as the other side of a contract (e.g.
`models/enums.py`'s `Phase` "mirrors PHASES in main.js"); if you change one
of those contracts (phase keys, reporter message shapes), the matching
change has to be made in `Just-Finish-It-Fleet` too.

`design-mockups/plan-list/` is standalone HTML mockups for the **fleet**
dashboard's plan view (they cite `frontend/src/main.js`). Nothing here
loads them; treat them as reference for fleet work, not as a UI of this
repo.

Where things live:

| Path | What it is |
|------|------------|
| `src/JFI/runner.py` | `main()` → `run_pipeline()` → `_run_session()` → `run_phase()` → `_drive_turn_loop()`. Owns `PHASES`, `TOOL_MAP`, per-phase LLM construction (`PHASE_ENV_PREFIX`), the tiered planner, the product-owner and review loops, tool-call execution + `AUTO-RECTIFY` coaching. |
| `src/JFI/session/simple_session_manager.py` | All phase/stage **prompt text** (`get_system_message`, `get_phase_trigger`, `PLAN_FORMAT_RULES`, `CONTEXT_CACHE_RULES`, `VERIFICATION_RULES`), history compression/digests, context-window budgeting, the `.jfi/.lock`. |
| `src/JFI/session/adaptive_session_manager.py` + `task_rules.py` | Default `SESSION_MANAGER=adaptive`: detects goal type (python/javascript/go/sql/html-css/data-eng/story) and swaps in type-specific plan rules. |
| `src/JFI/models/` | SQLModel tables — the session's real state (see "Where session state lives"). |
| `src/JFI/tool/` | Everything the model can call. `schemas.py` = JSON schemas; one module per tool family; `plan_db_tools.py` = the plan tree API. |
| `src/JFI/llm/` | `BaseLLMStream` + OpenAI-compatible and Anthropic backends; `backend_select.py` picks one from `LLM_BACKEND`. |
| `src/JFI/manager/` | `AbstractManager` (console contract; `PHASE_DISPLAY_NAMES`), the prompt_toolkit TUI (`pt_console_manager.py`, `key_bindings.py`, `theme_env.py`), plus `web_bridge.py` (file-based, for `jfi-web`) and `socket_reporter.py` (WebSocket client, for the fleet master). |
| `src/JFI/web/` | Optional Streamlit `jfi-web` dashboard (`dashboard.py`) and its console-script `launcher.py` (`--extra web`). |
| `src/JFI/orchestrator/` | Legacy `BaseOrchestrator`; `runner.py` doesn't use it. Don't build on it. |
| `src/JFI/create_env.py`, `src/build_binary/`, `src/export_db/` | `uv run create-env` / `build` / `export-db` console scripts. |
| `test/` | pytest suite. `benchmark/` is a separate, non-shipped eval harness (see its README). `utils/`, `docs/`, `design-mockups/` are dev-only. |

## Before you start: check `uv run` vs plain `python3`

This project always runs through `uv` (`uv run pytest`, `uv run build`,
`uv run create-env`, `uv run export-db`, `uv run jfi`). Plain `python3 …`
will fail with `ModuleNotFoundError` since dependencies live in `uv`'s
venv, not system Python. If you're unsure a command is right, try
`uv run <cmd>` first rather than debugging a bare-interpreter failure.

## Where session state lives

- Everything is under one **flat, hidden `.jfi/`** folder at the project
  root (`SESSION_PATH`, default cwd) — no per-session subfolders. The real
  store is `.jfi/JFI.db` (SQLite, WAL mode; or `DB_BACKEND=mysql|postgres`
  + `DATABASE_URL`), **one DB per project**, every table keyed by
  `session_id`. The only files left beside it are `.lock`,
  `llm_debug.jsonl`, and the `web_status.json`/`web_answer.json` bridge
  files. `models/__init__.py`'s docstring has the old-file → table map.
- `.jfi/.lock` is **project-wide**: only one JFI session (any name) can run
  against a project at a time.
- The plan, history, context cache, reviewer notes / review report / plan
  feedback (`SessionNote`), queue, processes, and unlocked tools are all
  DB rows now. Code or prompts that read/write `plan.md`,
  `NotesForReviewer.md`, `review.md`, `feedback_to_plan.md`,
  `context.json` or `history.jsonl.gz` as a source of truth are legacy.
  `plan_path`/`DEFAULT_PLAN_PATH` still exist only as a fallback/display
  path.
- Adding a column to an existing table: `create_all` never alters
  existing tables — add it to `_ensure_columns` in `models/db.py` too, or
  older project DBs won't get it (SQLite only).
- To inspect a DB by hand, `uv run export-db [project-dir]` dumps it to
  readable text. It's one-way — nothing may ever read those exports back.

## Pipeline and planner shape (when editing `runner.py` or prompts)

- Phase **keys** (`planner`, `product_owner`, `imp`, `testing`,
  `reviewer`, `cleanup`) and their `<PHASE>_COMPLETE` markers are
  persisted in history and drive resume — never rename them. Change only
  `PHASE_DISPLAY_NAMES` in `manager/abstract_manager.py` for UI text
  (e.g. `product_owner` displays as "Program Manager").
- The planner is tiered by default: Architect once over the whole tree
  (`ARCHITECT_STAGE_COMPLETE`), then **depth-first per top-level leaf**
  Lead → Dev → Task Planner (`team_lead`/`journeyman`/`function_breakdown`),
  each emitting a node-scoped marker like `LEAD_STAGE_COMPLETE_NODE_7`.
  Resume works by scanning history for those markers; `PLANNER_COMPLETE`
  is appended synthetically. `PLANNER_SINGLE_PASS=1` opts out.
- product_owner reviews **per leaf** via `review_leaf` (approve/reject
  with `expected_changes`, see `Leaf.review_status`/`rejection_count`)
  and can send the plan back with `write_plan_feedback`; capped by
  `MAX_PRODUCT_OWNER_ITERATIONS`. A failed review loops everything again,
  capped by `MAX_REVIEW_ITERATIONS`.

## The plan tree (if you're editing planner prompts or `plan_db_tools.py`)

- The plan is `Leaf` rows (`models/leaf.py`), edited only through the
  tools in `tool/plan_db_tools.py` (`get_plan`, `get_leaf`, `add_leaf`,
  `start_leaf`, `mark_leaf_done`, `split_leaf`, `merge_leaf`,
  `reorder_leaf`, `delete_leaf`, `update_leaf`, `review_leaf`) — never by
  text edits.
- Parent vs leaf is structural (has children or not), not a flag. Only
  real leaves carry status/timing; the tools reject status changes on a
  parent.
- Siblings are ordered by a gap-numbered `sort_key` (10, 20, 30…). Dot
  numbers like `1.1.2` are **computed for display only**
  (`display_number`), per phase — Implementation and Testing are
  separately numbered trees. Never store or hand-maintain them.
- Depth is capped mechanically (`MAX_LEAF_DEPTH`, and a stricter
  `MAX_INVESTIGATION_DEPTH` for diagnostic branches) because prompt
  guidance alone didn't stop runaway recursive splitting in a real run.
  Keep that backstop in code, not just prompt text.
- `render_plan_markdown` renders the old `- [ ] N.M` markdown for
  display/`export-db` only. `tool/plan_renumber.py` is legacy from the
  markdown era — don't point new prompts at it.

## Adding or changing a model-facing tool

1. Schema in `tool/schemas.py` — `DEFERRED_TOOLS` (locked until the model
   calls `load_tool`) unless it's needed nearly every turn. A deferred
   tool also needs a one-line entry in `_DEFERRED_TOOL_SUMMARIES`; an
   import-time `assert` enforces the two stay in sync.
2. A `TOOL_MAP` entry in `runner.py`. Tools that need the session's DB
   engine get a placeholder lambda there ("not available yet — no session
   is active") and are rebound in `_run_session` via a
   `make_*_tools(engine, session_id)` factory, like
   `make_context_tools`/`make_note_tools`/`ssm.plan_db_tools()`.
3. Return a string (only `view_image` returns a tuple). Start failures
   with `Error` (that's all `_is_failure` checks) so the AUTO-RECTIFY
   coaching and retry counting pick them up.
4. Mention it in the relevant phase prompt in
   `simple_session_manager.py`/`task_rules.py` — prompt tests
   string-match that text.

## After changing Python code

1. `uv run pytest` — run the **whole** suite, not just the file you
   touched. Tests here frequently exercise cross-module wiring (e.g.
   `runner.py`'s per-phase LLM construction, `simple_session_manager.py`'s
   prompt text), so a change in one file can silently break assertions in
   another test file that string-matches prompt content.
   - The Anthropic tests need `--extra anthropic` synced (see below).
   - **On Windows** the suite doesn't fully pass: `test_session_lock.py`
     fails to import (`fcntl` is POSIX-only — pass
     `--ignore=test/test_session_lock.py`), `test_process_tools.py`'s
     stop tests need `os.getpgid`, and `test_plan_location.py` /
     `test_get_system_message.py` assert `/` path separators. Compare
     against that baseline rather than chasing it, and don't "fix" the
     locking or process-group code for Windows as a side effect of an
     unrelated change. The project targets Linux/macOS.
2. `uv run ruff check <changed files>` (or `uv run ruff check .` — the
   repo currently has pre-existing warnings: 2 `E741` under
   `benchmark/tasks/**/verify_story.py` and a few `F401`/`F541` in
   `pt_console_manager.py`/`simple_session_manager.py`; don't feel
   obligated to fix unrelated pre-existing lint noise in the same change).
3. If you touched anything under `src/JFI/` that ships in the binary,
   `uv run build` to confirm PyInstaller still packages cleanly — cheap
   insurance, catches missing-import surprises before they reach a user
   running `dist/jfi` instead of from source.

## `uv sync --extra` — list every extra you want, every time

`uv sync --extra web` **removes** any other extra (e.g. `master`,
`anthropic`) that was previously synced but isn't named in that exact
command — it's not additive across separate invocations. Always list every
extra you need together: `uv sync --extra web --extra master --extra
anthropic --group dev`.

## Testing philosophy observed in this repo

- A test should pin an actually-observed behavior or a bug that actually
  happened, not a hypothetical. Test names and docstrings here read like
  "Observed in practice: X was doing Y" — follow that pattern; it tells the
  next person *why* the assertion exists, not just what it checks.
- Prefer testing against the real thing over a hand-copied reimplementation
  of it, not re-deriving expected output by eye. For plan logic, that
  means a real SQLite engine (tests build one in a tmp dir), not mocked
  `Leaf` objects.
- "The pipeline reported success" is not evidence of correctness — verify
  independently (fresh process, hand-computed expected values, direct
  field-by-field inspection of a response) before trusting a "done" claim,
  whether it's JFI's own review pass or your own prior turn's assumption.
  See `.claude/skills/run-jfi/SKILL.md` §9 for a concrete case of this
  going wrong (a stale server process masking a real regression).

## Code style (see also the root system prompt this agent operates under)

- No comments explaining *what* code does — names should do that. A
  comment is only earned by a non-obvious *why* (a workaround, an
  invariant, a past bug it prevents). The codebase's long prose comments
  are all of that kind — match them when you record a real failure that
  motivated a design, don't add narration.
- No speculative abstraction — three similar call sites beat a premature
  helper. Don't add config/flags for a case nobody asked for yet.
- Don't add error handling for scenarios that can't occur given the
  codebase's own guarantees; validate only at real boundaries (user input,
  external APIs/processes).
- New env vars: read them where they're used (see the `_env_flag`-style
  helpers in `runner.py`), document them in `JFI_ENV_TEMPLATE`, and, if
  they're per-phase, go through `phase_env` in `llm/base_llm_stream.py`.

## Context cache convention (for planner/session prompt work)

`src/JFI/session/simple_session_manager.py`'s `CONTEXT_CACHE_RULES` +
per-stage prompts teach the model to use `context_save`/`context_lookup`
instead of re-reading full source files repeatedly. If you add a new
planner/phase stage that does repeated file inspection across many small
steps (e.g. a leaf-by-leaf pass like Function Breakdown), explicitly tell
it to check `context_lookup` before re-opening a file it's already read
this pass, and `context_save` what it learns — this doesn't happen for
free just because `CONTEXT_CACHE_RULES` is in scope; it needs a concrete,
stage-specific nudge or the model defaults back to re-reading.

## Docs that have drifted (trust the code)

- `README.md` still describes the pre-DB layout in places: `JFI/readme/plan.md`,
  `history.json`, per-session `JFI/<session>/` folders, `.env_bk`, and a
  `./JFI` bash launcher that isn't in the repo. Today it's the flat `.jfi/`
  + `JFI.db`, `.env` (loaded by `find_dotenv()` in `runner.main`), and
  `uv run jfi`. Its "Project layout" tree is also stale: no `models/`,
  `plan_db_tools.py`, `anthropic_stream.py`, `export_db/` or `benchmark/`,
  and it describes `dashboard.py` as reading `JFI/<session>/*.json` +
  `plan.md`.
- `manager/web_bridge.py`'s and `web/dashboard.py`'s docstrings still say
  the bridge files live in `.jfi/<session>/`; they're directly in `.jfi/`.
- Many docstrings cite `/todo.md` or `todo_v1.md §N` for rationale; both
  files have been removed. Don't add new references to them. Put the
  reasoning in the docstring itself.

If you touch one of these areas, fixing the matching doc in the same
change is welcome.
