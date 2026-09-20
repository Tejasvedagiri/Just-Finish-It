# AGENTS.md

Working conventions for any agent (human-directed AI or otherwise) making
changes in this repo. This is about *how* to work here — see `README.md`
for what JFI is and how to run it.

## Repo shape

Two independently-built projects share this repo, deliberately kept apart:

- `src/JFI/` — the Python agent itself (`uv`-managed, PyInstaller-packaged
  via `uv run build`).
- `frontend/` — the fleet dashboard, a separate Node project (`npm run
  dev`/`npm run build`/`npm run master`). It has its own `package.json` and
  never shares dependencies or build tooling with the Python side.

Don't blur this line — e.g. don't reach for a Python templating step to
generate frontend assets, or vice versa.

## Before you start: check `uv run` vs plain `python3`

This project always runs through `uv` (`uv run pytest`, `uv run build`,
`uv run create-env`, the `create-env`/`build` console scripts). Plain
`python3 …` will fail with `ModuleNotFoundError` since dependencies live in
`uv`'s venv, not system Python. If you're unsure a command is right, try
`uv run <cmd>` first rather than debugging a bare-interpreter failure.

## After changing Python code

1. `uv run pytest` — run the **whole** suite, not just the file you
   touched. Tests here frequently exercise cross-module wiring (e.g.
   `runner.py`'s per-phase LLM construction, `simple_session_manager.py`'s
   prompt text), so a change in one file can silently break assertions in
   another test file that string-matches prompt content.
2. `uv run ruff check <changed files>` (or `uv run ruff check .` — note the
   repo currently has 2 pre-existing `E741` warnings under
   `benchmark/tasks/**/verify_story.py` that are not part of the shipped
   package; don't feel obligated to fix unrelated pre-existing lint noise
   in the same change).
3. If you touched anything under `src/JFI/` that ships in the binary,
   `uv run build` to confirm PyInstaller still packages cleanly — cheap
   insurance, catches missing-import surprises before they reach a user
   running `dist/jfi` instead of from source.

## After changing frontend code (`frontend/`)

`npm run build` from `frontend/`, then verify live rather than trusting the
diff — either open the dashboard in a browser or curl the served asset.
`master.js` serves `index.html` with `no-cache` and hashed assets with
`immutable` caching (see its `staticResponse()`) specifically so a rebuild
is picked up without a stale-bundle bug; if you ever see the dashboard
showing an old feature that's definitely in the built JS, suspect
Cache-Control/hard-refresh before suspecting the build itself.

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
  of it. When a frontend parsing bug was suspected, it was confirmed by
  extracting and running the *actual* functions from `main.js` against a
  real `plan.md` file, not by re-deriving expected output by eye.
- "The pipeline reported success" is not evidence of correctness — verify
  independently (fresh process, hand-computed expected values, direct
  field-by-field inspection of a response) before trusting a "done" claim,
  whether it's JFI's own review pass or your own prior turn's assumption.
  See `.claude/skills/run-jfi/SKILL.md` §9 for a concrete case of this
  going wrong (a stale server process masking a real regression).

## `plan.md` format (if you're editing the planner prompts)

- `## Implementation` and `## Testing` are separately-numbered trees.
- Only leaves get a checkbox: `- [ ] 1.1.1 Description` (no trailing period
  after the number).
- Parent/branch bullets are plain, no checkbox, **with** a trailing period
  after the number: `- 1. Description`. Any code that parses this file
  (see `frontend/src/main.js`'s `parsePlanLines`) must treat that period as
  optional-and-uncaptured, not part of the number — capturing it broke
  every parent/child prefix match once already.
- Never hand-patch numbering after inserting/removing a leaf; run
  `python -m JFI.tool.plan_renumber <plan_path> <parent-number>`.

## Code style (see also the root system prompt this agent operates under)

- No comments explaining *what* code does — names should do that. A
  comment is only earned by a non-obvious *why* (a workaround, an
  invariant, a past bug it prevents).
- No speculative abstraction — three similar call sites beat a premature
  helper. Don't add config/flags for a case nobody asked for yet.
- Don't add error handling for scenarios that can't occur given the
  codebase's own guarantees; validate only at real boundaries (user input,
  external APIs/processes).

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
