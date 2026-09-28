# Laya fine-tuning for the v2 planner judge

Fine-tunes a Laya checkpoint so `JFI.planner.judge` can tell whether a plan
node is **GOOD** (small enough for Dev), needs a **BREAKDOWN**, or needs a
**REDO** (an operational command or too vague to build).

## Result (held-out test, 101 nodes never trained on)

| Checkpoint | Raw accuracy | Judge (with confidence gate) |
|---|---|---|
| `english` (base) | 44% | 54% |
| `typed-decisions` (base) | 41% | 51% |
| head-only fine-tune, CPU, 140 rows | 65% | 62% |
| head-only fine-tune, GPU, 362 rows | 87% | 89% |
| **full fine-tune, GPU, 362 rows (recommended)** | **90%** | **88%** |

Confusion matrix of the recommended model (rows = correct answer):

| want ↓ / got → | GOOD | BREAKDOWN | REDO | recall |
|---|---|---|---|---|
| GOOD | **29** | 4 | 0 | 88% |
| BREAKDOWN | 2 | **36** | 0 | 95% |
| REDO | 1 | 3 | **26** | 87% |
| precision | 91% | 84% | 100% | |

The redo reason (operational vs vague) is right in 25 of 26 cases.

**Why "full" is recommended:** head-only and full are tied within noise
overall, but the full fine-tune passes a multi-part node as GOOD far less
often (2 vs 5). That's the costly mistake for the planner: Dev gets a job
too big for one short episode.

**What got it from 65% to 87%+:** the contrastive data
(`data/contrastive_nodes.py`): matched pairs of a single-thing and a
multi-part node at every level. The first model's main error was BREAKDOWN
called GOOD, 16 of 38; after the contrastive data it made that error 5
times.

**How it's trained:** the questions are the exact strings in
`src/JFI/planner/judge.py` (`questions_for`). **Changing them means
retraining.**

## Data

| File | Rows | What |
|---|---|---|
| `data/train.jsonl` | 362 | 20 hand-written apps (140) + 10 benchmark tasks (62) + 20 contrastive apps (160) |
| `data/test.jsonl` | 101 | 5 hand-written apps (49) + 8 *other* benchmark tasks (52). **Never train on it.** |

- **Row format:** `goal, level, path, node, done_when, files, label,
  redo_reason, source`.
- **`data/benchmark_nodes.py`:** plan nodes derived from `benchmark/tasks/`,
  split **by problem**, so a problem's Python and JS versions are always on
  the same side. Story tasks are skipped: they take the document path.
- **`data/contrastive_nodes.py`:** the matched GOOD/BREAKDOWN pairs; writes
  to train only, and asserts no overlap with the test set.
- **Augmentation** (in `finetune.py`): every row is also trained without
  `files`, and every REDO row also with a `done_when`. This breaks a
  shortcut observed in the first run, where empty `files` / `done_when`
  meant REDO.

## Run it

GPU (recommended; ~2 minutes on an RTX 5090). The project lock pins the CPU
build of torch, so install the CUDA build into the venv and run with
`--no-sync` so uv doesn't swap it back:

```bash
uv sync --extra laya --extra anthropic --group dev
uv pip install "torch==2.14.0" --index-url https://download.pytorch.org/whl/cu130 --reinstall-package torch
uv run --no-sync python laya-finetuning/finetune.py --base english --train-encoder --epochs 40 --patience 10 \
    --out laya-finetuning/checkpoints/jfi-judge-english-full
uv run --no-sync python laya-finetuning/evaluate.py --checkpoint english=laya-finetuning/checkpoints/jfi-judge-english-full
```

- **Without `--train-encoder`:** head-only, with the encoder frozen and its
  outputs cached. It works on CPU (~30 min) and on GPU (~2 min).
- **Checkpoints** land in `checkpoints/` (git-ignored, ~1.7 GB each).

## Use it in JFI

```bash
# .env
LAYA_ENGLISH_PATH=laya-finetuning/checkpoints/jfi-judge-english-full
```

The judge then loads this folder instead of the published `english`
checkpoint (`typed-decisions` still comes from the Hub).

## Recipe (what `finetune.py` does)

1. **Data:** each row becomes exactly the request the judge sends:
   `build_state(...)` + `questions_for(level)`, normalised with
   `Agent._to_internal` and laid out by `laya.common.build_sequence`. The
   target is a label-smoothed one-hot over A/B/C (GOOD / BREAKDOWN / REDO).
   REDO rows also train the `redo_reason` question.
2. **Loss:** soft cross-entropy on the masked option logits. That's the
   supervised half of Laya's RLCD recipe; the policy-gradient half and the
   2-GPU notebook schedule aren't needed at this data size.
3. **Validation:** a stratified 15% validation slice picks the best epoch.
   One choice temperature is fitted on it (LBFGS on log T, clamped to
   [0.1, 10]). `temperature_by_options` is dropped so it can't mask the fit.
4. **Output:** the same layout as a Hub checkpoint (`rl_agent_config.json`,
   `model.safetensors` in the base's dtype, `tokenizer/`, `encoder/`), so
   `Router(models={"english": <dir>})` loads it unchanged.
