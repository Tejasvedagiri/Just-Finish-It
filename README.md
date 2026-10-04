# Just Finish It (JFI) 🤖

**A plan-driven coding agent for local LLMs.** Give JFI a goal and it runs **planner → implementation → reviewer → cleanup** end to end against any OpenAI-compatible endpoint, one small, scoped conversation at a time.

All of its state lives in one SQLite database per project (`.jfi/JFI.db`):

- **The plan**: a tree of nodes. Every node is judged GOOD (small enough to build), BREAKDOWN (split it further) or REDO (rewrite it), so only genuinely small leaves reach implementation.
- **A runbook and a design**: how to set up, run and test the project, and the contracts between its parts, written by the planner and read by every later step.
- **A context cache**: facts one episode saves for later ones (`context_save` / `context_lookup`).
- **The history** of every conversation, which is what makes resuming mid-session reliable.

Each step gets a short, focused conversation (an *episode*) with only the context it needs, instead of one ever-growing transcript. That keeps a ~27–31B local model on track.

**Watch runs live:** the built-in `jfi-web` dashboard for one session, or **[Just-Finish-It-Fleet](https://github.com/Tejasvedagiri/Just-Finish-It-Fleet)** — a separate repo with a web dashboard for every JFI session on every machine (plan, Task | Judge table, runbook, log, pause/stop/queue).

---

## The pipeline

Every JFI session runs four phases in order:

| Phase     | What it does |
|-----------|--------------|
| **planner** | Architect → Lead → Task, each in short episodes. The Architect maps the project (components, runbook, design contracts) and plans the app's entry point (the runbook's `entry`, e.g. `src/main.js`) as its own component; Leads break each component into files; Tasks break files into functions. Every node carries notes (what to build and why) and references for the next layer. After each layer a **judge** labels every node GOOD or BREAKDOWN: a fixed rule, plus with `LAYA=1` a [Laya](https://github.com/NandhaKishorM/laya) score that asks whether the node's task can be solved within 20k tokens (see [Configuration](#configuration-env)). |
| **imp** | One short **Dev** episode per GOOD leaf, in dependency order: implement the one function (or change) and its unit test, then `mark_leaf_done`, which is refused until the test passes. Each passing leaf is committed as a git checkpoint in `.jfi/checkpoints.git` (your own git is never touched). Problems worth flagging go to the reviewer as notes. |
| **reviewer** | Re-runs the project's checks itself (the runbook's end-to-end check; for a web app it also loads the page with `check_page` and clicks through the goal's flows with `browser`), reads the Dev notes, and signs off, reopens the leaf that owns a bug (`leaf_diff` shows which leaf changed what), or writes a review report for missing work, which schedules another iteration. Up to 3 failed reviews per session before JFI stops. |
| **cleanup** | Tidies the working directory: moves anything worth keeping into `.jfi/` and deletes the rest. Never touches the deliverable or `.jfi/` itself. |

### How one run actually flows

1. You type a **session name**, then your **goal** (be as detailed as you want).
2. The **planner** builds and judges the tree. There is no approval gate between phases; the judge and the review loop are the quality gates.
3. **imp** works through the leaves one episode at a time. The header's progress bars (`implement ████░░░░ 8/18`) count finished leaves.
4. The **reviewer** checks the whole result. A failed review (or anything you queued meanwhile) becomes feedback for the next iteration: the planner adds nodes for it, leaving finished leaves alone, and the loop runs again (the header shows `loop #2`, etc.).
5. **cleanup** tidies up, then JFI idles with a live input line waiting for your next request, or exits.

A new session in the same project starts from the last session's runbook and its lasting design (stack, components, contracts, conventions), so it doesn't re-discover how to set up, run and test the project; the Architect keeps what still fits the new goal.

Every console line is also logged to the database as it happens (`uv run export-db` dumps it, and `jfi-web` shows it live).

The terminal's own tab/window title tracks the same thing (session name plus live progress, e.g. `story · Implement 8/18`) so you can tell which run needs attention without switching to it.

Sessions created by JFI's old v1 pipeline (tiered planner, Program Manager, testing phase) can't be resumed any more; `uv run export-db` still reads them.

---

## Getting started

### 1. What you need

- **Python 3.12+** and **[uv](https://docs.astral.sh/uv/)** (`pip install uv`, or the installer on uv's site).
- **git**.
- **An LLM.** Any OpenAI-compatible server — [LM Studio](https://lmstudio.ai/), [Ollama](https://ollama.com/), llama.cpp or vLLM for a local model (a ~27–31B model with a 32k+ context works well; see [Running locally on GPU](#running-locally-on-gpu)) — or a hosted API key (OpenAI, or Anthropic with the `anthropic` extra).
- *Optional:* Node.js 18+ for the fleet dashboard, [Just-Finish-It-Fleet](https://github.com/Tejasvedagiri/Just-Finish-It-Fleet).

### 2. Clone and install

```bash
git clone https://github.com/Tejasvedagiri/Just-Finish-It.git
cd Just-Finish-It
uv sync
```

`uv sync` creates `.venv` with everything JFI needs. Add extras for the optional parts, **all in one command** (a later `uv sync --extra X` drops any extra it doesn't name):

| Extra | Adds |
|-------|------|
| `web` | `jfi-web`, the Streamlit dashboard |
| `anthropic` | the Claude backend (`LLM_BACKEND=anthropic`) |
| `laya` | Laya's second score for the planner's judge (`LAYA=1`; pulls in torch, several GB) |
| `mysql` / `postgres` | a shared database instead of SQLite (`DB_BACKEND`) |

```bash
uv sync --extra web                         # the usual choice
uv sync --extra web --group dev             # + pytest, ruff and PyInstaller for development
```

### 3. Configure: `uv run create-env`

```bash
uv run create-env
```

In a terminal this walks you through every setting — the backend, key and model, the tuning knobs, the dashboards, the episode/planner/Dev limits, the judge, and the approval and debug flags.

It **measures instead of guessing**: your RAM and GPU memory (NVIDIA, AMD, Apple Silicon; Linux, macOS and Windows), what the model server has loaded and at what context (LM Studio, Ollama, llama.cpp, vLLM), and — with one short test request — the model's speed and that it can call tools. From that it computes `CONTEXT_SIZE`, `CONTEXT_COMPRESSION_RATIO`, the output caps, `LLM_REQUEST_TIMEOUT`, `TOOL_RESULT_MAX_TOKENS` and the Laya settings. Each prompt shows the computed value and why (plus your old value, if different); Enter accepts it. Then it checks Python, `uv`, and that the server answers — and warns if `.env` asks for more context than the server loaded. JFI repeats that last check every time a session starts. Design: [`docs/create_env_sizing.md`](docs/create_env_sizing.md).

- The first run writes `.env` in the JFI folder (from `JFI_ENV_TEMPLATE`).
- Later runs never overwrite it: they write `.env_v1`, `.env_v2`, … — rename the one you want to `.env`.
- **`CONTEXT_SIZE` must match what your server really loaded** (LM Studio's context length, for example). Every episode's token budget is `CONTEXT_SIZE × CONTEXT_COMPRESSION_RATIO`; a server that loaded less cuts replies off mid-answer.

Every variable is documented in [Configuration](#configuration-env) and in `JFI_ENV_TEMPLATE`.

### 4. Run it on your project

JFI works **in the folder you start it from** — that's the project it plans and builds, and where it keeps its state (`.jfi/JFI.db`). It reads the `.env` in that folder (or the nearest parent), so give your project one:

```bash
mkdir -p ~/code/my-app && cd ~/code/my-app
cp /path/to/Just-Finish-It/.env .
uv run --project /path/to/Just-Finish-It jfi
```

Answer two prompts — a **session name** and your **goal** (be as detailed as you like) — and let it work. The header shows the phase, the progress bars and the token use; see [Live input](#live-input--queue--force--idle) for pausing, queueing requests and stopping. `jfi --version` prints the version, `jfi --help` every flag.

**Resume** by starting it again in the same folder with the same session name: finished phases are skipped and the plan is re-read from the database, so it picks up where it stopped. Only one session runs per project folder at a time.

### 5. Watch it (optional)

- **`jfi-web`** — a local Streamlit dashboard: live status, the plan with the judge's Task | Judge table, the runbook and design, the log, and approvals. Set `JFI_WEB_BRIDGE=1` (and the `web` extra); JFI starts it on `JFI_WEB_PORT` (7777). See [Web dashboard](#web-dashboard-jfi-web).
- **The fleet dashboard, [Just-Finish-It-Fleet](https://github.com/Tejasvedagiri/Just-Finish-It-Fleet)** — one page for every session on every machine:

  ```bash
  git clone https://github.com/Tejasvedagiri/Just-Finish-It-Fleet.git
  cd Just-Finish-It-Fleet
  npm install && npm run build && npm run master   # http://localhost:8765
  ```

  Then set `MASTER_WS_URL=ws://<master-host>:8765/report` in your project's `.env`. See [Fleet dashboard](#fleet-dashboard-just-finish-it-fleet--standalone-node-master).
- **`uv run export-db [project-dir]`** — dumps a project's database to readable text files after the fact.

### 6. Build a standalone binary (optional)

```bash
uv sync --extra web --extra laya --group dev
uv run build --laya   # -> dist/jfi (one file; dist\\jfi.exe on Windows)
```

The output runs without Python or uv installed. It's a single file (`dist/jfi`, `dist\jfi.exe` on Windows) you can copy anywhere on its own. Put it on your `PATH`, then in your project folder (with its `.env`) just run `jfi`. Every build bundles the Streamlit dashboard and websockets for the fleet; the other extras go in only when you pass their flag: `--laya` (`LAYA=1`; torch + transformers, so the binary is several GB), `--anthropic`, `--mysql`, `--postgres`, or `--all`. The build stops with the `uv sync` command to run if one it needs isn't synced. See [`uv run build`](#uv-run-build--standalone-binary).

### Configuration (`.env`)

| Variable            | Purpose                                            | Example value                              |
|---------------------|----------------------------------------------------|--------------------------------------------|
| `LLM_BACKEND` *(optional)* | Which `BaseLLMStream` implementation actually serves requests (see `src/JFI/llm/backend_select.py`). Unset/`openai` talks to whatever OpenAI-compatible endpoint `OPENAI_URL` points at (the default, works for Ollama/llama.cpp/LM Studio/vLLM/real OpenAI alike). `ollama`, `llamacpp` (also accepted: `llama.cpp`, `llama-cpp`) and `lmstudio` (also accepted: `lm-studio`, `lm studio`) are pure convenience — they just fill in `OPENAI_URL`/`OPENAI_API_KEY` with that server's usual localhost defaults *if you haven't already set them yourself*, then still go through the OpenAI-compatible path. `anthropic` (also accepted: `claude`) instead talks to Claude's own native Messages API directly via `AnthropicStream`, which needs `ANTHROPIC_API_KEY` set (not `OPENAI_API_KEY`) — install it with `uv sync --extra anthropic`. Can be overridden per phase like `MODEL` (e.g. `REVIEWER_LLM_BACKEND=anthropic` with its own `REVIEWER_ANTHROPIC_API_KEY`), so different phases can even run on entirely different backends in the same session. An unrecognized value logs a warning and falls back to plain `openai`. | `anthropic` |
| `ANTHROPIC_API_KEY` *(required only when `LLM_BACKEND=anthropic`/`claude`)* | Claude API key, used instead of `OPENAI_API_KEY` for that backend. | `sk-ant-...` |
| `ANTHROPIC_MAX_TOKENS` *(optional)* | Max output tokens per request on the Anthropic backend — this API requires an explicit cap, unlike most OpenAI-compatible servers. Defaults to `8192` if unset. | `8192` |
| `OPENAI_URL`        | Base URL of any OpenAI-compatible chat API         | `http://127.0.0.1:1234/v1` (Ollama) or `https://api.openai.com/v1` |
| `OPENAI_API_KEY`    | API key for that server                            | `ollama` / your real key                   |
| `MODEL`             | Model name — the real model id for the backend you picked (a served Ollama/llama.cpp model name, an OpenAI model id, or a Claude model id like `claude-sonnet-5` when `LLM_BACKEND=anthropic`) | `gemma-4:31b`, `qwen3.5:35b-a3b`, `claude-sonnet-5`, …        |
| `TEMPERATURE`       | Sampling temperature                               | `0.7`                                      |
| `FREQUENCY_PENALTY` *(optional)* | Penalizes tokens proportional to how often they've already appeared in the response so far — the standard lever against a model falling into a verbatim repetition loop (seen in practice on smaller/quantized models). Defaults to `0.0` (a no-op) if unset; try `0.3`–`0.5` if a model gets stuck repeating itself. | `0.3` |
| `CONTEXT_SIZE` *(optional)* | Context window of the served model, in tokens. It sets every episode's token budget: `CONTEXT_SIZE × CONTEXT_COMPRESSION_RATIO` (the rest is room for the reply), so it must match what your server actually loaded. Size it to what *fits on your machine*: 4096–16384 for an ~8GB setup with a small model, 32768+ for 27B/31B runs. Defaults to 32768 if unset. | `32768`              |
| `CONTEXT_COMPRESSION_RATIO` *(optional)* | The share of `CONTEXT_SIZE` an episode may fill; the rest is headroom for the model's own reply. Defaults to `0.7` if unset. | `0.7` |
| `STREAM_OUTPUT_CAP` *(optional)* | Max tokens (estimated) for a single streamed response before it's abandoned as a runaway generation and retried as a fresh turn — see [When an LLM request fails](#when-an-llm-request-fails). Defaults to `10000` if unset. | `10000` |
| `REASONING_OUTPUT_CAP` *(optional)* | Max tokens (estimated) of `reasoning_content` alone — before any real content or tool call has started — before that turn is abandoned and retried, same mechanism as `STREAM_OUTPUT_CAP` but on a much tighter budget. Catches a model that burns thousands of tokens re-deliberating a decision every turn without ever tripping the larger cap (since it does eventually act). Defaults to `3000` if unset. | `3000` |
| `LLM_REQUEST_TIMEOUT` *(optional)* | Seconds to wait on a single LLM request (connect + read) before giving up and offering Retry/Stop, instead of hanging with no feedback — matters most after idling, when a local server that unloaded its model (or a dead localhost socket) otherwise looks indistinguishable from a genuine freeze. Can be overridden per phase like `MODEL`/`OPENAI_URL`. Defaults to `120` if unset. | `120` |
| `THEME` *(optional)*| Live-console color preset — see [Themes](#themes)  | `dark-ocean`, `light-paper`, or empty for auto-detect |
| `SHOW_STREAM_PROMPTS` *(optional, debug)* | Prints every message sent to the LLM each turn, in full — no truncation anywhere, unlike the normal tool-result preview. Verbose by design; meant for prompt-engineering debugging, not everyday runs. Accepts `1`/`true`/`yes`/`on`. | `1` |
| `LOG_LLM_CALL_DEBUG` *(optional, debug)* | Appends every LLM request/response pair to `.jfi/llm_debug.jsonl` (with its role and episode) — one JSON object per line, full request (messages + tools) and full response (content + tool_calls), no truncation. Unlike `SHOW_STREAM_PROMPTS` (console/TUI-only, meant for watching a run live), this persists to disk so a run can be inspected afterward. Accepts `1`/`true`/`yes`/`on`. | `1` |
| `LAYA` *(optional)* | After each planning layer, a fixed rule decides whether a node is ready (GOOD) or needs splitting (BREAKDOWN). `1` adds a second score from [Laya](https://github.com/NandhaKishorM/laya)'s published `english` checkpoint, asked whether the node's task and description can be solved within 20k tokens (yes: GOOD, no: BREAKDOWN): if the two agree, that's the verdict; if they disagree and Laya is at least `LAYA_MIN_CONFIDENCE` sure, one short LLM call breaks the tie; otherwise the rule decides. Laya needs `uv sync --extra laya` when run from source; the `jfi` binary bundles it. Without it the rule decides alone. Unset: the rule alone. | `1` |
| `LAYA_DEVICE` / `LAYA_MIN_CONFIDENCE` *(optional)* | Where Laya runs (`cpu` by default, so it never competes with a local LLM for VRAM; or `cuda`), and how sure Laya must be (default 0.75) for a disagreement with the rule to go to the LLM tie-break. | `cpu` / `0.75` |
| `SESSION_PATH` *(optional, advanced)* | Where the `JFI/` session folder is created, relative to. Defaults to the current working directory. | `.` |
| `FLARESOLVERR_URL` *(optional)* | Base URL of a [FlareSolverr](https://github.com/FlareSolverr/FlareSolverr) instance. When a `curl` run via `execute_command` gets blocked by a site's anti-bot/DoS protection (Cloudflare challenge, etc.), the model is told to retry the request through this endpoint instead. Left unset, the model is just told the direct request was blocked. | `http://localhost:8192/` |

Every one of `OPENAI_URL` / `OPENAI_API_KEY` / `MODEL` / `TEMPERATURE` / `FREQUENCY_PENALTY` / `CONTEXT_SIZE` can also be set **per phase**, prefixed `PLANNER_`, `IMP_`, `REVIEWER_`, or `CLEANUP_` (and per role: `ARCHITECT_`, `LEAD_` and `TASK_` fall back to `PLANNER_`, `DEV_` to `IMP_`) (e.g. `REVIEWER_MODEL=gpt-4.1`, `IMP_OPENAI_URL=http://127.0.0.1:8080/v1`). A phase with no prefixed override falls back to the shared, unprefixed setting — so the default (unset) behavior is exactly one model for every phase, and you only add prefixed lines for the phases you actually want to route elsewhere (e.g. a cheap/fast model for Dev, a stronger one for `reviewer`). `CONTEXT_SIZE` in particular is worth setting per phase whenever a phase's model differs from the shared default's real context window — otherwise that phase's episodes are budgeted against the wrong window.

`.env` is loaded before anything else runs (see `runner.py::main()`): the one in the folder JFI is started from, or the nearest parent.

### Resuming a session

Start JFI again in the same project folder with the **same session name**. Everything lives in `.jfi/JFI.db`, so it skips every phase that already finished (`PLANNER_COMPLETE`, `IMP_COMPLETE`, … in the history) and re-reads the plan from the database, continuing mid-phase if it stopped there. `.jfi/.lock` allows one running session per project folder; a second one is refused with a clear message.

### When an LLM request fails

A transient failure (a dropped connection, a 5xx from the server, or a single response that outgrows `STREAM_OUTPUT_CAP` — a runaway/looping generation, abandoned mid-stream) is retried automatically a few times with a short delay. If those retries run out — or the failure wasn't the transient kind to begin with (a bad request, an auth error, ...) — JFI does **not** end the run on its own. It asks: **Retry now**, or **Stop (progress is saved)**. Pick Retry as many times as you need (fix the server, change `.env`, whatever it takes) and the same request just tries again; only an explicit Stop — from that menu or Ctrl+C — actually ends the run.

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

Then point the `.env` at it — either set `OPENAI_URL`/`OPENAI_API_KEY` yourself, or just set `LLM_BACKEND=ollama`/`LLM_BACKEND=llamacpp` and let JFI fill in that server's usual localhost defaults for you:

| Server        | `LLM_BACKEND`   | `OPENAI_URL`                    | `MODEL`                     | `OPENAI_API_KEY`      |
|---------------|------------------|---------------------------------|-----------------------------|-----------------------|
| Ollama        | `ollama` *(or unset + set `OPENAI_URL` yourself)* | `http://127.0.0.1:11434/v1`     | e.g. `qwen3:8b`             | anything (`ollama`)   |
| llama.cpp     | `llamacpp` *(same)* | `http://127.0.0.1:8080/v1`      | whatever you loaded         | anything (e.g. `llama`) |
| LM Studio     | `lmstudio` *(same)* | `http://127.0.0.1:1234/v1`      | whatever you loaded         | anything (`lm-studio`) |
| vLLM / others | unset            | that server's `/v1` URL         | the served model id         | real key if required  |
| Claude (Anthropic's own API, not an OpenAI-compatible proxy) | `anthropic` (or `claude`) | *(not used — talks to Claude's Messages API directly)* | e.g. `claude-sonnet-5` | *(use `ANTHROPIC_API_KEY` instead)* |

**Model size vs. your machine:**

- **~8GB VRAM/RAM** — comfortable territory for ~4B–8B quantized models (e.g. `qwen3:8b`, `llama3.1:8b` at Q4/Q5). Set `CONTEXT_SIZE` to what the server actually serves (typically 4096–16384); every episode's budget is capped at `CONTEXT_SIZE × CONTEXT_COMPRESSION_RATIO`, so a small window means shorter episodes and more, smaller plan nodes.
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
| `Ctrl+P` | While a phase is running        | Pauses the pipeline — holds before the next turn so an in-flight call finishes first. Press again to resume. |
| `Ctrl+N` | Idle (`PIPELINE COMPLETE`) only | Starts a brand-new session from scratch (fresh name + goal), without leaving the console. |
| `Ctrl+C` | Anytime                         | Requests a stop; state is saved, so rerunning with the same session name resumes. |


---

## Web dashboard (`jfi-web`)

An optional Streamlit page for watching *and driving* a session from a browser instead of, or alongside, the terminal — the goal, approvals, and new follow-up requests can all be typed there:

```bash
uv sync --extra web       # pulls in streamlit
JFI_WEB_BRIDGE=1 uv run jfi    # runs jfi AND auto-launches the dashboard for it
```

`JFI_WEB_BRIDGE=1` does two things: it makes JFI mirror its live phase/state/task/token/plan progress to `.jfi/web_status.json` every half-second and watch `.jfi/web_answer.json` for input, *and* it auto-launches the dashboard itself as a child process — no second terminal needed. Serves on port **7777** by default (`http://localhost:7777`) — set `JFI_WEB_PORT` to change it. Set `JFI_WEB_DASHBOARD=0` to keep just the status files without the auto-launch (e.g. you're running `jfi-web` on a different machine, which still works — the two processes share nothing in memory, only the files under `JFI/`, as long as `jfi-web` can see the same folder or a copy synced some other way).

This works two ways, and picks whichever applies with no configuration needed: if `jfi-web` is on `PATH` (the `uv sync --extra web` case above), that's what gets launched; otherwise, if you're running the standalone `dist/jfi` binary (`uv run build`), it re-launches *itself* to serve the dashboard instead — streamlit is bundled into that binary (`build_binary` runs `--collect-all streamlit` whenever it's present at build time), so `JFI_WEB_BRIDGE=1` gives you the dashboard with nothing else installed, in any directory, even one with no Python at all. If neither applies (running from source without the `web` extra synced), you get one line explaining that and nothing else happens — the terminal still works normally either way.

The very first time `jfi-web` runs on a machine, `launcher.py` pre-creates `~/.streamlit/credentials.toml` (empty email) before starting Streamlit, so it never blocks on Streamlit's own first-run "Welcome to Streamlit! ... Email:" activation prompt — without that, a non-interactive launch (a background job, a service, no TTY on stdin) would just hang or die with nothing listening on the port and no obvious error. If you already have that file from using Streamlit for something else, it's left untouched.

Whenever the terminal is waiting on input, the dashboard shows the same prompt — clickable buttons for a fixed choice (an `execute_command` approval, an LLM-retry prompt, …), a text box for a free-text one (the goal, or any other plain question); whichever side answers first — terminal keypress or dashboard submission — wins, so nothing double-answers. With `REVIEW_LOOP_APPROVAL=1` also set, a failed review pauses for an explicit Approve/Reject before JFI starts another fix iteration, answerable the same way. While nothing is pending, the dashboard instead shows a "queue a new request" box — the same thing as typing a line at the terminal's idle input, replayed once the current review phase lands. Both flags default off — without them JFI runs exactly as it always has, no new files, no new prompts, no auto-launched process.

One prompt still has to happen at the terminal: the very first "session name" question, since the session's own folder (and everything the dashboard reads) doesn't exist until it's answered. Everything from the goal prompt onward is answerable from either side.

The dashboard also shows `plan.md`'s checklist progress and the latest `review.md` (if any) for every session under `JFI/`, live status or not — only the live phase/state/tokens/approvals/new-request-box need the bridge running.

---

## Fleet dashboard ([`Just-Finish-It-Fleet`](https://github.com/Tejasvedagiri/Just-Finish-It-Fleet) — standalone Node master)

`jfi-web` needs a shared filesystem between the session and the dashboard. The fleet dashboard doesn't: its master is a small Node WebSocket server meant to run on its own machine (or just a different terminal) and watch *multiple* sessions, anywhere, over the network — a session and the master only ever talk over one socket, never files. The master lives in its own standalone repo, `Just-Finish-It-Fleet`, not this one and not Python — JFI (Python) sessions are pure WebSocket clients of it, so the wire protocol (plain WebSocket + JSON) is all that connects the two; nothing on the Python side cares what language the master is written in, or where its code lives.

```bash
# websockets is a base dependency: nothing extra to sync for the fleet client
git clone https://github.com/Tejasvedagiri/Just-Finish-It-Fleet.git ../Just-Finish-It-Fleet
cd ../Just-Finish-It-Fleet && npm install && npm run build   # build the fleet UI once
npm run master                                      # serves it on :8765 (MASTER_PORT to change)
```

Then, on each session you want it to watch (any machine that can reach the master's port):

```bash
MASTER_WS_URL=ws://<master-host>:8765/report uv run jfi
```

That's independent of `JFI_WEB_BRIDGE`/`jfi-web` — both can run at once, and neither depends on the other. A session identifies itself to the master with an opaque `hostname::session-id` string and a short repo-directory label, never a filesystem path (the master has no way to read a remote session's disk anyway).

Open `http://<master-host>:8765` for the fleet UI: a **Fleet** tab (every reporting session as a card, grouped by phase, with a live phase-distribution chart — click any card for detail), an **Activity** tab (a merged, chronological feed of every session's phase changes, ticks, and prompts), and a **Session** tab for the clicked session, with two sub-tabs:

- **Overview** — phase timeline, progress bars (phase/total/context), the current task, token detail, a **tasks-vs-tokens heatmap** (grouped into one sub-section per phase — Implement and Testing are numbered as separate trees in `plan.md`, so the same leaf number can legitimately appear in both, and a flat grid would conflate them), background processes this session started (`start_background_process` et al.), and a live log tail (last 400 lines).
- **Plan checklist** — the full `plan.md` tree exactly as the planner wrote it (depth from each leaf's own number, not raw indentation), done/pending/current state per leaf, and how long each one took (a live-updating duration for the leaf in progress, a fixed one — hover for the start/end clock time — for finished leaves).

A **controls bar** above both sub-tabs lets you drive the session, not just watch it: **Pause**/**Resume** (the same state Ctrl+P toggles locally), **Stop** (with a confirmation — progress is saved and it can be resumed later, same as Ctrl+C), and a **queue** box for a new follow-up request, plus the queued list itself. The **Awaiting input** panel's own option buttons are answerable straight from here too — whichever side answers first (terminal, `jfi-web`, or this dashboard) wins. All of it rides the same WebSocket a session already opens for `MASTER_WS_URL` (see `socket_reporter.py`'s `_dispatch_control`) — no new port, no shared filesystem, works across machines exactly like the read-only fields already did.

A theme dropdown top-right switches the whole palette — the same 20 presets `THEME` supports in the terminal itself (see [Themes](#themes)), derived into this app's full CSS token set from each preset's 5 raw colors, with a computed contrast floor so faint/muted text and badge labels stay legible on every preset — remembered per browser via `localStorage`. Everything shown is a real field from `get_status_snapshot()` or derived from it server-side — there's no invented "cost" or "model" number anywhere.

**Security:** the master ships with no authentication — anyone who can reach its port can see every reporting session's live status (task text, token counts, review findings) *and* control it (pause/resume/stop/queue/answer). Only run it where you already trust the network (behind a VPN, a firewalled LAN), the same trust model `execute_command` already has.

---

## `uv run build` — standalone binary

Builds a single native `jfi` executable with PyInstaller — the machine that runs it needs no Python or uv at all.

```bash
uv sync --extra web --group dev            # web is always bundled, plus pyinstaller
uv run build                               # -> dist/jfi (one file; dist\\jfi.exe on Windows)

uv sync --extra web --extra laya --extra anthropic --group dev
uv run build --laya --anthropic            # add extras by flag; --all for every one
```

| Flag | Bundles | For |
|---|---|---|
| *(always)* | Streamlit, websockets | the `jfi-web` dashboard, the fleet |
| `--laya` | Laya, torch, transformers (several GB) | `LAYA=1` |
| `--anthropic` | `anthropic` | `LLM_BACKEND=anthropic` |
| `--mysql` / `--postgres` | `pymysql` / `psycopg` | `DB_BACKEND` |
| `--all` | every extra above | |

`src/JFI/runner.py` is built with PyInstaller, bundling prompt_toolkit, the openai client and every base dependency, plus the extras above. An extra you didn't ask for is excluded even when it's synced, so the binary holds exactly what you asked for; one you asked for that isn't synced stops the build with the `uv sync` command to run. Without `--laya` the judge uses its rule alone; without `--anthropic`, `LLM_BACKEND=anthropic` stops with the rebuild command. The result is always a single file (`dist/jfi`, `dist\jfi.exe` on Windows) that can be copied anywhere on its own. The cost: it unpacks itself on every launch, which with torch (`--laya`, 2.2 GB) took 18-35 s before it started on Windows; a folder build started faster but its executable was useless without the `_internal/` directory beside it. Laya's checkpoint itself isn't bundled: it downloads from Hugging Face the first time `LAYA=1` runs. It reads the `.env` in the folder it's started from, like `uv run jfi`. Build logic lives in `src/build_binary/__init__.py`.

---

## Project layout

```
Just-Finish-It/
├── pyproject.toml                # package metadata, dependencies and extras (web, laya, anthropic, mysql, postgres)
├── JFI_ENV_TEMPLATE              # every .env setting, documented; `uv run create-env` fills it in
├── AGENTS.md                     # how to work in this repo (read before changing the pipeline)
├── TODO.md                       # what's still open
│
├── src/JFI/
│   ├── runner.py                 # main() -> run_pipeline() -> _run_session() -> run_phase(): the 4 phases, the review loop, queue/force/idle
│   ├── create_env.py             # `uv run create-env`: the setup wizard and readiness check
│   ├── create_env_detect.py      # what create-env measures: RAM/GPU (Linux, macOS, Windows), the model server, the model's speed -> computed settings
│   ├── episode/                  # the episode engine: one short, scoped LLM conversation (brief, role tools, token budget, turn cap, finish)
│   ├── planner/                  # Architect -> Lead -> Task: the loop, the node tools, the prompts, the judge (rule + optional Laya)
│   ├── imp/                      # Dev: the leaf queue and one gated episode per leaf (mark_leaf_done runs the leaf's test)
│   ├── review/                   # the reviewer (confirms the e2e itself, reopens broken leaves) and cleanup
│   ├── models/                   # SQLModel tables: .jfi/JFI.db is the session's real state (plan, history, episodes, verdicts, runbook, design, ...)
│   ├── session/                  # session persistence (history, metadata, queue) and the project-wide .jfi/.lock
│   ├── llm/                      # OpenAI-compatible and Anthropic backends, retries, LM Studio control
│   ├── manager/                  # the prompt_toolkit terminal UI, and the bridges to jfi-web (files) and the fleet (WebSocket)
│   ├── tool/                     # everything the model can call: code, commands, runbook/design, plan reads, context cache, images, browser, ...
│   ├── web/                      # jfi-web: the Streamlit dashboard and its launcher
│   └── utils/                    # small shared helpers (text_sanitize.py)
│
├── src/build_binary/             # `uv run build`: the PyInstaller binary, one file, dist/jfi (Streamlit and websockets always; --laya, --anthropic, ... by flag)
├── src/export_db/                # `uv run export-db`: a project's JFI.db as readable text
│
├── test/                         # pytest suite (see "Tests" below)
├── benchmark/                    # the eval harness, its tasks, score.py, and laya_poc.py (the judge's Laya check; not shipped)
├── utils/                        # dev-only: capture_theme_screenshots.py (real TUI screenshots for docs/images/)
└── docs/                         # pipeline.md and the phase pages, plan-tree.md, create_env_sizing.md, Themes.md, feature-description.md,
                                  # and the rewrite's design and build log (laya_plan.md, laya_impl_phases.md)
```

The fleet dashboard is its own repo, [Just-Finish-It-Fleet](https://github.com/Tejasvedagiri/Just-Finish-It-Fleet), not a folder here.

---

## Context cache

Alongside the plan, each session keeps a small persistent fact store (`ContextEntry` rows in `.jfi/JFI.db`) for
things worth remembering from one short episode to the next (each episode starts fresh), out
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

It's meant to stay small — a handful of high-value facts, not a transcript. One convention baked into every phase's own instructions: the first time a session works out the correct way to actually run/build/test the target project (which interpreter, which package-manager script, required env vars), it saves that under a stable `"run_commands"` key immediately — so a session never re-discovers it the hard way twice (e.g. `python3` failing with `ModuleNotFoundError`, then trying `.venv/bin/python`/`uv run`, then hitting the exact same wall again three phases later once that turn has aged out of context).

---

## The tool set (what the model can call)

Each episode gets only its role's tools (`ROLE_CORE_TOOLS` in `src/JFI/episode/roles.py`). Anything in the optional group can be added for the rest of an episode with `load_tool`. How a call travels through the engine is in `src/JFI/episode/engine.py`; every result is capped at `TOOL_RESULT_MAX_TOKENS`.

**Planning** (Architect, Lead, Task)

| Tool | What it does |
|------|--------------|
| `add_node` / `update_node` / `delete_node` | Write the plan tree. A node has a short description, `done_when`, `files`, and `notes` (what to implement and how) plus `references` (design entries, source ranges, docs) that the next layer's brief shows. Checked in code: scope, ownership, duplicates, `depends_on` ids and cycles, the depth cap, description length, and that a test file follows the runbook's `test_dir`. |
| `get_node` / `list_nodes` / `get_plan` | Read the plan. |
| `escalate` | Send the parent node back to the layer above when the node can't be fixed at this layer. |
| `design_set` / `design_get` | The design: stack, components, contracts, conventions, assumptions, a document's outline. |
| `runbook_set` / `runbook_get` | How to set up, run, stop, test (`test`, `test_one`), build, check (`e2e`) and run a scratch script (`script`), the file the app starts from (`entry`), plus the layout: `src_dir`, `test_dir`, `test_naming`. Operating the app is never a plan node. |
| `scaffold_file` / `unscaffold_file` | Create a file of stubs (a declaration and what it does; the body is generated), or remove one. |
| `mark_change` | Mark an existing function for Dev to change or delete. |
| `finish` | End the episode. The Architect's also requires the runbook, design and test setup (or the outline, for a document). |

**Reading and editing code** (all roles, by need)

| Tool | What it does |
|------|--------------|
| `outline_file` / `list_dir` | A cheap map of a folder or file (sections, functions, headings) with line numbers. |
| `list_symbols` / `read_symbol` / `replace_symbol` | Work one top-level function or class at a time. |
| `find_references` | Where a function or class is defined, imported and called (comments and strings skipped): every caller a change or delete leaf must update. |
| `read_file` / `search_code` | Read a file or a line range; search the project (text or regex). |
| `write_file` / `replace_in_file` / `copy_lines` | Write a file, patch one exact substring, or copy lines between files without retyping them. |
| `apply_patch` | Dev: a unified diff across one or more files in one call, all-or-nothing when a hunk doesn't match. |

**Building and checking** (Dev, reviewer, cleanup)

| Tool | What it does |
|------|--------------|
| `execute_command` | Run a shell command. Needs your approval unless `AUTO_APPROVE_COMMANDS=1`, or a saved command prefix matches. |
| `mark_leaf_done` | Dev's finish: runs the leaf's own test through the runbook's `test_one` (or a `check` command) and refuses until it passes. |
| `add_reviewer_note` / `get_reviewer_notes` | Dev's notes for the reviewer. |
| `reopen_leaf` | Reviewer: send a built leaf back to Dev with a fix note. |
| `leaf_diff` | Reviewer: what one leaf changed (its git checkpoint), or which leaves changed a file. |
| `check_page` | Reviewer (optional for others): load a URL in a headless browser -- the runbook's `view`, or a static page as `file:///...` -- and report console errors, exceptions, failed requests, the visible text and a screenshot. |
| `write_review_report` | Reviewer: record work the plan never covered, which starts another iteration. |
| `finish` | Reviewer's pass re-runs the runbook's `e2e` itself; a pass the e2e refutes is refused. |

**Optional** (added with `load_tool`)

| Tool | What it does |
|------|--------------|
| `start_background_process` / `list_processes` / `stop_background_process` / `clear_finished_processes` | Long-running commands (dev servers), tracked by handle, never by raw pid; the reviewer has the first and third by default. |
| `capture_screenshot` / `view_image` | Screenshot the screen, and attach an image so the model can see it. |
| `fetch_webpage_images` / `browse_webpage` | Download a page's images; load a page in a headless browser (runs its JavaScript). |
| `browser` | An interactive headless browser that stays open across calls: open a URL, screenshot it (the model sees the image), find, click and type by `[ref]` number, visible text or CSS selector, press keys, scroll, read the text, go back. Each call reports console errors and failed requests since the last one. For UI flows: fill a form, click a tab, check what changed. |
| `http_request` | Call an API the project serves: status, key headers and the body (JSON pretty-printed), instead of `curl` through the shell. |
| `extract_video_frames` | A video's visually distinct frames (needs `ffmpeg`). |
| `context_save` / `context_lookup` | The [context cache](#context-cache): facts one episode saves for later ones. |
| `ask_llm` | A one-off, stateless call to the role's own model, for text work no tool covers. |
| `append_to_file` | Build a long document in chunks. |

A result that starts with `Error` comes back with concrete `AUTO-RECTIFY` advice naming what to change, and after 3 identical failures the model is told to take a different approach.


---

## Tests

```bash
uv sync --extra web --extra anthropic --group dev
uv run pytest     # full suite (config in pyproject.toml: testpaths = ["test"])
```

On Windows, add `--ignore=test/test_session_lock.py` (it needs POSIX `fcntl`); everything else passes there. The project targets Linux/macOS.

Highlights:

- `test_planner.py` / `test_imp.py` / `test_reviewer.py` — each phase against a scripted model and a real SQLite DB (Dev's gate runs real pytest).
- `test_laya_judge.py` — the rule, Laya's sizing score (task + description, 20k tokens, the 0.4 cut-off), the LLM tie-break, and the `LAYA` flag.
- `test_checkpoint_tools.py` — per-leaf git checkpoints, `leaf_diff` and `revert`, against real git (the project's own repo untouched).
- `test_browser_session.py` / `test_browser_tools.py` — `browser` and `check_page` against a real headless Chromium and a real page.
- `test_project_memory.py` — a new session starting from the last session's runbook and design.
- `test_episode_engine.py` — episodes: budget, turn cap, isolation, forced input, retries.
- `test_phase_messages.py` / `test_phase_completion.py` — phase keys and completion-marker detection.
- `test_session_manager.py` — history append/resume semantics and metadata persistence.
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

What's still open is in [`TODO.md`](TODO.md).
