# Just-Finish-It (JFI)

An autonomous, plan-driven coding agent. You give it a goal; it writes a step-by-step
plan, then implements and tests the work itself — ticking off each step in its own
workspace as it goes. The LLM does all of that by calling tools (`write_file`,
`read_file`, `append_to_file`, `replace_in_file`, `execute_command`, `capture_screenshot`,
`view_image`) against your project; JFI provides the loop, the terminal UI, and context
management.

### What to expect: tokens vs. capability

JFI is deliberately **token-hungry**: each run issues many tool calls across four
phases (plan → implement → test → review), replays a lot of context, and keeps an
append-only transcript plus a per-session context cache — so a single session can
burn through a large share of your token budget. In exchange it is engineered to
run on **~31B-parameter models** (any OpenAI-compatible endpoint works) and still
produce results that are close to what much larger frontier-class models achieve.
If you want small-model quality without paying for frontier-scale inference, JFI's
multi-phase loop is the trade: more tokens, better outcomes.

> **TODO:** real-life evaluation — running JFI on a set of real-world tasks and
> comparing its output against baseline/frontier models — is still pending. The
> "close to frontier" claim above reflects internal observation so far, not yet a
> benchmarked result.

## How a run works (phase workflow)

A session moves through four phases in order:

1. **planner** — You describe the goal. The LLM writes a structured plan file with
   GitHub-style task-list items (`- [ ]` / `- [x]`) and section numbering
   (`1.` sections, `1.1` steps). Nothing else is allowed to generate this file:
   only the LLM's own tool calls may create or edit it.
2. **imp** (implementation) — The agent works through the plan one item at a time:
   read the current step, do the work with tools, tick exactly that checkbox, repeat
   until every implementation item is `- [x]`.
3. **testing** — A separate pass over the Testing items in the same plan file; each
   test gets its own checkbox and is verified by actually running it.
4. **reviewer** — Final review of the completed work before the run ends.

The plan itself lives inside a per-session workspace folder:

```
.JFI/<session_id>/plan.md
```

`.JFI/` is created automatically for every session (it is git-ignored) and holds the
plan plus small metadata about progress. The system messages handed to the model in
each phase always reference this exact path, so plans are generated *in* the `.JFI`
folder — never as a bare `./plan.md`.

### Context cache

Alongside the plan sits `.JFI/<session_id>/context.json` — a small, flat JSON file
the model can read and write with the same file tools it already uses, for facts
worth keeping across turns and phases (key decisions, discovered schema/API/config
details, gotchas) that would otherwise be lost once older turns get compressed out
of context. It starts as `{}` and every phase's system prompt tells the model where
it is; nothing else manages its contents — the model reads it when it needs earlier
context and rewrites it (via `read_file` then `write_file`) when it has something
worth keeping.

### Vision (screenshots and images)

Two separate tools, since a tool result can only ever be plain text on the wire:

- **`capture_screenshot(directory)`** grabs the primary monitor and saves it as an
  auto-numbered PNG (`screen-1.png`, `screen-2.png`, ...) inside `directory` — the
  model is told to pass its own `.JFI/<session_id>` folder. Needs a real display and
  the `mss` package; in a headless/CI environment it fails with a plain error message
  instead of crashing, and the model is instructed to skip the step and move on.
- **`view_image(file_path)`** reads that PNG (or a JPEG/GIF/WebP) back and attaches
  it to the conversation as an actual image the model can see on its next turn — not
  just text about it, the way `read_file` would be. Internally this is the one tool
  whose result isn't just a string: the runner appends a follow-up multimodal message
  with the image content right after the tool result.

