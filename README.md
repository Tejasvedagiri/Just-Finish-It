# Just Finish It (JFI) 🤖

**A multi-phase, plan-driven coding agent for local LLMs.** Give JFI a goal and it runs the full software-engineering loop — **planner → implementer → tester → reviewer** — end to end against any OpenAI-compatible endpoint, one checkbox at a time.

What makes it different from "just an autonomous mode" is that all of its state lives on disk:

- A **persisted plan file** (`JFI/readme/plan.md`) — a strict, recursively-broken-down *tree*, where every task is split into the smallest doable pieces and only the leaves (the ones not split further) carry a `- [ ] N.M.…` / `- [x] N.M.…` checkbox; parents stay plain, checkbox-less bullets. The single source of truth for what's done, re-read by every phase and every restart.
- A **context cache** (a small JSON fact store) that survives turns and phases, so key decisions and discovered details don't have to be re-derived after history compression.
- An append-only session transcript (`history.json`) plus a live `run.log`, which is what makes resuming mid-session — even mid-phase — reliable.

JFI is token-hungry by design: every step re-sends the (growing) session history so a mid-sized model can always see its own progress and stay on track. In exchange it runs well on ~31B-parameter models, producing results close to frontier-class agents at a fraction of the cost — as long as you give the model enough context window and patience.

---

## The four-phase pipeline

Every JFI session runs the same four phases in order:

| Phase     | What it does                                                                                          |
|-----------|--------------------------------------------------------------------------------------------------------|
| **planner**   | Writes a step-by-step plan to `JFI/readme/plan.md` as a tree, recursing each task into the smallest doable pieces — only the leaves get a `- [ ] N.M.…` checkbox. |
| **imp**       | Implements the code, ticking each box with `replace_in_file` *immediately* after finishing that one item — never batched. |
| **testing**   | Runs the test suite and fixes failures.                                                                |
| **reviewer**  | Signs off (`REVIEWER_COMPLETE`) or writes a `review.md` report, which automatically schedules another full iteration. Up to 3 failed-review loops per session before JFI stops so a bad cycle is visible instead of looping forever. |

