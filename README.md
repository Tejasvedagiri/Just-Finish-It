# Just Finish It (JFI) 🤖

**The idea: any problem can be solved by a small model if you break it down far enough.** Rather than asking one model call to hold a whole project in its head, JFI decomposes a goal into a plan, and the plan into small, single-focus checkboxes — then hands the model exactly one checkbox at a time, never the whole picture at once. That takes more turns and more tokens than a single frontier-model pass would, but it's a trade well worth making: a modest local model, given a small enough piece, solves it just as reliably as a much bigger one. **planner → implementer → tester → reviewer**, one checkbox at a time, against any OpenAI-compatible endpoint, until the plan reads all `[x]`.

The same idea — solve the smallest piece in front of you, not the whole problem at once — carries all the way down to how JFI manages memory, not just tasks:

- A **persisted plan file** (`.JFI/readme/plan.md`) in strict `- [ ] N.M` / `- [x] N.M` checkbox form (plus a third state, `- [○] N.M`, for an item you skipped with Ctrl+K — see [Live input](#live-input-queue-force-idle)) — the single source of truth for what's done, re-read by every phase and every restart. `imp`/`testing` are only ever handed their own *currently unchecked* items, freshly parsed from the file each turn, rather than the whole plan.
- A **context cache** (`context.json`) the model writes its own distilled notes to — key decisions, discovered schema/API details, approved shell-command prefixes — so a fact stays available long after the turn that discovered it has scrolled out of view.
- An append-only session transcript (`history.jsonl.gz`) plus a live `run.log`, which is what makes resuming mid-session — even mid-phase — reliable.

Even a single request to the model gets the same treatment: instead of the full, ever-growing transcript, each turn sends just the slice that's actually relevant to the step at hand — see [Context management](#context-management). In exchange, JFI runs well on ~27B–31B-parameter local models, producing results close to frontier-class agents at a fraction of the API cost, given enough context window and patience. See [TODO](#todo) for a note on how that claim is currently validated.

---

## Context management

The same "smallest piece in front of you" idea applies to memory as much as to tasks. The full transcript is kept forever in `.JFI/<session>/history.jsonl.gz`, but what actually goes into each request is a small, freshly-trimmed slice of it — recomputed every turn by `SimpleSessionManager.compress_history` (`src/JFI/session/simple_session_manager.py`) to fit the model's own context budget. Trimming works in tiers, cheapest loss first, stopping the moment things fit:

1. **Drop stall-prevention nudges** — pure filler, repeated often.
2. **Trim long content** — big tool output or text blocks keep their head and tail, and lose the middle.
3. **Point at files instead of quoting them** — a `write_file`/`append_to_file` call's content is dropped from history once it's safely landed in the real file; the model just `read_file`s it back if it's ever needed again.
4. **Summarize the rest** — everything still over budget in the middle becomes one short factual note: files touched, commands run, tool-call counts.
5. **Age out the oldest turns**, as a last step, keeping the opening goal and that summary note intact.

The budget is `CONTEXT_SIZE × CONTEXT_COMPRESSION_RATIO` (see [Configuration](#configuration-env)), recomputed every turn; the header's live `ctx` counter and the `🗜 Context compressed: ~566357 → ~7341 tokens` console line show exactly what was sent. Nothing important is ever lost — it's either right there in the current slice, waiting on disk (the plan, the context cache, the files themselves), or summarized well enough to point the model back to it.

---

## The four-phase pipeline

Every JFI session runs the same four phases in order:

| Phase     | What it does                                                                                          |
|-----------|--------------------------------------------------------------------------------------------------------|
| **planner**   | Writes a step-by-step plan to `.JFI/readme/plan.md`, every item as a `- [ ] N.M` checkbox.             |
| **imp**       | Implements the code, ticking each box with `replace_in_file` *immediately* after finishing that one item — never batched. |
| **testing**   | Runs the test suite and fixes failures.                                                                |
| **reviewer**  | Signs off (`REVIEWER_COMPLETE`) or writes a `review.md` report, which automatically schedules another full iteration. Up to 3 failed-review loops per session before JFI stops so a bad cycle is visible instead of looping forever. |

The plan file is the single source of truth across phases *and* across restarts — every phase re-reads it, and resuming a session picks up at the first unticked item (see [Resuming](#resuming-a-session)).

### How one run actually flows

1. You type a **session name**, then your **goal** (be as detailed as you want).
2. The planner writes `plan.md`. As soon as its last line is written and the phase-complete marker is emitted, JFI moves on automatically — there is no approval gate between phases; the review loop *is* the quality gate.
3. During **imp** and **testing**, the model works strictly one checkbox at a time: it does the work, then ticks exactly that one box in `plan.md` (byte-identical text, only `[ ]`→`[x]`). This is what makes the header's live plan progress counter (`plan 7/14`) meaningful and makes resumption robust.
4. The **reviewer** reads the finished work; if it finds real issues it writes `review.md`, JFI deletes that file, folds its contents into a fresh user message, and loops all four phases again (the header shows `loop #2`, etc.). A clean review ends the run.
5. Once done — or if you queue more requests at any point (see [Live input](#live-input-queue-force-idle)) — JFI either idles with a live input line waiting for your next request, or exits.

The whole transcript is also written to `.JFI/readme/<session>/run.log` as it happens (`tail -f` friendly), and every file the agent touches is tracked so a project-state summary can be folded into later iterations.

---

## Quick start — `JFI` launcher (interactive)

```bash
./JFI                      # uses the bundled .venv if present, else python3
```

The launcher:
1. Ensures Python ≥ 3.10 is available (bundled venv → active venv → system `python3`).
2. Creates `.venv` and installs dependencies on first run (or after a dependency change).
3. Verifies the project's `.env` exists — if not, it copies the bundled `JFI_ENV_TEMPLATE` into it so you only have to fill in your own values.

Then answer two prompts: **session name** and **goal**, and let it work.

### Configuration (`.env`)

| Variable            | Purpose                                            | Example value                              |
|---------------------|----------------------------------------------------|--------------------------------------------|
| `OPENAI_URL`        | Base URL of any OpenAI-compatible chat API         | `http://127.0.0.1:1234/v1` (Ollama) or `https://api.openai.com/v1` |
| `OPENAI_API_KEY`    | API key for that server                            | `ollama` / your real key                   |
| `MODEL`             | Model name                                         | `gemma-4:31b`, `qwen3.5:35b-a3b`, …        |
| `TEMPERATURE`       | Sampling temperature                               | `0.7`                                      |
| `CONTEXT_SIZE` *(optional)* | Context window of the served model, in tokens — history is compressed once a request would exceed `CONTEXT_SIZE × CONTEXT_COMPRESSION_RATIO`. Size it to what *fits on your machine*: 4096–16384 for an ~8GB setup with a small model, 32768+ for 27B/31B runs. Defaults to 32768 if unset. | `32768`              |
| `RESPONSE_MAX_TOKEN` *(optional)* | Cap on a *single response's* generated tokens. A smaller model occasionally loops instead of finishing a turn (repeating itself, or generating an oversized tool call) — once a response's running estimate crosses this, JFI aborts that response and re-sends the same turn rather than letting it run away. Defaults to 10000 if unset. | `10000` |
| `THEME` *(optional)*| Live-console color preset — see [Themes](#themes)  | `dark-ocean`, `light-paper`, or empty for auto-detect |

`.env` is loaded before anything else runs (see `runner.py::main()`), so a `THEME=...` line in it takes effect no matter how JFI was launched.

### Resuming a session

Rerun with the **same session name**: JFI detects `.JFI/readme/<session>/history.json`, skips every phase that already completed, and continues from the first unfinished one — including mid-phase, since the plan file's checkboxes are re-read rather than assumed to match the old transcript.

---

## Running locally on GPU

JFI talks to any OpenAI-compatible `/v1/chat/completions` endpoint, so a local model is all you need:

```bash
# Ollama (easiest)
ollama serve &                                  # listens on http://127.0.0.1:11434
# or llama.cpp server
./server -m model.gguf --port 8080              # OpenAI-compatible at /v1
# or vLLM for larger models
vllm serve <model> --port 8000                   # OpenAI-compatible at /v1
```

Then point the `.env` at it:

| Server        | `OPENAI_URL`                    | `MODEL`                     | `OPENAI_API_KEY`      |
|---------------|---------------------------------|-----------------------------|-----------------------|
| Ollama        | `http://127.0.0.1:11434/v1`     | e.g. `qwen3:8b`             | anything (`ollama`)   |
| llama.cpp     | `http://127.0.0.1:8080/v1`      | whatever you loaded         | anything (e.g. `llama`) |
| vLLM / others | that server's `/v1` URL         | the served model id         | real key if required  |

**Model size vs. your machine:**

- **~8GB VRAM/RAM** — comfortable territory for ~4B–8B quantized models (e.g. `qwen3:8b`, `llama3.1:8b` at Q4/Q5). Set `CONTEXT_SIZE` to what the server actually serves (typically 4096–16384); JFI's history compression (`CONTEXT_COMPRESSION_RATIO=0.7`) keeps requests under budget automatically, but small windows mean more frequent compression and a shorter effective memory for long tasks.
- **27B / ~31B models** — the sweet spot JFI is tuned for (e.g. `gemma-4:31b`, Qwen3.5-class 27–35B, GLM air-class): use Q4 quantization with a large context window (`CONTEXT_SIZE=32768` or more) and expect long runs — that's the token-burn trade paying off.
- Plans are in place to push JFI down as low as **8GB total** (model + overhead), i.e. usable on a modest laptop GPU; until then, treat ~4B–8B quantized models as the practical floor for quality results.

---

## Themes

The live console supports ten named presets, selectable via `THEME=` in `.env`. Every preset but `dark-default` also paints the terminal's actual background — not just the message text — so it looks right regardless of what your terminal profile's own background happens to be:

| Preset                 | Look                                                                  |
|------------------------|------------------------------------------------------------------------|
| `dark-default`         | ANSI-color baseline — inherits your terminal's own palette and background. |
| `dark-ocean`           | Fixed blue/teal-on-dark, deep navy background.                       |
| `dark-mono`            | Pure grayscale on near-black.                                        |
| `light-default`        | Classic black/blue/green on white.                                    |
| `light-sunrise`        | Warm magenta/maroon on a cream background.                            |
| `light-paper`          | Soft muted green/blue, "paper" feel — the most contrast-friendly light option. |
| `catppuccin-mocha`     | [Catppuccin](https://github.com/catppuccin/catppuccin)'s flagship dark flavor. |
| `catppuccin-macchiato` | Catppuccin, one step lighter than Mocha.                               |
| `catppuccin-frappe`    | Catppuccin's softest dark flavor.                                      |
| `catppuccin-latte`     | Catppuccin's light flavor.                                             |

Leave `THEME` unset (or set to `auto`) and JFI detects your terminal's background via the standard `COLORFGBG` environment variable and picks `dark-default` or `light-default` accordingly. An explicit value always wins; an unknown name falls back to auto-detection with a hint, so a typo never crashes startup.

Live screenshots of each preset: [docs/Themes.md](docs/Themes.md).

---

## Live input — queue · force · idle

The bottom line is *always* live while JFI works (the pipeline runs on its own thread):

| What you type       | Effect                                                                                                    |
|---------------------|------------------------------------------------------------------------------------------------------------|
| `text` + Enter      | **Queued** for the next iteration, replayed automatically once the current review phase lands.              |
| `!text` + Enter     | **Forced** — injected into the AI's very next turn, mid-phase.                                              |
| bare `!` + Enter    | Promotes *everything* already queued into the current turn at once.                                         |

When a run finishes with an empty queue, JFI doesn't exit — it idles with the input line live (`PIPELINE COMPLETE — IDLE`), so feeding it more work is optional and stateful (same session, same history). Multi-line input uses Shift+Enter or Alt+Enter; Enter submits.

**Ctrl+K** skips the current checklist item outright, during **imp** or **testing** — no need to convince the model to move on. It marks that one line `- [○] N.M ...` in the plan instead of `- [x]` (a third state: skipped by you, not done by the model) and tells the model to continue with the next item. A skipped item counts toward the header's plan progress the same as a ticked one, and is never reopened, redone, or flagged as a defect by a later reviewer pass. Planner and reviewer have no per-item checklist, so Ctrl+K does nothing there.

---

## Command approval

Every `execute_command` call is gated behind a human before it runs. When the model wants to run a shell command, a menu appears pinned to the bottom status bar — move the highlighted option with **←/→** or **↑/↓**, confirm with **Enter** (typing a key or the full label and hitting Enter jumps straight to it, no arrowing needed):

| Option | Effect |
|--------|--------|
| **Yes, run once** | Runs this one command; the next command asks again. |
| **Yes, save & run** | Runs it, and remembers its leading word (`npm`, `git`, …) in this session's `context.json` under `"approved-cmd"` — any future command starting with that prefix skips the prompt, including after resuming the session later. |
| **Yes, for the rest of this session** | Runs it, and stops asking for every command until this process exits (in-memory only — not persisted, so a fresh run asks again). |
| **No, don't run** | The command never executes; the model gets `"Command not executed (user answered No)"` back instead of a shell result. |

This lives in `src/JFI/tool/cmd_tools.py` (`CmdApprovalGate`) and `AbstractManager.get_user_choice` — any console implementation gets at least the free-text fallback (type the key or the full label), and `PromptToolkitConsoleManager` overrides it with the arrow-key menu described above.

---

## `uv run build` — standalone binary

Builds a single native `jfi` executable with PyInstaller — the machine that runs it needs no Python install at all (unlike `./JFI`, which still needs a system Python to bootstrap its own venv on first run).

```bash
uv sync --group dev   # pulls in pyinstaller
uv run build          # -> dist/jfi
```

The binary is a onefile build of `src/JFI/runner.py`, bundling prompt_toolkit, the openai client, and every other dependency. It still reads `.env` from its current working directory at startup, same as `./JFI`. Build logic lives in `src/build_binary/__init__.py`.

---

## Project layout

```
Just-Finish-It/
├── JFI                          # bash launcher (venv bootstrap + .env check)
├── .python-version              # 3.10 — matches pyproject.toml's requires-python floor
├── pyproject.toml               # package metadata, deps (pytest/pyinstaller for dev), pytest config
├── JFI_ENV_TEMPLATE             # copied to ./.env on first run if missing
├── README.md                    # this file
│
├── src/JFI/                     # the agent itself
│   ├── runner.py                # main() + run_pipeline(): orchestrates the 4 phases, review loop, queue/force/idle
│   │                            #   • execute_tool_call() — runs one tool call, coaches on failure (AUTO-RECTIFY), tracks per-signature retry counts
│   │                            #   • collect_next_iteration() / review_outcome() — the no-prompt loop back to planner after a failed review or queued request
│   ├── llm/
│   │   ├── base_llm_stream.py   # BaseLLMStream: MODEL/TEMPERATURE from .env, abstract send_message/close
│   │   └── openai_compatable_stream.py# OpenAICompatableStream: any OpenAI-compatible endpoint (Ollama, vLLM, LM Studio, …)
│   ├── session/
│   │   └── simple_session_manager.py  # the heart of statefulness: history.json (append-only), plan.md parsing
│   │                                  #   • phase completion detection (per-phase COMPLETE markers + checkbox tracking)
│   │                                  #   • context-window budgeting & automatic history compression when a request would overflow it
│   │                                  #   • token_usage() / estimate_request_tokens() feed the header's live ctx % and ↓/↑ read/write counters
│   ├── manager/
│   │   ├── abstract_manager.py  # AbstractManager: the console API contract (display_*, get_user_input, get_user_choice, print_agent_response, safe_get_user_input)
│   │   ├── pt_console_manager.py# PromptToolkitConsoleManager: full-screen UI — header/task/status lines, streaming AI space,
│   │   │                        #   always-live input line (queue/force/answer), arrow-key choice menu, scroll lock, theme presets,
│   │   │                        #   live run.log mirroring, clear_console() on exit
│   │   └── key_bindings.py      # teaches the terminal Shift+Enter/Ctrl+Enter encodings so multiline input works everywhere
│   ├── orchestrator/
│   │   └── basic_orchestrator.py# legacy single-turn orchestrator (kept for reference/testing; runner.py is what JFI actually runs)
│   └── tool/
│       ├── schemas.py           # AVAILABLE_TOOLS: the JSON-schema tool definitions sent to every LLM request
│       ├── file_tools.py        # write_file / read_file / append_to_file / replace_in_file (the exact functions documented in each phase prompt)
│       ├── cmd_tools.py         # execute_command, gated by CmdApprovalGate — see [Command approval](#command-approval)
│       └── image_tools.py       # capture_screenshot / view_image — the one tool pair that returns an image to the model, not just text
│
├── src/build_binary/             # `uv run build` — PyInstaller onefile packaging of src/JFI/runner.py
│   └── __init__.py
│
├── test/                        # pytest suite (see "Tests" below) — unit tests per module plus the plan-file protocol
└── docs/
    ├── Themes.md                # live screenshots of all six theme presets
    └── images/theme-*.png       # those screenshots, generated by docs/make_theme_screenshots.py
```

---

## The tool set (what the model can call)

| Tool               | Purpose                                                                                          |
|--------------------|--------------------------------------------------------------------------------------------------|
| `write_file`       | Create/overwrite a file.                                                                         |
| `read_file`        | Read an existing file's content.                                                                 |
| `append_to_file`   | Append to a file — the sanctioned way to build long documents in chunks instead of one oversized write. |
| `replace_in_file`  | Replace exactly one substring, leaving everything else untouched. This is *the* mechanism for ticking plan checkboxes and making surgical edits; a bad match (0 or >1 hits) errors out rather than corrupting the file. |
| `execute_command`  | Run any shell command; returns stdout+stderr. Gated behind a human approval prompt first — see [Command approval](#command-approval). |
| `capture_screenshot` | Snapshot the monitor to disk — used by the testing phase for visual verification where possible (fails cleanly with a "skip this step" hint if there's no display). |
| `view_image`       | Attach an image file into the model's next turn (the only tool whose result becomes an actual image message, not just text) — this is how the agent can actually *see* screenshots it captured. |

Every failed call gets a concrete `AUTO-RECTIFY:` instruction naming the exact tool/argument to change, and after 3 identical failures JFI tells the model to abandon that approach rather than loop — the difference between "retry forever" and "fix or move on."

---

## Tests

```bash
pytest            # full suite (config in pyproject.toml: testpaths = ["test"])
```

Highlights:

- `test_phase_messages.py` / `test_phase_completion.py` — pin the exact phase-trigger prompts and their completion-marker detection.
- `test_plan_parsing.py` / `test_plan_location.py` — the `- [ ] N.M` protocol: parsing, ticking, and that the plan file always lands at `.JFI/readme/plan.md`.
- `test_session_manager.py` — history append/resume semantics, context-budget compression.
- `test_pt_line_count.py` — the live console's scroll/cursor math (the exact rendering invariant described in [Project layout](#project-layout)).
- `test_safe_get_user_input.py` — input-prompt retry behavior.
- `test_review_outcome.py` — the review-loop decision logic (pass/fail, iteration re-scheduling).
- `test_cmd_approval_gate.py` / `test_cmd_prefix_matching.py` / `test_cmd_context_cache.py` — the Y/S/A/N approval gate, its persisted-prefix matching, and the shared context.json cache.
- `test_choice_menu.py` — the arrow-key menu's state machine (movement, typed-answer matching, confirm) and the free-text fallback for managers without one.

---

## TODO

- [ ] **Evaluate JFI on a concrete real-world task** with a ~31B local model: pick one representative coding task (not a toy example), run it fully through the pipeline, and record — completion or not, final code quality vs. what a frontier agent would likely produce for the same prompt, total tokens consumed, wall-clock time, and any phase that needed a forced/queued nudge to keep moving. This turns the "small models can finish it too" claim from real usage and anecdotes into numbers.
