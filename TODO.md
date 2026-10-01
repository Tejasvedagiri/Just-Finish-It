# TODO

Open work. What's done, and the runs that checked it, is in
[`docs/feature-description.md`](docs/feature-description.md); the Laya
sizing POC is in [`docs/laya_poc.md`](docs/laya_poc.md).

## Open

1. **Commit `feature/description`** in both repos (JFI and
   Just-Finish-It-Fleet). Nothing since `c70fa82` is committed.
2. **Update the QA machine's old install** (macOS). It still runs a v1 build
   (Product Owner and Testing phases). On that machine: stop the old
   session, `git pull`, `uv sync --extra web --extra laya`,
   `uv run create-env`, then `uv run build` or `uv run jfi`. A v1 session
   can't be resumed; a new session in the same project starts from the last
   v2 session's runbook and design.

## Fixed 2026-10-01

- **Laya's score is the sizing question.** With `LAYA=1` the judge asks
  Laya's `english` checkpoint, for every node, "Can this be solved with 20k
  tokens?" given only the task and its description (the node's notes);
  P(yes) >= 0.4 is GOOD, otherwise BREAKDOWN. The decision is as before:
  Laya and the rule agree -> that's the verdict; they disagree and Laya's
  confidence >= `LAYA_MIN_CONFIDENCE` -> the LLM tie-break; otherwise the
  rule. On the POC's 67 nodes it ranks them at AUC 0.76 and is 73% right at
  the 0.4 cut-off (88% on nodes that needed a breakdown). Rerun
  `benchmark/laya_poc.py` on new runs to keep checking the cut-off.

## Dropped

- The fix-loop test on a real run (a reopened leaf). `reopen_leaf` and its
  `revert` stay unit-tested (`test_reviewer.py`, `test_checkpoint_tools.py`);
  the escalation path did run for real.

## Not planned

Set aside on 2026-10-01; kept so they aren't rediscovered.

- Ollama, llama.cpp and vLLM detection is untested against real servers
  (only LM Studio was checked live).
- AMD GPUs on Windows report "unknown"; Laya on Apple Silicon stays on the
  CPU (no `mps`). Decisions 9 and 10 in
  [`docs/create_env_sizing.md`](docs/create_env_sizing.md).
