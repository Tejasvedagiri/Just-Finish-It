# AGENTS.md

Working conventions for any agent (human-directed AI or otherwise) making
changes in this repo. This is about *how* to work here — see `README.md`
for what JFI is and how to run it (but see "Docs that have drifted" below
before trusting README's file/path details).

## Read first: how the pipeline works

Before you change `runner.py`, any phase prompt, or the plan tools, read
[`docs/pipeline.md`](docs/pipeline.md) (call chain, episodes, resume,
iterations, known gaps). Then read the doc for the phase you're touching:

- [`docs/phase-planner.md`](docs/phase-planner.md) (Architect → Lead → Task, judged)
- [`docs/phase-imp.md`](docs/phase-imp.md) (one Dev episode per leaf)
- [`docs/phase-reviewer.md`](docs/phase-reviewer.md)
- [`docs/phase-cleanup.md`](docs/phase-cleanup.md)
- [`docs/plan-tree.md`](docs/plan-tree.md) (the `Leaf` model and plan read tools)

If you change behavior those docs describe, update them in the same
change. If a doc disagrees with the code, the code wins; fix the doc.

## The v2 pipeline

JFI was rewritten (complete 2026-09-29): Architect → Lead → Task planning in
short, scoped episodes with a judge between layers, then one Dev episode per
leaf. **v2 is the only pipeline.** v1 (the tiered Arc → Lead → Dev →
Task Planner planner, the Program Manager phase, the testing phase, the
adaptive session manager and v1's plan-editing tools) was deleted; its
sessions can't be resumed (`_run_session` refuses them by
`SessionRecord.pipeline_version`), but `export-db` still reads them, so the
`product_owner`/`testing` enum values and v1 columns stay. If your task
touches the planner, imp, reviewer, the plan tools or the DB schema, read
these first:

- [`docs/laya_plan.md`](docs/laya_plan.md): the design and every agreed decision;
- [`docs/laya_impl_phases.md`](docs/laya_impl_phases.md): how it was built,
  phase by phase, and the log of real runs and the bugs they found.

The code is `src/JFI/episode/` (scoped episodes), `src/JFI/planner/`,
`src/JFI/imp/` and `src/JFI/review/` (reviewer + cleanup); `run_phase` hands
every phase to them. There is no long, compressed conversation any more:
every LLM call is one short episode.

The planner's judge is a fixed rule; `LAYA=1` in `.env` adds a second score
from Laya's published `english` checkpoint, and an LLM breaks the tie when
they disagree and Laya is confident (`LAYA_MIN_CONFIDENCE`). Laya isn't fine-tuned:
fine-tunes overfitted to the node's level (see `docs/phase-planner.md`).

Both are a reference now: record later design changes in the `docs/phase-*.md`
page they touch.

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

Where things live:

| Path | What it is |
|------|------------|
| `src/JFI/runner.py` | `main()` → `run_pipeline()` → `_run_session()` → `run_phase()` → the phase's episodes. Owns `PHASES`, `TOOL_MAP` (the shared tools, rebound per session), per-phase LLM construction (`PHASE_ENV_PREFIX`), the judge per session, and the review/iteration loop. |
| `src/JFI/session/simple_session_manager.py` | Session persistence: history (DB, legacy file migration), metadata, plan progress, the `.jfi/.lock`. |
| `src/JFI/episode/`, `src/JFI/planner/`, `src/JFI/imp/` | The episode engine, the planner (loop, node tools, judge) and Dev (queue, gated `mark_leaf_done`). |
| `src/JFI/models/` | SQLModel tables — the session's real state (see "Where session state lives"). |
| `src/JFI/tool/` | Everything the model can call. `schemas.py` = JSON schemas; one module per tool family; `plan_db_tools.py` = the plan's read tools and display helpers. |
| `src/JFI/llm/` | `BaseLLMStream` + OpenAI-compatible and Anthropic backends; `backend_select.py` picks one from `LLM_BACKEND`. |
| `src/JFI/manager/` | `AbstractManager` (console contract; `PHASE_DISPLAY_NAMES`), the prompt_toolkit TUI (`pt_console_manager.py`, `key_bindings.py`, `theme_env.py`), plus `web_bridge.py` (file-based, for `jfi-web`) and `socket_reporter.py` (WebSocket client, for the fleet master). |
| `src/JFI/utils/` | Small shared helpers that belong to no one subsystem (`text_sanitize.py`: strips leaked chat-template tokens from model output). Not the repo-root `utils/`, which is dev-only scripts. |
| `src/JFI/web/` | Optional Streamlit `jfi-web` dashboard (`dashboard.py`) and its console-script `launcher.py` (`--extra web`). |
| `src/JFI/create_env.py`, `src/build_binary/`, `src/export_db/` | `uv run create-env` / `build` / `export-db` console scripts. |
| `test/` | pytest suite. `benchmark/` is a separate, non-shipped eval harness (see its README). `utils/` and `docs/` are dev-only. |

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
  `context.json` or `history.jsonl.gz` as a source of truth are legacy;
  nothing reads a `plan.md` any more.
- Adding a column to an existing table: `create_all` never alters
  existing tables — add it to `_ensure_columns` in `models/db.py` too, or
  older project DBs won't get it (SQLite only).
- To inspect a DB by hand, `uv run export-db [project-dir]` dumps it to
  readable text. It's one-way — nothing may ever read those exports back.

## Pipeline and planner shape (when editing `runner.py` or prompts)

- Phase **keys** (`planner`, `imp`, `reviewer`, `cleanup`) and their
  `<PHASE>_COMPLETE` markers are persisted in history and drive resume —
  never rename them (the fleet dashboard mirrors them too). Change only
  `PHASE_DISPLAY_NAMES` in `manager/abstract_manager.py` for UI text.
- The planner loop (`JFI.planner.loop`) re-reads the DB every step, so
  resume is "run again"; `PLANNER_COMPLETE` is appended when every node is
  GOOD. imp writes `IMP_COMPLETE` when every leaf is finished. Neither
  trusts model text for completion.
- A failed review loops everything again, capped by `MAX_REVIEW_ITERATIONS`;
  the review report reaches the planner as the iteration's feedback
  (`runner._replan_feedback`).

## The plan tree (if you're editing planner prompts or the node tools)

- The plan is `Leaf` rows (`models/leaf.py`), written only through the
  planner's node tools (`JFI.planner.nodes`) and Dev's gated
  `mark_leaf_done` (`JFI.imp.dev`) — never by text edits. The reviewer
  reads it with `get_plan`/`get_leaf` (`tool/plan_db_tools.py`).
- Parent vs leaf is structural (has children or not), not a flag. Only
  real leaves carry status/timing.
- Siblings are ordered by a gap-numbered `sort_key` (10, 20, 30…). Dot
  numbers like `1.1.2` are **computed for display only**
  (`display_number`). Never store or hand-maintain them.
- Depth is capped mechanically (`MAX_LEAF_DEPTH` in `JFI.planner.nodes`)
  because prompt guidance alone didn't stop runaway recursive splitting in
  a real run. Keep that backstop in code, not just prompt text; the same
  goes for the duplicate, ownership and `depends_on` checks there.
- `render_plan_markdown` renders the old `- [ ] N.M` markdown, plain (the
  judge's scores are in the dashboard's Task | Judge table), for the status
  bar and the fleet dashboard only. The table itself reaches the fleet as
  `plan_detail` in the status snapshot (`plan_status_fields`: the
  `plan_judge_rows`, runbook and design); the fleet's `renderJudgePanel`
  reads those row keys, so renaming a column means changing it there too.

## Adding or changing a model-facing tool

1. Schema: shared tools go in `tool/schemas.py`'s `TOOL_SCHEMAS`; a tool
   family with its own module keeps its schemas beside it (the planner's
   `NODE_TOOL_SCHEMAS`, `CODE_TOOL_SCHEMAS`, `RUNBOOK_TOOL_SCHEMAS`, ...)
   and is added to `episode/tools.py`'s `_known_schemas`.
2. Who gets it: a role's core set in `episode/roles.py`
   (`ROLE_CORE_TOOLS`), or `OPTIONAL_POOL` for a rarely-needed tool that
   any episode can `load_tool` for itself.
3. The implementation reaches the episode through the role's tool dict:
   `make_*_tools(engine, session_id)` factories, or `TOOL_MAP` in
   `runner.py` (rebound per session in `_run_session`) for the shared and
   pool tools.
4. Return a string (only `view_image` returns a tuple). Start failures
   with `Error` (that's all `episode/rectify.is_failure` checks) so the
   AUTO-RECTIFY coaching and retry counting pick them up.
5. Mention it in the role's prompt (`JFI.planner.prompts`,
   `JFI.imp.prompts`, `JFI.review.prompts`) -- prompt tests string-match
   that text.

## After changing Python code

1. `uv run pytest` — run the **whole** suite, not just the file you
   touched. Tests here frequently exercise cross-module wiring (e.g.
   `runner.py`'s per-phase LLM construction, the role prompts), so a change
   in one file can silently break assertions in another test file that
   string-matches prompt content.
   - The Anthropic tests need `--extra anthropic` synced (see below).
   - **On Windows** the suite doesn't fully pass: `test_session_lock.py`
     fails to import (`fcntl` is POSIX-only — pass
     `--ignore=test/test_session_lock.py`), `test_process_tools.py`'s
     stop tests need `os.getpgid` (3 failures). Compare against that
     baseline rather than chasing it, and don't "fix" the locking or
     process-group code for Windows as a side effect of an unrelated
     change. The project targets Linux/macOS.
2. `uv run ruff check <changed files>` (or `uv run ruff check .` — the
   repo currently has 2 pre-existing `E741` warnings under
   `benchmark/tasks/**/verify_story.py`; don't feel obligated to fix
   unrelated pre-existing lint noise in the same change).
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

## Context cache convention (for role prompts)

`context_save` / `context_lookup` are in every episode's optional pool, but
an episode only loads them when its prompt says why. If you add a role or
stage that inspects the same files across many small steps, tell it
explicitly to `context_lookup` before re-opening a file and `context_save`
what it learns -- the model doesn't do it unprompted.

## Docs that have drifted (trust the code)

- `README.md`'s Getting started, Configuration, Resuming and build sections
  are current (clone → `uv sync` → `create-env` → run in the project folder
  → `uv run build`). Its "Project layout" tree is still stale: no
  `models/`, `plan_db_tools.py`, `anthropic_stream.py`, `export_db/`,
  `episode/`, `planner/`, `imp/`, `review/` or `utils/`.
- `manager/web_bridge.py`'s and `web/dashboard.py`'s docstrings still say
  the bridge files live in `.jfi/<session>/`; they're directly in `.jfi/`.
- Many docstrings cite `/todo.md` or `todo_v1.md §N` for rationale; both
  files have been removed. Don't add new references to them. Put the
  reasoning in the docstring itself.

If you touch one of these areas, fixing the matching doc in the same
change is welcome.