Requires a model/endpoint with vision support. An 8MB size cap applies to both tools
(some OpenAI-compatible servers reject oversized request bodies), and the context
compressor charges a small flat token estimate per attached image (not proportional
to the image's raw size) so a screenshot can't blow the context budget on its own —
see [Configuration](#configuration-env) for `CONTEXT_SIZE`.

## Prerequisites

- Python **>= 3.12**
- An OpenAI-compatible API endpoint (any local server or hosted one works)
- Optionally: [uv](https://docs.astral.sh/uv/) — a fast dependency manager. Plain
  `pip` in a virtualenv works just as well.

## Installation

From the repository root, pick either path:

```bash
# Option A: uv (fastest; creates .venv and installs all dependencies)
uv sync

# Option B: plain pip + venv
python -m venv .venv && .venv/bin/pip install -e .
```

Both create a `.venv` with an editable install of this repo. `pyproject.toml`
declares a console script, so the CLI entry point (`jfi`) is available right after
install — no further steps needed.

## Running

The repository ships an executable launcher at the project root — `./JFI`. It
prefers the `.venv` install and falls back to running the package straight out of
`src/` (`python -m JFI.runner`), so you can invoke it directly without activating
anything:

```bash
# 1. straight from the repo root (no activation needed)
./JFI

# 2. activate the environment, then use either name — they're equivalent
source .venv/bin/activate
jfi          # installed console script (from pyproject.toml)
# or
JFI          # same launcher; with an active venv it resolves to `jfi`
```

Both are interactive and fully prompt-driven — no arguments needed:

1. You're asked for a **session name** (this becomes `<session_id>`, normalized to
   lowercase/underscores). Using an existing session name resumes that session,
   skipping phases it already finished.
2. You type your **goal**, as detailed as you like — this is fed straight into the
   planner phase, whose job is to turn it into `.JFI/<session_id>/plan.md`.
3. The agent then runs through `imp` → `testing` → `reviewer` on its own, ticking
   off plan items with real tool calls as it goes.

While a run is in flight you can type at any time: plain text queues the request to
be handled after the review phase; prefixing with `!` injects it into the current
turn immediately (`!run the tests now`). Type `exit`, `quit`, or `done` as a queued
request to end the session.

`.env` is loaded automatically on startup, so no further setup flags are needed.

## Configuration (`.env`)

All configuration lives in a `.env` file in the project root, loaded via
`python-dotenv` on startup:

| Variable | Required | Meaning |
|---|---|---|
| `OPENAI_URL` | yes | Base URL of an OpenAI-compatible endpoint, e.g. `http://127.0.0.1:1234/v1`. |
| `OPENAI_API_KEY` | yes | API key for that endpoint (any non-empty string works with local servers). |
| `MODEL` | no | Model name to request; defaults to `glm-5.3-flash-colibri` if unset. Any OpenAI-compatible model id works, including ~31B-parameter local models. |
| `TEMPERATURE` | no | Sampling temperature for LLM calls. Default: 0.7. |
| `CONTEXT_SIZE` | no | Context window of the model, in tokens. The session manager compresses history once the transcript would exceed `CONTEXT_SIZE * CONTEXT_COMPRESSION_RATIO`. Defaults: 32768. |
| `CONTEXT_COMPRESSION_RATIO` | no | Fraction (0–1) at which compression kicks in. Default: 0.7. |
| `SESSION_PATH` | no | Directory that the `.JFI/` session workspace is created under. Default: `.` (the project root), so sessions live in `<project>/.JFI/<session_id>/`. |
| `THEME` | no | Console color theme, see below. Empty or unset = auto-detect terminal background. |

### Themes

`THEME` picks one of six named presets; an explicit value always wins over
auto-detection. Leave it empty (or set `THEME=auto`) and JFI inspects your terminal
(`COLORFGBG`, TTY) to choose a dark or light palette automatically. Unknown values
log a hint and fall back safely — they never crash startup.

**Precedence rules:**

1. An explicit, known preset in `.env` always wins over auto-detection.
2. Empty, whitespace-only, unset, or the literal `auto` all mean "detect my terminal".
3. Values are case-insensitive and accept `_` or `-` as separators (`Dark_Ocean`,
   `dark_ocean`, and `dark-ocean` all select the same preset).
4. Unknown names print a `[system]` hint listing valid presets, then fall back to
   auto-detection (they never crash startup).

**Confirming your theme took effect:** the full-screen UI's header bar shows the
resolved source on every frame — e.g. `Just Finish It  ·  theme: env:THEME=dark-ocean`
when `.env` selected a preset, or `theme: auto (light)` when detection kicked in.

| Preset | Background |
|---|---|
| `dark-default` | dark |
| `dark-ocean`   | dark (cool blue/teal) |
| `dark-mono`    | dark (grayscale only) |
| `light-default`  | light |
| `light-sunrise`  | light (warm palette) |
| `light-paper`    | light (ink-on-paper, low saturation) |

Example:

```dotenv
OPENAI_URL="http://127.0.0.1:1234/v1"
OPENAI_API_KEY="local"
MODEL="qwen/qwen3.8-27b"
CONTEXT_SIZE=32768
CONTEXT_COMPRESSION_RATIO=0.7
THEME=dark-ocean
```

## Project layout (at a glance)

| Path | Role |
|---|---|
| `JFI` | Repo-root executable launcher; prefers `.venv/bin/jfi`, falls back to the source tree. |
| `src/JFI/runner.py` | CLI entry point (`jfi`) and the main agent loop: wires up session, LLM and console, runs the phase pipeline on a worker thread. |
| `src/JFI/session/simple_session_manager.py` | Session workspace (`.JFI/<session_id>/`), plan path resolution, phase triggers/completion, append-only gzip history, context cache and compression. |
| `src/JFI/manager/pt_console_manager.py` | The terminal UI: full-screen prompt_toolkit app, status bar, theme presets, streaming display of LLM output. |
| `src/JFI/manager/theme_env.py`, `src/JFI/manager/key_bindings.py` | Theme-preset resolution rules and the console's key bindings (extracted from the UI for testability). |
| `src/JFI/llm/base_llm_stream.py`, `src/JFI/llm/colibri_llm_stream.py` | Abstract streaming LLM base class (model/temperature, YES-NO approval check) and the OpenAI-compatible streaming client it builds on. |
| `src/JFI/tool/file_tools.py`, `src/JFI/tool/cmd_tools.py`, `src/JFI/tool/image_tools.py`, `src/JFI/tool/schemas.py` | The tools the LLM calls — file read/write/append/replace, shell execution, screenshot capture/viewing — plus their JSON schemas (`AVAILABLE_TOOLS`). |
| `src/JFI/orchestrator/basic_orchestrator.py` | Minimal generic orchestrator (system prompt + message list) available for simpler agent loops. |

Note: the repo also contains a separate `src/shbuild/` package (a standalone-`.sh`
builder CLI); it is independent of JFI and not covered by this README.

## Known limitations / TODO

- **Real-life evaluation pending.** JFI has been exercised on internal tasks only; a
  systematic comparison against baseline/frontier models on real-world tasks (with
  measurable success rates) is still to be done. Treat the "frontier-close results on
  ~31B-parameter models" claim as promising but not yet benchmarked.
- **Token usage is high by design.** The four-phase loop, tool-call replays and
  per-session context cache mean long sessions can consume a large token budget —
  budget your endpoint accordingly (see "What to expect: tokens vs. capability").

## Tests

```bash
uv run pytest
# or, in an activated venv: .venv/bin/python -m pytest
```

The test suite lives in `test/` (16 test modules covering plan parsing, phase
completion, context helpers, the session manager, themes and more); `.JFI/` is
excluded from collection so your real session plans never interfere with it.
