# `create-env`: compute the settings from the machine and the model

**Status:** built (2026-09-29) with every proposal below as written: detection in
`src/JFI/create_env_detect.py`, the wizard in `src/JFI/create_env.py`, the
session-start check in `runner._run_session`. Tests:
`test/test_create_env_detect.py` (every platform's readers, fed real output)
and `test/test_create_env.py`. One change from the table: the `LAYA` question
is always asked, with a note when the `laya` extra isn't installed.

## The problem

`uv run create-env` offers fixed defaults (`CONTEXT_SIZE=32768`,
`CONTEXT_COMPRESSION_RATIO=0.7`, `REASONING_OUTPUT_CAP=3000`, …). They're
right for nobody in particular, and wrong in ways that cost real runs:

- **LM Studio silently reloaded the model at a 4,096-token context** while
  `.env` said 38,000. Every reply was cut off mid-reasoning and the run died
  in 4 minutes. Nothing checked the two against each other.
- **`MASTER_WS_URL=8765`** — a bare port accepted as a URL (fixed since).
- **A 15-turn cap on a 30k-token budget:** the turn cap, not the budget, ended
  two Dev leaves on the last calc run.

The server and the machine already know most of the right answers. The wizard
should **measure, compute, explain, and let the user override**.

## What we can detect (verified on this machine)

| Source | What it gives | Example here |
|---|---|---|
| LM Studio `GET /api/v0/models` | per model: `state` (loaded / not-loaded), `max_context_length`, **`loaded_context_length`**, `arch`, `quantization`, `capabilities` (`tool_use`) | qwen3.8-27b: loaded at **38,144**, max 262,144 |
| `lms ps --json` (LM Studio CLI) | the loaded model's `sizeBytes`, `paramsString`, `quantization`, `contextLength`, `parallel` | 23.4 GB, 27B, Q6_K, 38,144 |
| `nvidia-smi --query-gpu=name,memory.total,memory.used` | GPU name, VRAM total/used | RTX 5090, 32,607 MiB total, 29,696 used |
| System RAM | total / free RAM | 61.6 GB / 16.3 GB free |
| `os.cpu_count()` | cores | 12 |
| One short timed request to the chosen model | **tokens per second** (and that tool calling works) | — |

Not verified here (no server running); per their docs, to confirm while building:

| Backend | Loaded context | Model size / params |
|---|---|---|
| Ollama | `POST /api/show` → `model_info.*.context_length`; `GET /api/ps` → loaded models, `size_vram` | `details.parameter_size`, `quantization_level` |
| llama.cpp server | `GET /props` → `n_ctx` | `GET /v1/models` → `meta.n_params`, `n_ctx_train` |
| vLLM | `GET /v1/models` → `max_model_len` | — |
| OpenAI / Anthropic (hosted) | not reported → the known-model table `create_env` already has (`guess_context_size`) | — |

Everything stays stdlib-only (`create-env` must run before dependencies are
installed): `urllib`, `subprocess`, `ctypes`. Any probe that fails just means
"unknown", and the value falls back to today's default with the reason
"couldn't detect".

## Platforms: Linux, macOS and Windows

Every probe has a Linux, a macOS and a Windows path. **Linux and macOS are the
project's main targets**; nothing may be Windows-only. The same `compute()`
runs on all three — only the readers differ.

| What | Linux | macOS | Windows |
|---|---|---|---|
| **Total / free RAM** | `/proc/meminfo`: `MemTotal`, `MemAvailable` | `sysctl -n hw.memsize`; free from `vm_stat` (free + inactive + speculative pages × page size) | `ctypes` → `GlobalMemoryStatusEx` |
| **NVIDIA GPU** | `nvidia-smi --query-gpu=name,memory.total,memory.used --format=csv,noheader,nounits` | — (no current Macs) | same `nvidia-smi` |
| **AMD GPU** | `rocm-smi --showmeminfo vram --json`; without ROCm, `/sys/class/drm/card*/device/mem_info_vram_total` and `mem_info_vram_used` | Intel Macs with AMD: `system_profiler SPDisplaysDataType -json` (VRAM total only) | not detected → "unknown" |
| **Apple Silicon** | — | unified memory: GPU-usable ≈ 75% of RAM (Metal's default working-set limit), detected via `sysctl -n hw.optional.arm64` = 1; the chip name from `sysctl -n machdep.cpu.brand_string` | — |
| **Intel / no GPU** | CPU only: models run from RAM | CPU only | CPU only |
| **Cores** | `os.cpu_count()` | same | same |
| **LM Studio CLI** | `lms` on `PATH`, else `~/.lmstudio/bin/lms` | same | `lms` on `PATH`, else `%USERPROFILE%\.lmstudio\bin\lms.exe` |
| **Server APIs** (LM Studio, Ollama, llama.cpp, vLLM, hosted) | HTTP, identical everywhere | same | same |

What changes per platform in the computed values:

- **Where the model lives:** a discrete GPU's VRAM, Apple Silicon's shared
  memory, or plain RAM on CPU-only machines. "Does the model fit" and the free
  headroom use whichever applies.
- **`LAYA_DEVICE`:** `cuda` only with an NVIDIA GPU and ≥ 4 GB free; otherwise
  `cpu`. Apple's `mps` isn't accepted by `laya_device()` today (decision 9).
- **Apple Silicon memory is shared** by the model, Laya and everything else,
  so `UNLOAD_LLM_BEFORE_LAYA` is judged against the same pool as the model.

Tests fake each reader's output (a `/proc/meminfo` sample, `vm_stat` and
`sysctl` output, `nvidia-smi` and `rocm-smi` output, `system_profiler` JSON)
and run on every OS, so a Linux or Mac path can't break unseen on a Windows
dev machine — and vice versa.

## What we'd compute

Each value is shown with **why**, e.g.

```
CONTEXT_SIZE [38144]   <- LM Studio has qwen3.8-27b loaded with a 38,144-token context
```

| Setting | Computed from | Rule | This machine |
|---|---|---|---|
| **`CONTEXT_SIZE`** | the server's **loaded** context | exactly what's loaded. If the model isn't loaded: see decision 2. | 38,144 |
| **`CONTEXT_COMPRESSION_RATIO`** | context and the reply reserve | budget = context − reply reserve; ratio = budget / context, clamped 0.5–0.9 | 0.73 (27.8k budget, 10.3k for the reply) |
| **Reply reserve** (not a setting, drives the two above and below) | whether the model reasons (`arch`/name: qwen3, deepseek-r1, gpt-oss, "thinking"…) | reasoning models: 10k (the calc runs saw single replies up to 8,023 tokens); others: 4k | 10k |
| **`STREAM_OUTPUT_CAP`** | **measured** (2026-10-06): three short JFI-like requests (plan with a tool, implement a function, fix a bug; `REPLY_PROBES`), each reply's thinking and answer counted | twice the longest reply; at least 8,500 for a model that thinks (real calc runs saw 8,023-token replies, far above any test reply); at most 45% of the context; the test limit if a reply never finished | 8,500 (test replies up to ~3,560) |
| **`REASONING_OUTPUT_CAP`** | the stream cap | the stream cap minus room for the answer and tool call after the thinking; 2,000 for a model that didn't think | 7,500 |
| **`LLM_REQUEST_TIMEOUT`** | measured tokens/s | 2 × (STREAM_OUTPUT_CAP / tok/s) + 60 s for prompt processing, minimum 120 | ~260 s at ~100 tok/s |
| **`TOOL_RESULT_MAX_TOKENS`** | episode budget | ~10% of the budget, rounded, 2k–8k | 3,000 |
| **`MAX_EPISODE_TURNS`** | not machine-dependent | 25 (see decision 5) | 25 |
| **`LAYA_DEVICE`** | free VRAM after the model | `cuda` if ≥ 4 GB VRAM free with the model loaded, else `cpu` | cpu (2.8 GB free) |
| **`UNLOAD_LLM_BEFORE_LAYA`** | free RAM | on if free RAM < 6 GB (Laya's ~3.5 GB plus headroom) | off |
| **`LAYA`** | whether the `laya` extra is installed | offered only when it is; default off | off |
| **`PARALLEL_LLM`** | the loaded model's `parallel` (`lms ps`) / llama.cpp's `total_slots`, its loaded context | min(slots, loaded context // `CONTEXT_SIZE`, 10); hosted: 4; Ollama or nothing reported: 1 (added with `feature/parllel`, see `docs/phase-planner.md`) | 1 (38,144 loaded = one 38,144 episode) |
| **`MODEL`** | the server's model list | prefer the **loaded** model; if none is loaded, mark which ones fit in VRAM (`sizeBytes` ≤ free VRAM) | qwen/qwen3.8-27b |

Not computed (they're preferences, not sizing): `TEMPERATURE`,
`FREQUENCY_PENALTY`, `THEME`, the dashboards, the approval and debug flags,
`PLANNER_ITEM_MAX_CHARS` (keep 200: the run with 500 got bigger leaves and
more overflows), the planner caps.

## Checks the wizard would add

1. **Loaded context vs `CONTEXT_SIZE`.** If `.env` asks for more than the
   server loaded, say so in red and offer the loaded value. (The 4,096 run.)
2. **The model is actually loaded** (LM Studio / Ollama): if not, say that the
   first request will load it at the server's *default* context, which may be
   small — and offer the `lms load … --context-length N` command.
3. **Tool calling works:** the timed request asks for one tool call; a model
   that can't do tool calls can't run JFI.
4. **Memory headroom:** model size vs the memory it runs from (GPU VRAM,
   Apple Silicon's shared memory, or RAM on CPU-only machines), and a warning
   if the chosen context plus the model won't fit (see decision 2 for how
   precise to be).

## Where it runs

- **Inside the wizard**: a "Detected" block first (machine, server, model,
  speed), then the usual prompts with computed values as the defaults.
- **In the non-interactive check** (`create-env` without a terminal): the same
  detection and the mismatch warnings, no prompts.
- **Optional, at JFI start** (decision 7): check 1 (loaded context vs
  `CONTEXT_SIZE`) as a one-line warning, since the server can be reloaded
  between `create-env` and a run.

## Decisions

1. **Existing `.env` values vs computed ones.** When `.env` already has a value
   that differs from the computed one, which is the default at the prompt?
   *Proposal:* the computed value, with "(currently 38000 in .env)" shown next
   to it. The user said "do not take default values", and an old value is how
   the 4,096 mismatch happened.
2. **The model isn't loaded.** Then the context is unknown. Options:
   (a) recommend a context from free VRAM with a rough rule (KV cache per
   token isn't reported by any server, so this is an estimate), or
   (b) recommend loading it first — show the `lms load` command with a
   suggested context — and use its max context capped at 32,768 meanwhile.
   *Proposal:* (b); an honest "load it, then rerun" beats a guessed number.
3. **The timed request.** It costs a few seconds and generates ~200 tokens.
   Always, or only when asked? *Proposal:* always; it also proves tool calling
   works.
4. **Reply reserve for reasoning models: 10k.** Based on the calc runs (single
   replies up to 8,023 tokens). *Proposal:* 10k, with the reason shown.
5. **`MAX_EPISODE_TURNS` 15 → 25.** The token budget was never the limit on
   the last run; the turn cap ended two leaves (one of them already done).
   Pairs with the two Dev fixes proposed earlier (run the leaf's test before
   splitting; warn two turns before the cap). *Proposal:* 25.
6. **Per-phase overrides.** If a phase uses a different model/server, run the
   same detection for it. *Proposal:* yes, same code with the phase prefix.
7. **The check at JFI start** (loaded context vs `CONTEXT_SIZE`, one warning
   line, no prompt). *Proposal:* yes — the server can change after setup.
8. **Hosted backends** (OpenAI, Anthropic): nothing to measure except speed;
   context from the known-model table as today, ratio 0.8, timeout from the
   timed request. *Proposal:* as described.
9. **Laya on Apple Silicon.** `laya_device()` accepts `cpu` and `cuda` only.
   Add `mps` (Apple's GPU) so a Mac can run Laya off the CPU? *Proposal:* not
   now — keep `cpu` on Macs, and add `mps` only after trying it on a real Mac.
10. **AMD GPUs on Windows** have no dependable stdlib probe. *Proposal:* report
    "unknown" and fall back to RAM-based advice there; Linux AMD is covered.

## Build notes

- New module `JFI.create_env_detect` (stdlib only): one reader per platform
  and source (`_ram_linux`, `_ram_macos`, `_ram_windows`, `_gpu_nvidia`,
  `_gpu_amd_linux`, `_gpu_apple`, …) behind `detect_system()`,
  `detect_server(backend, url, key)`, `measure_speed(...)`, `compute(settings,
  detected)` → a list of `(name, value, reason)`. `create_env.py` only asks and
  writes.
- `compute` is a pure function: tested with recorded server responses (the
  LM Studio JSON above, Ollama/llama.cpp samples) and fake hardware, no
  network.
- `JFI_ENV_TEMPLATE` and the README's "Configure" step describe what's
  computed and how to override it.
