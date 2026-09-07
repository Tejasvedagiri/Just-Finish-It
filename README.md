# Just-Finish-It (JFI)

An autonomous, plan-driven coding agent. You give it a goal; it writes a step-by-step
plan, then implements and tests the work itself — ticking off each step in its own
workspace as it goes. The LLM does all of that by calling tools (`write_file`,
`read_file`, `append_to_file`, `replace_in_file`, `execute_command`) against your
project; JFI provides the loop, the terminal UI, and context management.

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
| `MODEL` | no | Model name to request; defaults to a sensible built-in if unset. |
| `CONTEXT_SIZE` | no | Context window of the model, in tokens. The session manager compresses history once the transcript would exceed `CONTEXT_SIZE * CONTEXT_COMPRESSION_RATIO`. Defaults: 32768. |
| `CONTEXT_COMPRESSION_RATIO` | no | Fraction (0–1) at which compression kicks in. Default: 0.7. |
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
The Rich renderer prints the same hint once at startup instead.

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
MODEL="qwen3.8-27b-ultra-uncensored-heretic-native-mtp-preserved"
CONTEXT_SIZE=32768
CONTEXT_COMPRESSION_RATIO=0.7
THEME=dark-ocean
```

## Project layout (at a glance)

| Path | Role |
|---|---|
| `JFI` | Repo-root executable launcher; prefers `.venv/bin/jfi`, falls back to the source tree. |
| `src/JFI/runner.py` | CLI entry point (`jfi`) and the main agent loop. |
| `src/JFI/session/simple_session_manager.py` | Session workspace, plan path resolution, phase triggers, context compression. |
| `src/JFI/manager/pt_console_manager.py` | The live terminal UI: full-screen prompt_toolkit app, status bar, theme presets, streaming display of LLM output. |
| `src/JFI/manager/rich_console_manager.py` | Alternative Rich-based terminal UI implementing the same manager interface. |
| `src/JFI/llm/colibri_llm_stream.py` | OpenAI-compatible streaming client. |
| `src/JFI/tool/file_tools.py`, `src/JFI/tool/cmd_tools.py` | The tools the LLM calls: file read/write/append/replace and shell execution. |

## Tests

```bash
uv run pytest
```

The test suite lives in `test/`; `.JFI/` is excluded from collection so your real
session plans never interfere with it.