The plan file is the single source of truth across phases *and* across restarts — every phase re-reads it, and resuming a session picks up at the first unticked item (see [Resuming](#resuming-a-session)).

### How one run actually flows

1. You type a **session name**, then your **goal** (be as detailed as you want).
2. The planner writes `plan.md`. As soon as its last line is written and the phase-complete marker is emitted, JFI moves on automatically — there is no approval gate between phases; the review loop *is* the quality gate.
3. During **imp** and **testing**, the model works strictly one checkbox at a time: it does the work, then ticks exactly that one box in `plan.md` (byte-identical text, only `[ ]`→`[x]`). This is what makes the header's live progress bars — the current phase's own checklist (`implement ████░░░░ 8/18`) alongside the whole plan (`total ███░░░░░ 8/21`) — meaningful and makes resumption robust.
4. The **reviewer** reads the finished work; if it finds real issues it writes `review.md`, JFI deletes that file, folds its contents into a fresh user message, and loops all four phases again (the header shows `loop #2`, etc.). A clean review ends the run.
5. Once done — or if you queue more requests at any point (see [Live input](#live-input-queue-force-idle)) — JFI either idles with a live input line waiting for your next request, or exits.

The whole transcript is also written to `JFI/readme/<session>/run.log` as it happens (`tail -f` friendly), and every file the agent touches is tracked so a project-state summary can be folded into later iterations.

The terminal's own tab/window title tracks the same thing (session name plus live progress, e.g. `story · Implement 8/18`) so you can tell which run needs attention without switching to it — most terminals show this in the tab bar.

---

## Quick start — `JFI` launcher (interactive)

```bash
./JFI                      # uses the bundled .venv if present, else python3
```

The launcher:
1. Ensures Python ≥ 3.12 is available (bundled venv → active venv → system `python3`).
2. Creates `.venv` and installs dependencies on first run (or after a dependency change).
3. Verifies the project's `.env` exists — if not, it copies the bundled `JFI_ENV_TEMPLATE` into it so you only have to fill in your own values.

Then answer two prompts: **session name** and **goal**, and let it work.

`./JFI --version` (or `jfi --version` once installed) prints the installed version and exits — useful for confirming which build you're actually running (e.g. after rebuilding `dist/jfi`) without launching a real session. `./JFI --help` lists every flag.

### Configuration (`.env`)

| Variable            | Purpose                                            | Example value                              |
|---------------------|----------------------------------------------------|--------------------------------------------|
| `OPENAI_URL`        | Base URL of any OpenAI-compatible chat API         | `http://127.0.0.1:1234/v1` (Ollama) or `https://api.openai.com/v1` |
| `OPENAI_API_KEY`    | API key for that server                            | `ollama` / your real key                   |
| `MODEL`             | Model name                                         | `gemma-4:31b`, `qwen3.5:35b-a3b`, …        |
| `TEMPERATURE`       | Sampling temperature                               | `0.7`                                      |
| `FREQUENCY_PENALTY` *(optional)* | Penalizes tokens proportional to how often they've already appeared in the response so far — the standard lever against a model falling into a verbatim repetition loop (seen in practice on smaller/quantized models). Defaults to `0.0` (a no-op) if unset; try `0.3`–`0.5` if a model gets stuck repeating itself. | `0.3` |
| `CONTEXT_SIZE` *(optional)* | Context window of the served model, in tokens — history is compressed once a request would exceed `CONTEXT_SIZE × CONTEXT_COMPRESSION_RATIO`. Size it to what *fits on your machine*: 4096–16384 for an ~8GB setup with a small model, 32768+ for 27B/31B runs. Defaults to 32768 if unset. | `32768`              |
| `CONTEXT_COMPRESSION_RATIO` *(optional)* | Headroom left for the model's own reply when deciding whether to compress history. Defaults to `0.7` if unset. | `0.7` |
| `STREAM_OUTPUT_CAP` *(optional)* | Max tokens (estimated) for a single streamed response before it's abandoned as a runaway generation and retried as a fresh turn — see [When an LLM request fails](#when-an-llm-request-fails). Defaults to `10000` if unset. | `10000` |
| `THEME` *(optional)*| Live-console color preset — see [Themes](#themes)  | `dark-ocean`, `light-paper`, or empty for auto-detect |
| `SHOW_STREAM_PROMPTS` *(optional, debug)* | Prints every message sent to the LLM each turn, in full — no truncation anywhere, unlike the normal tool-result preview. Verbose by design; meant for prompt-engineering and context-compression debugging, not everyday runs. Accepts `1`/`true`/`yes`/`on`. | `1` |
| `SESSION_PATH` *(optional, advanced)* | Where the `JFI/` session folder is created, relative to. Defaults to the current working directory. | `.` |

Every one of `OPENAI_URL` / `OPENAI_API_KEY` / `MODEL` / `TEMPERATURE` / `FREQUENCY_PENALTY` can also be set **per phase**, prefixed `PLANNER_`, `IMP_`, `TESTING_`, or `REVIEWER_` (e.g. `REVIEWER_MODEL=gpt-4.1`, `IMP_OPENAI_URL=http://127.0.0.1:8080/v1`). A phase with no prefixed override falls back to the shared, unprefixed setting — so the default (unset) behavior is exactly one model for every phase, and you only add prefixed lines for the phases you actually want to route elsewhere (e.g. a cheap/fast model for `testing`, a stronger one for `reviewer`).

`.env` is loaded before anything else runs (see `runner.py::main()`), so a `THEME=...` line in it takes effect no matter how JFI was launched.

### Resuming a session

Rerun with the **same session name**: JFI detects `JFI/readme/<session>/history.json`, skips every phase that already completed, and continues from the first unfinished one — including mid-phase, since the plan file's checkboxes are re-read rather than assumed to match the old transcript.

Each session locks its own folder while it's running (a `.lock` file under `JFI/<session>/`), so starting the **same** session name in a second terminal is refused with a clear message instead of racing on `history.jsonl.gz`/`plan.md`/`context.json` — just pick a different name in the prompt to try again. The lock is released automatically on exit (even a crash) and never lingers as a stale file to clean up.

### When an LLM request fails

A transient failure (a dropped connection, a 5xx from the server, or a single response that outgrows `STREAM_OUTPUT_CAP` — a runaway/looping generation, abandoned mid-stream) is retried automatically a few times with a short delay. If those retries run out — or the failure wasn't the transient kind to begin with (a bad request, an auth error, ...) — JFI does **not** end the run on its own. It asks: **Retry now**, or **Stop (progress is saved)**. Pick Retry as many times as you need (fix the server, swap `.env` values, whatever it takes) and the same turn just tries again; only an explicit Stop — from that menu or Ctrl+C — actually ends the run.

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

The live console supports twenty named presets, selectable via `THEME=` in `.env`. Every preset but `dark-default` also paints the terminal's actual background — not just the message text — so it looks right regardless of what your terminal profile's own background happens to be:

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
| `tokyo-night`          | [Tokyo Night](https://github.com/enkia/tokyo-night-vscode-theme) — blue/green/violet on deep indigo. |
| `dracula`              | [Dracula](https://draculatheme.com/) — cyan/green/pink on its classic near-black purple. |
| `nord`                 | [Nord](https://www.nordtheme.com/) — arctic blue-gray with muted frost accents. |
| `gruvbox-dark`         | [Gruvbox](https://github.com/morhetz/gruvbox) — warm retro-contrast on soft black. |
| `solarized-dark`       | [Solarized](https://ethanschoonover.com/solarized/) Dark — the precision-tuned classic. |
| `solarized-light`      | Solarized Light — same accents on Solarized's signature cream base.  |
| `rose-pine`            | [Rosé Pine](https://rosepinetheme.com/) — muted foam/pine/iris on soft plum-black. |
| `rose-pine-dawn`       | Rosé Pine's light companion flavor, warm cream base.                  |
| `one-dark`             | [One Dark](https://github.com/atom/atom/tree/master/packages/one-dark-ui) — Atom's iconic blue/green/purple on slate. |
| `everforest-dark`      | [Everforest](https://github.com/sainnhe/everforest) — soft nature-toned greens on muted forest-green. |

Leave `THEME` unset (or set to `auto`) and JFI detects your terminal's background via the standard `COLORFGBG` environment variable and picks `dark-default` or `light-default` accordingly. An explicit value always wins; an unknown name falls back to auto-detection with a hint, so a typo never crashes startup.

Want your own colors instead? Set `THEME` to a JSON object (wrapped in single quotes in `.env`) instead of a name, e.g. `THEME='{"": "bg:#112233 fg:#eee", "out.user": "bold #ff8800"}'` — any subset of style classes may be set, and invalid JSON or an invalid style both fall back safely rather than crashing. See [Custom themes](docs/Themes.md#custom-themes-theme-as-json) for the full syntax.

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

### Keyboard shortcuts

| Key      | When it works                  | Effect                                                                          |
|----------|---------------------------------|----------------------------------------------------------------------------------|
| `Ctrl+K` | While a phase is running        | Skips the current checklist item outright — marks it `- [○]` in the plan directly (imp/testing only). |
| `Ctrl+Q` | While a phase is running        | Skips every remaining item in the current phase's checklist, then wraps the phase up. |
| `Ctrl+P` | While a phase is running        | Pauses the pipeline — holds before the next turn so an in-flight call finishes first. Press again to resume. |
| `Ctrl+N` | Idle (`PIPELINE COMPLETE`) only | Starts a brand-new session from scratch (fresh name + goal), without leaving the console. |
| `Ctrl+C` | Anytime                         | Requests a stop; state is saved, so rerunning with the same session name resumes. |

A skipped item (`- [○]`) is treated exactly like a finished one everywhere progress is counted — it just means a human decided it was done with, not the model.

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
├── .python-version              # 3.12 — matches pyproject.toml's requires-python floor
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
│   │   ├── abstract_manager.py  # AbstractManager: the console API contract (display_*, get_user_input, print_agent_response, safe_get_user_input)
│   │   ├── pt_console_manager.py# PromptToolkitConsoleManager: full-screen UI — header/task/status lines, streaming AI space,
│   │   │                        #   always-live input line (queue/force/answer), scroll lock, theme presets, live run.log mirroring
│   │   └── key_bindings.py      # teaches the terminal Shift+Enter/Ctrl+Enter encodings so multiline input works everywhere
│   ├── orchestrator/
│   │   └── basic_orchestrator.py# legacy single-turn orchestrator (kept for reference/testing; runner.py is what JFI actually runs)
│   └── tool/
│       ├── schemas.py           # AVAILABLE_TOOLS: the JSON-schema tool definitions sent to every LLM request
│       ├── file_tools.py        # write_file / read_file / append_to_file / replace_in_file (the exact functions documented in each phase prompt)
│       ├── cmd_tools.py         # execute_command, gated by CmdApprovalGate — see [Command approval](#command-approval)
│       ├── context_tools.py     # context_save / context_lookup — the model's fact store, see [Context cache](#context-cache)
│       ├── image_tools.py       # capture_screenshot / view_image — the one tool pair that returns an image to the model, not just text
│       ├── web_tools.py         # fetch_webpage_images — downloads a page's images to disk (view_image shows them, same as a screenshot)
│       └── llm_tools.py         # ask_llm — a stateless one-off LLM call for text work with no dedicated tool
│
├── src/build_binary/             # `uv run build` — PyInstaller onefile packaging of src/JFI/runner.py
│   └── __init__.py
│
├── test/                        # pytest suite (see "Tests" below) — unit tests per module plus the plan-file protocol
├── utils/                       # dev-only scripts, not part of the shipped package
│   ├── capture_theme_screenshots.py # real pty capture of the console under every THEME (proof images, or --docs for docs/images/)
│   └── _docs_frame_app.py       # no-LLM harness rendering one representative frame for the --docs capture
└── docs/
    ├── Themes.md                # live screenshots of all twenty theme presets, plus the custom-theme (THEME as JSON) docs
    └── images/theme-*.png       # those screenshots, captured from the real console by ../utils/capture_theme_screenshots.py --docs
```

---

## Context cache

Alongside the plan, each session keeps a small persistent fact store (`context.json`) for
things worth remembering that would otherwise be lost once older turns are compressed out
of context: key decisions, discovered schema/API/config details, gotchas — anything a later
phase or iteration would otherwise have to re-derive.

The model manages it with two dedicated tools rather than `read_file`/`write_file`:

- `context_save(key, value)` — merges one fact in with a single call, never disturbing any
  other key already there (including internal bookkeeping like approved `execute_command`
  prefixes — see [Command approval](#command-approval)).
- `context_lookup(keyword)` — searches instead of dumping the whole file. Called with no
  keyword it lists every saved key plus a short preview (a live-computed index, so it can
  never drift out of sync with the facts themselves); called with a keyword it returns the
  full text of just what matches, case-insensitively, against keys and values.

It's meant to stay small — a handful of high-value facts, not a transcript.

---

## The tool set (what the model can call)

| Tool               | Purpose                                                                                          |
|--------------------|--------------------------------------------------------------------------------------------------|
| `write_file`       | Create/overwrite a file.                                                                         |
| `read_file`        | Read an existing file's content.                                                                 |
| `append_to_file`   | Append to a file — the sanctioned way to build long documents in chunks instead of one oversized write. |
| `replace_in_file`  | Replace exactly one substring, leaving everything else untouched. This is *the* mechanism for ticking plan checkboxes and making surgical edits; a bad match (0 or >1 hits) errors out rather than corrupting the file. |
| `execute_command`  | Run any shell command; returns stdout+stderr. Times out after 300s by default — pass `timeout` to raise it for a slow install/build/test step.                                                    |
| `context_save`     | Save one fact to the persistent [context cache](#context-cache) in a single call — merges it in without touching any other key. |
| `context_lookup`   | Search the context cache instead of reading it wholesale — call with no keyword to list every saved key, or a keyword to get the full text of just what matches. |
| `capture_screenshot` | Snapshot the monitor to disk — used by the testing phase for visual verification where possible (fails cleanly with a "skip this step" hint if there's no display). |
| `fetch_webpage_images` | Fetch a web page (http/https only) and download the images it references — Open Graph/Twitter preview image first, then every `<img>` tag — to disk, auto-numbered. Same "writes files, doesn't show you anything" design as `capture_screenshot`. |
| `view_image`       | Attach an image file into the model's next turn (the only tool whose result becomes an actual image message, not just text) — this is how the agent can actually *see* screenshots it captured or images `fetch_webpage_images` downloaded. |
| `ask_llm`          | A general-purpose escape hatch: sends `prompt` as a fresh, **stateless** single-turn LLM call (no tools, no conversation history, no plan/file access) and returns the reply. For one-off text work — a description, a clarification, a rephrase, brainstorming a name — that doesn't warrant its own dedicated tool. Uses whatever model the calling phase itself is configured for (see [per-phase models](#configuration-env) above); its cost still counts toward the header's cumulative ↓/↑ token totals, real usage if the server reports it, an estimate otherwise. |

Every failed call gets a concrete `AUTO-RECTIFY:` instruction naming the exact tool/argument to change, and after 3 identical failures JFI tells the model to abandon that approach rather than loop — the difference between "retry forever" and "fix or move on."

---

## Tests

```bash
pytest            # full suite (config in pyproject.toml: testpaths = ["test"])
```

Highlights:

- `test_phase_messages.py` / `test_phase_completion.py` — pin the exact phase-trigger prompts and their completion-marker detection.
- `test_plan_parsing.py` / `test_plan_location.py` — the `- [ ] N.M` protocol: parsing, ticking, and that the plan file always lands at `JFI/readme/plan.md`.
- `test_session_manager.py` — history append/resume semantics, context-budget compression.
- `test_pt_line_count.py` — the live console's scroll/cursor math (the exact rendering invariant described in [Project layout](#project-layout)).
- `test_safe_get_user_input.py` — input-prompt retry behavior.
- `test_review_outcome.py` — the review-loop decision logic (pass/fail, iteration re-scheduling).

## Linting

```bash
uv run ruff check .   # config in pyproject.toml: [tool.ruff]
```

Scoped to Pyflakes plus pycodestyle's error-level checks (unused imports/variables, undefined names, syntax-adjacent mistakes) — real bugs, not a style ruleset, so it stays quiet on an already-written codebase's own conventions (prose-heavy comments, its existing line lengths, import ordering, ...).

---

## TODO

- [ ] **Evaluate JFI on a concrete real-world task** with a ~31B local model: pick one representative coding task (not a toy example), run it fully through the pipeline, and record — completion or not, final code quality vs. what a frontier agent would likely produce for the same prompt, total tokens consumed, wall-clock time, and any phase where the model got stuck or needed a forced/queued nudge. This is the honest "is this actually worth the token burn" check the project currently only has anecdotes for.
